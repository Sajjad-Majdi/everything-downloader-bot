"""aiohttp web server: /health, /webhook, /dl/<token>."""
from __future__ import annotations

import asyncio
import collections
import hmac
import logging
import os
import time
from urllib.parse import urlparse

from aiohttp import web

from . import jsc, warp, ytdl
from . import texts as T
from .bot import Bot, Pending, auto_option, is_quick, quick_option
from .formats import build_options, entries_of
from .config import Config
from .stream import Streamer
from .tg import Telegram

log = logging.getLogger("server")
ICON = os.environ.get("BOT_ICON") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "assets", "bot_icon.jpg")
CFG = web.AppKey("cfg", Config)
BOT = web.AppKey("bot", Bot)
STREAMER = web.AppKey("streamer", Streamer)
TG = web.AppKey("tg", Telegram)
STATE = web.AppKey("state", dict)
STARTED = time.monotonic()
# Another copy of this bot (the old Singapore service) re-registers the webhook to itself
# whenever it wakes up, which silently moved every update to the slow instance (2026-10-26).
# The live instance checks the webhook every WEBHOOK_CHECK_EVERY seconds and takes it back.
WEBHOOK_CHECK_EVERY = float(os.environ.get("WEBHOOK_CHECK_EVERY", "60"))
LOG_RING: collections.deque[str] = collections.deque(maxlen=1500)


class RingHandler(logging.Handler):
    """Keeps the last log lines in RAM for /logs (Render's log view needs the dashboard)."""

    def emit(self, record):
        try:
            LOG_RING.append(self.format(record))
        except Exception:
            pass


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return ""


def mem_report() -> dict:
    """Container memory (cgroup), RSS per process name and where temp files live (Render free = 512 MB)."""
    cur = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    peak = _read("/sys/fs/cgroup/memory.peak") or _read("/sys/fs/cgroup/memory/memory.max_usage_in_bytes")
    lim = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    mb = lambda v: round(int(v) / 1048576) if v and v.isdigit() else v or None
    procs: dict[str, int] = {}
    for pid in (p for p in os.listdir("/proc") if p.isdigit()) if os.path.isdir("/proc") else []:
        st = _read(f"/proc/{pid}/status")
        name = next((ln.split()[1] for ln in st.splitlines() if ln.startswith("Name:")), None)
        rss = next((int(ln.split()[1]) for ln in st.splitlines() if ln.startswith("VmRSS:")), 0)
        if name:
            procs[name] = procs.get(name, 0) + rss // 1024
    import tempfile
    tmp = tempfile.gettempdir()
    fs = next((ln.split()[2] for ln in _read("/proc/mounts").splitlines() if ln.split()[1:2] == [tmp]), "overlay?")
    try:
        sv = os.statvfs(tmp)
        tmp_free = round(sv.f_bavail * sv.f_frsize / 1048576)
    except Exception:
        tmp_free = None
    return {"cgroup_mb": mb(cur), "peak_mb": mb(peak), "limit_mb": mb(lim), "rss_mb": procs, "tmp": tmp,
            "tmp_fs": fs, "tmp_free_mb": tmp_free}


async def health(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    if request.query.get("mem") == "1":
        return web.json_response(mem_report())
    return web.json_response({"ok": True, "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
                              "token": bool(request.app[CFG].bot_token), "up": int(time.monotonic() - STARTED),
                              "tasks": len(bot.tasks), "streams": request.app[STREAMER].active,
                              "webhook_ok": request.app[STATE].get("webhook_ok"),
                              "warp": warp.state["ready"], "warp_error": warp.state["error"],
                              "warp_restarts": warp.state["restarts"], "jsc": jsc.stats})


async def logs(request: web.Request) -> web.Response:
    """Owner-only: /logs?k=BENCH_KEY&n=200&grep=word. The bot token is masked."""
    cfg = request.app[CFG]
    if not cfg.bench_key or not hmac.compare_digest(request.query.get("k", ""), cfg.bench_key):
        return web.Response(status=403)
    n = min(int(request.query.get("n", "200")), LOG_RING.maxlen)
    grep = request.query.get("grep", "").lower()
    lines = [x for x in LOG_RING if not grep or grep in x.lower()][-n:]
    text = "\n".join(lines)
    if cfg.bot_token:
        text = text.replace(cfg.bot_token, "<token>")
    return web.Response(text=text + "\n")


async def webhook(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not cfg.bot_token or not cfg.webhook_secret or not hmac.compare_digest(got, cfg.webhook_secret):
        return web.Response(status=403)
    try:
        update = await request.json()
    except Exception:
        return web.Response(status=400)
    request.app[BOT].spawn(request.app[BOT].handle_update(update))
    return web.Response(text="ok")  # answer at once; work runs in the background


async def bench(request: web.Request) -> web.Response:
    """Owner-only speed test: /bench?k=BENCH_KEY&url=...&p=360&reuse=1

    Runs the real pipeline (extract, download, thumbnail, upload to the owner chat silently),
    deletes the test message at once, and returns the per-phase timings as JSON.
    """
    cfg, bot = request.app[CFG], request.app[BOT]
    if not cfg.bench_key or not hmac.compare_digest(request.query.get("k", ""), cfg.bench_key):
        return web.Response(status=403)
    if not cfg.bot_token or not cfg.owner_id:
        return web.json_response({"error": "no token"}, status=503)
    url = request.query.get("url", "")
    want = request.query.get("p", "")
    reuse = request.query.get("reuse", "1") == "1"
    extra, dbg = {}, None
    if request.query.get("pc"):
        extra["extractor_args"] = {"youtube": {"player_client": request.query["pc"].split(",")}}
    if request.query.get("warp") == "0":   # compare with the direct (blocked) route
        extra["proxy"] = ""
    if request.query.get("xa"):   # raw YouTube extractor args for experiments, JSON
        import json
        extra.setdefault("extractor_args", {}).setdefault("youtube", {}).update(json.loads(request.query["xa"]))
    if request.query.get("dbg") == "1":
        dbg = ytdl.DebugLog()
        extra |= {"logger": dbg, "verbose": True, "quiet": False}
    cookies = None if request.query.get("nocookies") == "1" else cfg.cookie_file
    t0 = time.monotonic()
    try:
        async with bot.extracts:
            fast = request.query.get("fast", "1") == "1" and not request.query.get("pc")
            info = await asyncio.to_thread(ytdl.extract, url, cookies, extra, fast)
    except Exception as e:
        return web.json_response({"error": f"extract: {e}"[:300], "log": dbg.lines[-80:] if dbg else None},
                                 status=502)
    extract_s = round(time.monotonic() - t0, 2)
    if request.query.get("only") == "extract":
        out = {"extract": extract_s, "formats": len(info.get("formats") or []),
               "client": info.get("_fast_client") or info.get("_clients") or "default",
               "warp": warp.state["ready"] and "proxy" not in extra, "jsc": dict(jsc.stats),
               "log": dbg.lines[-120:] if dbg else None}
        if request.query.get("fmts") == "1":
            out["list"] = [{k: f.get(k) for k in ("format_id", "ext", "vcodec", "acodec", "width", "height", "tbr",
                                                   "filesize", "filesize_approx", "protocol")}
                           | {"host": urlparse(f.get("url") or "").hostname}
                           for f in (info.get("formats") or [])]
            out["options"] = [(o.label, o.spec, o.size) for o in build_options(info)]
        return web.json_response(out)
    await bot.fill_sizes(info)
    index = None
    entries = entries_of(info)
    if entries:
        index, info = next(((i + 1, e) for i, e in enumerate(entries) if build_options(e)), (None, info))
    opts = build_options(info)
    if not opts:
        return web.json_response({"error": "no video formats", "extract": extract_s}, status=422)
    # same as a real message: one quality goes out at once; else the menu (menu_s = time until the
    # buttons show) and p= plays the tap, auto_option() when p is not given
    quick = is_quick(opts)
    menu_s = round(time.monotonic() - t0, 2)
    if request.query.get("only") == "menu":
        return web.json_response({"extract": extract_s, "menu": menu_s, "quick": quick,
                                  "options": [(o.label, T.human_size(o.size, o.approx)) for o in opts]})
    opt = next((o for o in opts if want and o.label.startswith(want)), None) or (
        quick_option(opts) if quick else auto_option(opts))
    if request.query.get("spec"):   # diagnostics: force an exact yt-dlp format spec
        from dataclasses import replace
        opt = replace(opt, spec=request.query["spec"], size=None)
    p = Pending(info.get("webpage_url") or url, info.get("title") or "", info.get("duration"), opts,
                index=index, info=info)
    if request.query.get("nothumb") == "1":
        p.info = dict(p.info, thumbnails=[], thumbnail=None, extractor_key="x")
    if request.query.get("nometa") == "1":
        p.duration = None
        opt.video = {k: v for k, v in opt.video.items() if k not in ("width", "height")}
    try:
        tm = await bot.run_job(cfg.owner_id, None, p, opt, reuse=reuse, quiet=True,
                               diag={"diskread": request.query.get("diskread") == "1",
                                     "asdoc": request.query.get("asdoc") == "1"})
    except Exception as e:
        return web.json_response({"error": f"job: {type(e).__name__}: {e}"[:300], "extract": extract_s}, status=500)
    if tm.get("msg"):
        await bot.tg.delete(cfg.owner_id, tm.pop("msg"))
    out = {"extract": extract_s, "menu": menu_s, "client": info.get("_fast_client") or info.get("_clients") or "default",
           "quick": quick, "label": opt.label, "options": [o.label for o in opts], **tm}
    log.info("bench %s", out)
    return web.json_response(out)


async def netbench(request: web.Request) -> web.Response:
    """Owner-only egress test: /netbench?k=..&mb=20&target=tg|cf

    tg: POST the bytes as a sendDocument to chat_id 0 (Telegram reads the body, then answers
    "chat not found"; nothing is sent to anyone). cf: POST to Cloudflare's speed-test sink.
    Tells whether a slow upload is Telegram's intake or Render's egress.
    """
    cfg, bot = request.app[CFG], request.app[BOT]
    if not cfg.bench_key or not hmac.compare_digest(request.query.get("k", ""), cfg.bench_key):
        return web.Response(status=403)
    size = min(int(request.query.get("mb", "20")), 45) * 1024 * 1024
    target = request.query.get("target", "tg")
    sent = 0

    async def body():
        nonlocal sent
        chunk = os.urandom(1 << 20)
        while sent < size:
            sent += len(chunk)
            yield chunk

    import aiohttp

    from .tg import upload_socket
    holder: dict = {}
    fresh = request.query.get("fresh", "1") == "1"
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
        force_close=True, socket_factory=lambda ai: upload_socket(ai, holder))) if fresh else await bot.tg.session()
    t0 = time.monotonic()
    try:
        if target == "cf":
            async with session.post("https://speed.cloudflare.com/__up", data=body(),
                                    headers={"Content-Type": "application/octet-stream"}) as r:
                status = r.status
                await r.read()
        else:
            form = __import__("aiohttp").FormData()
            form.add_field("chat_id", "0")
            form.add_field("document", body(), filename="x.bin", content_type="application/octet-stream")
            async with session.post(f"{cfg.api_base}/bot{cfg.bot_token}/sendDocument", data=form) as r:
                status = r.status
                await r.read()
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"[:200], "sent": sent,
                                  "secs": round(time.monotonic() - t0, 2)})
    finally:
        if fresh:
            await session.close()
    secs = round(time.monotonic() - t0, 2)
    return web.json_response({"target": target, "cc": holder.get("cc"), "mb": round(sent / 1048576, 1), "secs": secs,
                              "mb_s": round(sent / 1048576 / max(secs, 0.01), 2), "status": status})


WARMUP_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19 s public clip


WARMUP_EVERY = float(os.environ.get("WARMUP_EVERY", str(3 * 3600)))


async def boot(app: web.Application):
    """WARP tunnel for YouTube first (YouTube blocks Render's IP), then the warm-up extraction.
    The warm-up repeats every WARMUP_EVERY s, so a new YouTube player version (its one-time ~15 s
    preprocessing on 0.1 CPU) is met by the bot, not by Sajjad's next link."""
    await warp.start()
    app[STATE]["warp_task"] = asyncio.create_task(warp.keep_alive())   # keep a reference (no GC)
    if not (app[CFG].bot_token and os.environ.get("WARMUP", "1") == "1"):
        return
    while True:
        await warm_up(app)
        await asyncio.sleep(WARMUP_EVERY)


async def warm_up(app: web.Application):
    """One YouTube extraction at boot fills the yt-dlp cache (player JS, solver), so the
    first real link does not pay for it."""
    await asyncio.sleep(1)
    t0 = time.monotonic()
    try:
        async with app[BOT].extracts:
            await asyncio.to_thread(ytdl.extract, WARMUP_URL, app[CFG].cookie_file, None, False)
        log.info("timing warm-up extract=%.1fs", time.monotonic() - t0)
    except Exception as e:
        log.warning("warm-up failed after %.1fs: %s", time.monotonic() - t0, e)


async def register_webhook(cfg: Config, tg: Telegram):
    await tg.call("setWebhook", url=f"{cfg.public_url}/webhook", secret_token=cfg.webhook_secret,
                  allowed_updates=["message", "callback_query"], max_connections=8)


async def set_webhook(app: web.Application):
    cfg, tg = app[CFG], app[TG]
    if not cfg.bot_token:
        log.warning("TELEGRAM_BOT_TOKEN is not set; webhook not registered")
        return
    if not cfg.public_url:
        log.warning("PUBLIC_URL / RENDER_EXTERNAL_URL missing; webhook not registered")
        return
    for attempt in range(5):
        try:
            await register_webhook(cfg, tg)
            await tg.call("setMyCommands", commands=[{"command": "start", "description": "راهنما"}])
            log.info("webhook set to %s/webhook", cfg.public_url)
            app[STATE]["webhook_ok"] = True
            break
        except Exception as e:
            log.warning("setWebhook attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(5 * (attempt + 1))
    await ensure_photo(tg)


async def check_webhook(app: web.Application) -> bool:
    """One check: True when the webhook already points here; re-registers it otherwise."""
    cfg, tg = app[CFG], app[TG]
    want = f"{cfg.public_url}/webhook"
    info = await tg.call("getWebhookInfo")
    if info.get("last_error_message"):
        app[STATE]["webhook_error"] = info.get("last_error_message")
    if info.get("url") == want:
        app[STATE]["webhook_ok"] = True
        return True
    app[STATE]["webhook_ok"] = False
    log.warning("webhook points to %r (pending %s); taking it back", info.get("url"), info.get("pending_update_count"))
    await register_webhook(cfg, tg)
    app[STATE]["webhook_ok"] = True
    app[STATE]["webhook_taken_back"] = app[STATE].get("webhook_taken_back", 0) + 1
    return False


async def webhook_guard(app: web.Application):
    while True:
        await asyncio.sleep(WEBHOOK_CHECK_EVERY)
        try:
            await check_webhook(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("webhook check failed: %s", e)


async def ensure_photo(tg: Telegram, icon: str = ICON):
    """Give the bot its profile photo once; never blocks or breaks startup."""
    try:
        me = await tg.call("getMe")
        photos = await tg.call("getUserProfilePhotos", user_id=me["id"], limit=1)
        if photos.get("total_count", 0) > 0:
            log.info("bot photo already set")
            return
        if not os.path.exists(icon):
            log.warning("bot icon missing at %s", icon)
            return
        await tg.call_with_file("setMyProfilePhoto", "pic", icon, "image/jpeg",
                                photo={"type": "static", "photo": "attach://pic"})
        log.info("bot profile photo set")
    except Exception as e:
        log.warning("bot photo step failed: %s", e)


def make_app(cfg: Config | None = None, tg: Telegram | None = None) -> web.Application:
    cfg = cfg or Config.from_env()
    tg = tg or Telegram(cfg.bot_token, cfg.api_base)
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app[CFG] = cfg
    app[TG] = tg
    app[BOT] = Bot(cfg, tg)
    app[STREAMER] = Streamer(cfg)
    app[STATE] = {}
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook", webhook)
    app.router.add_get("/dl/{token}", app[STREAMER].handle)
    app.router.add_get("/_src/{sid}", app[STREAMER].relay)   # local only: ranged googlevideo relay for ffmpeg
    app.router.add_get("/bench", bench)
    app.router.add_get("/netbench", netbench)
    app.router.add_get("/logs", logs)
    background: list[asyncio.Task] = []

    async def on_startup(app):
        app[BOT].spawn(set_webhook(app))
        if cfg.bot_token and cfg.public_url and os.environ.get("WEBHOOK_GUARD", "1") == "1":
            background.append(asyncio.create_task(webhook_guard(app)))
        background.append(asyncio.create_task(boot(app)))

    async def on_cleanup(app):
        for t in background:
            t.cancel()
        warp.stop()
        await app[STREAMER].close()
        await app[TG].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    ring = RingHandler()
    ring.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(ring)
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), access_log=None)


if __name__ == "__main__":
    main()
