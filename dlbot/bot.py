"""Update handlers: link -> quality menu -> video, or a link when too big."""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from . import texts as T
from . import ytdl
from .cache import LRU
from .config import LINK_TTL, SAFE_UPLOAD_LIMIT, Config
from .formats import (Option, build_options, direct_url, entries_of, entry_is_resolved, plan)
from .signing import sign
from .tg import Telegram, TgError, keyboard

log = logging.getLogger("bot")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
EDIT_EVERY = 3.0


@dataclass
class Pending:
    url: str
    title: str = ""
    duration: float | None = None
    options: list[Option] = field(default_factory=list)
    index: int | None = None          # 1-based item inside a carousel
    entries: list[dict] = field(default_factory=list)  # playlist / carousel menu
    carousel: bool = False


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


def option_rows(rid: str, options: list[Option]) -> list[list[tuple[str, str]]]:
    buttons = [(f"{o.label} · {T.human_size(o.size, o.approx)}", f"q:{rid}:{i}") for i, o in enumerate(options)]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


class Bot:
    def __init__(self, cfg: Config, tg: Telegram):
        self.cfg = cfg
        self.tg = tg
        self.pending = LRU(maxsize=64, ttl=6 * 3600)
        self.file_ids = LRU(maxsize=300)
        self.jobs = asyncio.Semaphore(cfg.max_jobs)
        self.extracts = asyncio.Semaphore(2)
        self._ids = itertools.count(1)
        self.tasks: set[asyncio.Task] = set()

    # entry point -----------------------------------------------------------
    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def handle_update(self, upd: dict):
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
        try:
            async with self.extracts:
                info = await asyncio.to_thread(ytdl.extract, url, self.cfg.cookie_file)
        except Exception as e:
            log.warning("extract failed for %s: %s", url, e)
            await self.tg.edit(chat, mid, T.error_text(e))
            return
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
                    return await self.show_menu(chat, mid, Pending(url, e.get("title") or info.get("title") or "",
                                                                    e.get("duration"), build_options(e), index=idx))
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
        await self.show_menu(chat, mid, Pending(info.get("webpage_url") or url, info.get("title") or "",
                                                info.get("duration"), options), rid)

    async def show_menu(self, chat, mid, p: Pending, rid: str | None = None):
        rid = rid or self.new_id()
        self.pending.put(rid, p)
        head = p.title[:200]
        dur = T.human_duration(p.duration)
        text = f"{head}\n{('مدت: ' + dur) if dur else ''}\n{T.CHOOSE}".replace("\n\n", "\n")
        await self.tg.edit(chat, mid, text, keyboard(option_rows(rid, p.options)))

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
        if kind == "q":
            opt = p.options[int(parts[2])]
            await self.deliver(chat, mid, p, opt)
        elif kind == "e":
            e = p.entries[int(parts[2])]
            if p.carousel:
                await self.show_menu(chat, mid, Pending(p.url, e.get("title") or p.title, e.get("duration"),
                                                        build_options(e), index=e["_index"]))
            else:
                await self.tg.edit(chat, mid, T.CHECKING)
                await self.analyze(chat, mid, e.get("url") or e.get("webpage_url"))
        elif kind == "a" and p.carousel:
            await self.tg.edit(chat, mid, T.PREPARING)
            for e in p.entries:
                opts = build_options(e)
                sub = Pending(p.url, e.get("title") or p.title, e.get("duration"), opts, index=e["_index"])
                status = await self.tg.send(chat, T.PREPARING)
                await self.deliver(chat, status["message_id"], sub, auto_option(opts))
            await self.tg.delete(chat, mid)

    # delivery ------------------------------------------------------------------
    async def deliver(self, chat: int, mid: int, p: Pending, opt: Option):
        key = (p.url, p.index, opt.spec)
        caption = p.title[:900]
        fid = self.file_ids.get(key)
        if fid:
            try:
                await self.tg.call("sendVideo", chat_id=chat, video=fid, caption=caption, supports_streaming=True)
                await self.tg.delete(chat, mid)
                return
            except TgError:
                log.info("cached file_id failed, downloading again")
        mode = plan(opt)
        if mode == "url_send":
            await self.tg.edit(chat, mid, T.UPLOADING)
            try:
                res = await self.tg.call("sendVideo", chat_id=chat, video=direct_url(opt), caption=caption,
                                         supports_streaming=True)
                self.remember(key, res)
                await self.tg.delete(chat, mid)
                return
            except TgError as e:
                log.info("URL send failed (%s), uploading instead", e.desc)
                mode = "upload"
        if mode == "upload":
            try:
                await self.upload(chat, mid, p, opt, key, caption)
                return
            except ytdl.TooBig:
                mode = "direct_link" if direct_url(opt) else "stream_link"
            except Exception as e:
                log.exception("download failed")
                await self.tg.edit(chat, mid, T.error_text(e))
                return
        await self.send_link(chat, mid, p, opt, mode)

    def remember(self, key, res: dict):
        fid = (res.get("video") or res.get("document") or res.get("animation") or {}).get("file_id")
        if fid:
            self.file_ids.put(key, fid)

    async def upload(self, chat, mid, p: Pending, opt: Option, key, caption):
        if self.jobs.locked():
            await self.tg.edit(chat, mid, T.QUEUED)
        async with self.jobs:
            tmpdir = tempfile.mkdtemp(prefix="dl-")
            progress = {"done": 0, "cur": 0, "total": opt.size}
            ticker = asyncio.create_task(self.tick(chat, mid, progress))
            try:
                path = await asyncio.to_thread(ytdl.download, p.url, opt.spec, tmpdir, progress, p.index,
                                               self.cfg.cookie_file)
                ticker.cancel()
                await self.tg.edit(chat, mid, T.UPLOADING)
                v = opt.video
                res = await self.tg.send_video_file(
                    chat, path, caption=caption, duration=int(p.duration) if p.duration else None,
                    width=v.get("width"), height=v.get("height"))
                self.remember(key, res)
                await self.tg.delete(chat, mid)
            finally:
                ticker.cancel()
                shutil.rmtree(tmpdir, ignore_errors=True)

    async def tick(self, chat, mid, progress: dict):
        last = ""
        try:
            while True:
                await asyncio.sleep(EDIT_EVERY)
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
