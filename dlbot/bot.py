"""Update handlers: link -> quality menu -> video, or a link when too big."""
from __future__ import annotations

import asyncio
import itertools

import aiohttp
import logging
import re
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from . import texts as T
from . import ytdl
from .cache import LRU
from .config import LINK_TTL, SAFE_UPLOAD_LIMIT, Config
from .formats import (Option, bucket, build_options, direct_url, entries_of, entry_is_resolved, mp4_meta, plan,
                      sizeless_direct)
from .signing import sign
from .tg import Telegram, TgError, keyboard
from .thumbs import make_thumb

log = logging.getLogger("bot")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
EDIT_EVERY = 3.0
INFO_FRESH = 20 * 60      # reuse extracted media URLs for this long, then extract again
KEEP_INFOS = 6            # info dicts are large; keep only the newest few in RAM
AUTO_MAX_P = 720          # quality cap for the automatic pick when we must upload the file ourselves
PROBE_BYTES = 64 * 1024   # MP4 header (size via Content-Range, height and duration via the moov box)


@dataclass
class Pending:
    url: str
    title: str = ""
    duration: float | None = None
    options: list[Option] = field(default_factory=list)
    index: int | None = None          # 1-based item inside a carousel
    entries: list[dict] = field(default_factory=list)  # playlist / carousel menu
    carousel: bool = False
    info: dict | None = None          # extract() result, reused for the download (no second extraction)
    t: float = field(default_factory=time.monotonic)


def find_url(msg: dict) -> str | None:
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("type") == "text_link" and ent.get("url"):
            return ent["url"]
    m = URL_RE.search(msg.get("text") or msg.get("caption") or "")
    return m.group(0).rstrip(").,]") if m else None


def auto_option(options: list[Option]) -> Option:
    fits = [o for o in options if not o.best and o.size is not None and o.size <= SAFE_UPLOAD_LIMIT]
    if fits:
        return fits[-1]
    unknown = [o for o in options if o.size is None]
    return unknown[-1] if unknown else options[0]


def quick_option(options: list[Option]) -> Option:
    """Pick for an automatic send: the best one Telegram fetches by URL itself (costs us nothing),
    else the best one up to AUTO_MAX_P that fits the upload cap, else auto_option()."""
    by_url = [o for o in options if not o.best and plan(o) == "url_send"]
    if by_url:
        return by_url[-1]
    capped = [o for o in options if not o.best and 0 < o.height <= AUTO_MAX_P and o.size is not None
              and o.size <= SAFE_UPLOAD_LIMIT]
    if capped:
        return capped[-1]
    return auto_option(options)


def is_quick(options: list[Option]) -> bool:
    """Send at once only when there is nothing to choose: every option has the same height (an
    Instagram reel's progressive file and its "best" DASH twin are both 720p). With two or more
    heights the menu comes first and only the chosen one is downloaded (Sajjad, 2026-10-26: the
    quality choice must come before the download; social links and short clips were sent at once)."""
    return len({bucket(o.height) for o in options}) <= 1


def option_rows(rid: str, options: list[Option]) -> list[list[tuple[str, str]]]:
    buttons = [(f"{o.label} · {T.human_size(o.size, o.approx)}", f"q:{rid}:{i}") for i, o in enumerate(options)]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _time_read(path: str) -> float:
    t = time.monotonic()
    with open(path, "rb") as fh:
        while fh.read(1 << 20):
            pass
    return round(time.monotonic() - t, 2)


def _cgroup_mb() -> int | None:
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.current"):
        try:
            with open(path) as fh:
                return round(int(fh.read().strip()) / 1048576)
        except Exception:
            continue
    return None


def dl_timings(progress: dict) -> dict:
    """prep = yt-dlp work before the first byte, transfer = bytes on the wire, merge = ffmpeg."""
    s0 = progress.get("t_start")
    if not s0:
        return {}
    fb, de = progress.get("t_first_byte"), progress.get("t_dl_end")
    pp, ppe = progress.get("t_pp"), progress.get("t_pp_end")
    return {"prep": round((fb or s0) - s0, 2), "transfer": round(de - fb, 2) if fb and de else 0,
            "merge": round(ppe - pp, 2) if pp and ppe else 0, "files": progress.get("files"),
            "bytes": progress.get("done")}


class Bot:
    def __init__(self, cfg: Config, tg: Telegram):
        self.cfg = cfg
        self.tg = tg
        self.pending = LRU(maxsize=64, ttl=6 * 3600)
        self.file_ids = LRU(maxsize=300)
        self.jobs = asyncio.Semaphore(cfg.max_jobs)
        # extraction is mostly network wait now (warm deno): 4 at once, so an X link does not wait 8 s
        # behind two YouTube extractions (measured 2026-10-26 with 4 links at once)
        self.extracts = asyncio.Semaphore(4)
        self._ids = itertools.count(1)
        self.tasks: set[asyncio.Task] = set()
        self._with_info: deque[Pending] = deque()
        self.busy: set[tuple[int, int]] = set()   # (chat, message) with a job running: double taps are ignored

    # entry point -----------------------------------------------------------
    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def handle_update(self, upd: dict):
        date = (upd.get("message") or {}).get("date")
        if date:   # how late Telegram delivered it (retries after webhook errors show up here)
            log.info("update age=%.1fs", time.time() - date)
        try:
            if "callback_query" in upd:
                await self.on_callback(upd["callback_query"])
            elif "message" in upd:
                await self.on_message(upd["message"])
        except Exception:
            log.exception("update failed")

    def allowed(self, user: dict | None) -> bool:
        return bool(user) and user.get("id") in self.cfg.allowed_ids

    # messages ----------------------------------------------------------------
    async def on_message(self, msg: dict):
        chat = msg["chat"]["id"]
        if not self.allowed(msg.get("from")):
            await self.tg.send(chat, T.NOT_ALLOWED)
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/help"):
            await self.tg.send(chat, T.START)
            return
        url = find_url(msg)
        if not url:
            await self.tg.send(chat, T.NO_LINK)
            return
        status = await self.tg.send(chat, T.CHECKING, reply_to=msg["message_id"])
        await self.analyze(chat, status["message_id"], url)

    async def analyze(self, chat: int, mid: int, url: str):
        t0 = time.monotonic()
        try:
            async with self.extracts:
                info = await asyncio.to_thread(ytdl.extract, url, self.cfg.cookie_file)
            log.info("timing extract=%.1fs url=%s", time.monotonic() - t0, url)
        except Exception as e:
            log.warning("extract failed for %s: %s", url, e)
            await self.tg.edit(chat, mid, T.error_text(e))
            return
        await self.fill_sizes(info)
        rid = self.new_id()
        entries = entries_of(info)
        if entries:
            resolved = [e for e in entries if entry_is_resolved(e)]
            if resolved and len(resolved) == len(entries):
                vids = [(i + 1, e) for i, e in enumerate(entries) if build_options(e)]
                if not vids:
                    await self.tg.edit(chat, mid, T.NO_VIDEO)
                    return
                if len(vids) == 1:
                    idx, e = vids[0]
                    return await self.offer(chat, mid, Pending(url, e.get("title") or info.get("title") or "",
                                                               e.get("duration"), build_options(e), index=idx, info=e))
                p = Pending(url, info.get("title") or "", entries=[dict(e, _index=i) for i, e in vids], carousel=True)
                self.pending.put(rid, p)
                rows = [[(T.ITEM.format(i=T.fa(n + 1)), f"e:{rid}:{n}")] for n in range(len(vids))]
                rows.append([(T.ALL_ITEMS, f"a:{rid}")])
                await self.tg.edit(chat, mid, T.CAROUSEL.format(n=T.fa(len(vids))), keyboard(rows))
                return
            p = Pending(url, info.get("title") or "", entries=entries[:6])
            self.pending.put(rid, p)
            rows = [[((e.get("title") or f"#{n + 1}")[:40], f"e:{rid}:{n}")] for n, e in enumerate(p.entries)]
            await self.tg.edit(chat, mid, T.PLAYLIST.format(n=T.fa(len(entries))), keyboard(rows))
            return
        options = build_options(info)
        if not options:
            await self.tg.edit(chat, mid, T.NO_VIDEO)
            return
        await self.offer(chat, mid, p=Pending(info.get("webpage_url") or url, info.get("title") or "",
                                              info.get("duration"), options, info=info), rid=rid)

    async def offer(self, chat, mid, p: Pending, rid: str | None = None):
        """Quality menu first; only a video with a single quality goes out right away."""
        if not is_quick(p.options):
            return await self.show_menu(chat, mid, p, rid)
        await self.deliver(chat, mid, p, quick_option(p.options))

    async def fill_sizes(self, info: dict):
        """Instagram/X often give no file size, and Instagram's progressive files no height or duration
        either (the menu showed «ویدیو» and «حجم نامعلوم»). Read the first 64 KB of each open-CDN file in
        parallel (~0.3 s): Content-Range gives the size, the MP4 header the height and the duration (which
        also sizes the DASH options from their bitrate)."""
        pairs = [(item, f) for item in (entries_of(info) or [info]) for f in sizeless_direct(item)][:6]
        session = await self.tg.session() if pairs else None
        if session is None:
            return
        t0 = time.monotonic()

        async def one(item, f):
            try:
                headers = dict(f.get("http_headers") or {}, Range=f"bytes=0-{PROBE_BYTES - 1}")
                async with session.get(f["url"], headers=headers, allow_redirects=True,
                                       timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status not in (200, 206):
                        return
                    total = r.headers.get("Content-Range", "").rpartition("/")[2]
                    n = int(total) if total.isdigit() else (int(r.headers.get("Content-Length") or 0)
                                                            if r.status == 200 else 0)
                    head = b""
                    while len(head) < PROBE_BYTES:   # never the whole file, even when Range is ignored
                        chunk = await r.content.read(PROBE_BYTES - len(head))
                        if not chunk:
                            break
                        head += chunk
                if n > 0 and not f.get("filesize"):
                    f["filesize"] = n
                meta = mp4_meta(head)
                if meta.get("height") and not f.get("height"):
                    f["width"], f["height"] = meta["width"], meta["height"]
                if meta.get("duration") and not item.get("duration"):
                    item["duration"] = round(meta["duration"], 1)
            except Exception:
                pass

        await asyncio.gather(*(one(item, f) for item, f in pairs))
        log.info("timing sizes=%.2fs n=%d", time.monotonic() - t0, len(pairs))

    async def show_menu(self, chat, mid, p: Pending, rid: str | None = None):
        rid = rid or self.new_id()
        self.pending.put(rid, p)
        self.keep_info(p)
        head = p.title[:200]
        dur = T.human_duration(p.duration)
        text = f"{head}\n{('مدت: ' + dur) if dur else ''}\n{T.CHOOSE}".replace("\n\n", "\n")
        await self.tg.edit(chat, mid, text, keyboard(option_rows(rid, p.options)))

    def keep_info(self, p: Pending):
        if p.info is None:
            return
        self._with_info.append(p)
        while len(self._with_info) > KEEP_INFOS:
            old = self._with_info.popleft()
            old.info = None

    def new_id(self) -> str:
        n = next(self._ids)
        return format(int(time.time()) % 100000 * 1000 + n % 1000, "x")

    # buttons -----------------------------------------------------------------
    async def on_callback(self, cq: dict):
        await self.tg.answer(cq["id"])
        if not self.allowed(cq.get("from")):
            return
        msg = cq.get("message") or {}
        chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
        parts = (cq.get("data") or "").split(":")
        p: Pending | None = self.pending.get(parts[1]) if len(parts) > 1 else None
        if p is None or chat is None:
            if chat is not None:
                await self.tg.edit(chat, mid, T.EXPIRED)
            return
        kind = parts[0]
        if kind in ("q", "a"):
            if (chat, mid) in self.busy:   # double tap on the same button: one job is enough
                return
            self.busy.add((chat, mid))
        try:
            await self._on_button(kind, parts, p, chat, mid)
        finally:
            if kind in ("q", "a"):
                self.busy.discard((chat, mid))

    async def _on_button(self, kind: str, parts: list[str], p: Pending, chat: int, mid: int):
        if kind == "q":
            opt = p.options[int(parts[2])]
            await self.deliver(chat, mid, p, opt)
        elif kind == "e":
            e = p.entries[int(parts[2])]
            if p.carousel:
                await self.offer(chat, mid, Pending(p.url, e.get("title") or p.title, e.get("duration"),
                                                    build_options(e), index=e["_index"], info=e, t=p.t))
            else:
                await self.tg.edit(chat, mid, T.CHECKING)
                await self.analyze(chat, mid, e.get("url") or e.get("webpage_url"))
        elif kind == "a" and p.carousel:
            await self.send_all(chat, mid, p)

    async def send_all(self, chat: int, mid: int, p: Pending):
        """Every video of a carousel, one after another; `mid` (menu or status) is removed at the end."""
        await self.tg.edit(chat, mid, T.PREPARING)
        for e in p.entries:
            opts = build_options(e)
            sub = Pending(p.url, e.get("title") or p.title, e.get("duration"), opts, index=e["_index"], info=e, t=p.t)
            status = await self.tg.send(chat, T.PREPARING)
            await self.deliver(chat, status["message_id"], sub, quick_option(opts))
        await self.tg.delete(chat, mid)

    # delivery ------------------------------------------------------------------
    async def deliver(self, chat: int, mid: int, p: Pending, opt: Option):
        try:
            res = await self.run_job(chat, mid, p, opt)
        except Exception as e:
            log.exception("job failed")
            await self.tg.edit(chat, mid, T.error_text(e))
            return
        if res.get("link"):
            await self.send_link(chat, mid, p, opt, res["link"])
        else:
            await self.tg.delete(chat, mid)

    async def run_job(self, chat: int, mid: int | None, p: Pending, opt: Option, reuse: bool = True,
                      quiet: bool = False, diag: dict | None = None) -> dict:
        """Send one video. Returns timings; {"link": mode} when it must go out as a link instead."""
        t0 = time.monotonic()
        tm: dict = {"spec": opt.spec, "size": opt.size}
        key = (p.url, p.index, opt.spec)
        caption = p.title[:900]
        extra = {"disable_notification": True} if quiet else {}

        async def status(text):
            if mid is not None:
                try:
                    await self.tg.edit(chat, mid, text)
                except TgError:
                    pass

        fid = self.file_ids.get(key)
        if fid and not quiet:
            try:
                res = await self.tg.call("sendVideo", chat_id=chat, video=fid, caption=caption,
                                         supports_streaming=True)
                tm.update(mode="cached", total=round(time.monotonic() - t0, 2), msg=res.get("message_id"))
                log.info("timing job %s", tm)
                return tm
            except TgError:
                log.info("cached file_id failed, downloading again")
        info = p.info if (reuse and p.info is not None and time.monotonic() - p.t < INFO_FRESH) else None
        tm["reuse"] = info is not None
        mode = plan(opt)
        tm["mode"] = mode
        if mode in ("direct_link", "stream_link"):
            return tm | {"link": mode}
        v = opt.video
        meta = dict(duration=int(p.duration) if p.duration else None, width=v.get("width"), height=v.get("height"))
        tmpdir = tempfile.mkdtemp(prefix="dl-")
        thumb_task = None
        if p.info:
            thumb_task = asyncio.create_task(make_thumb(await self.tg.session(), p.info, tmpdir))
        progress = {"done": 0, "cur": 0, "total": opt.size, "phase": "dl"}
        ticker = asyncio.create_task(self.tick(chat, mid, progress)) if mid is not None else None
        try:
            if mode == "url_send":
                thumb = await self._thumb(thumb_task, tm, wait=2)   # Telegram is fast here; do not wait long
                await status(T.UPLOADING)
                t1 = time.monotonic()
                try:
                    res = await self.tg.send_video(chat, direct_url(opt), is_file=False, caption=caption,
                                                   thumb=thumb, **meta, **extra)
                    tm["url_send"] = round(time.monotonic() - t1, 2)
                    return self._done(tm, t0, key, res)
                except TgError as e:
                    tm["url_send_failed"] = round(time.monotonic() - t1, 2)
                    log.info("URL send failed (%s), uploading instead", e.desc)
            if self.jobs.locked():
                await status(T.QUEUED)
            async with self.jobs:
                tm["queue"] = round(time.monotonic() - t0, 2)
                try:
                    path = await asyncio.to_thread(ytdl.download, p.url, opt.spec, tmpdir, progress, p.index,
                                                   self.cfg.cookie_file, info=info)
                except ytdl.TooBig:
                    return tm | {"link": "direct_link" if direct_url(opt) else "stream_link"}
                finally:
                    tm.update(dl_timings(progress))
                thumb = await self._thumb(thumb_task, tm)
                if diag and diag.get("diskread"):
                    tm["disk_read"] = await asyncio.to_thread(_time_read, path)
                progress["phase"] = "up"
                t1 = time.monotonic()
                if diag and diag.get("asdoc"):
                    extra = dict(extra, method="sendDocument")
                    meta = {}
                res = await self.tg.send_video(chat, path, is_file=True, caption=caption, thumb=thumb,
                                               progress=progress, **meta, **extra)
                tm["upload"] = round(time.monotonic() - t1, 2)
                if progress.get("t_up_done"):   # body acked by Telegram; the rest is its processing
                    tm["upload_body"] = round(progress["t_up_done"] - t1, 2)
                tm["up_attempts"], tm["cc"] = progress.get("attempts"), progress.get("cc")
                return self._done(tm, t0, key, res)
        finally:
            if ticker:
                ticker.cancel()
            if thumb_task and not thumb_task.done():
                thumb_task.cancel()
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _thumb(self, task, tm, wait: float = 6):
        if task is None:
            return None
        t = time.monotonic()
        try:
            thumb = await asyncio.wait_for(asyncio.shield(task), wait)
        except Exception:
            thumb = None
        tm["thumb"] = bool(thumb)
        tm["thumb_wait"] = round(time.monotonic() - t, 2)
        return thumb

    def _done(self, tm: dict, t0: float, key, res: dict) -> dict:
        self.remember(key, res)
        cur = _cgroup_mb()
        if cur:
            tm["mem_mb"] = cur
        tm.update(total=round(time.monotonic() - t0, 2), msg=res.get("message_id"))
        log.info("timing job %s", {k: v for k, v in tm.items() if k != "msg"})
        return tm

    def remember(self, key, res: dict):
        fid = (res.get("video") or res.get("document") or res.get("animation") or {}).get("file_id")
        if fid:
            self.file_ids.put(key, fid)

    async def tick(self, chat, mid, progress: dict):
        last = ""
        try:
            while True:
                await asyncio.sleep(EDIT_EVERY)
                if progress.get("phase") == "up":
                    total = progress.get("up_total")
                    pct = min(99.0, 100.0 * progress.get("up", 0) / total) if total else None
                    text = T.uploading(pct)
                else:
                    total = progress.get("total")
                    got = progress.get("done", 0) + progress.get("cur", 0)
                    pct = min(99.0, 100.0 * got / total) if total else None
                    text = T.downloading(pct, total)
                if text != last:
                    last = text
                    try:
                        await self.tg.edit(chat, mid, text)
                    except TgError:
                        pass
        except asyncio.CancelledError:
            pass

    async def send_link(self, chat, mid, p: Pending, opt: Option, mode: str):
        size_txt = T.human_size(opt.size, opt.approx)
        if mode == "direct_link":
            text, url = T.too_big_direct(size_txt), direct_url(opt)
        else:
            if not self.cfg.public_url:
                await self.tg.edit(chat, mid, "آدرس عمومی سرور (PUBLIC_URL) تنظیم نشده.")
                return
            token = sign(self.cfg.link_key, {"u": p.url, "f": opt.spec, "i": p.index, "t": p.title[:60]}, LINK_TTL)
            url = f"{self.cfg.public_url}/dl/{token}"
            text = T.too_big_stream(size_txt) + "\n" + url
        await self.tg.edit(chat, mid, f"{p.title[:200]}\n{text}", keyboard([[(T.BUTTON_DL, "url:" + url)]]))
