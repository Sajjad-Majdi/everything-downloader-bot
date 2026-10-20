"""aiohttp web server: /health, /webhook, /dl/<token>."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time

from aiohttp import web

from . import ytdl
from .bot import Bot, Pending, auto_option
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


async def health(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    return web.json_response({"ok": True, "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
                              "token": bool(request.app[CFG].bot_token),
                              "tasks": len(bot.tasks), "streams": request.app[STREAMER].active})


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
    if request.query.get("dbg") == "1":
        dbg = ytdl.DebugLog()
        extra |= {"logger": dbg, "verbose": True, "quiet": False}
    t0 = time.monotonic()
    try:
        async with bot.extracts:
            fast = request.query.get("fast", "1") == "1" and not request.query.get("pc")
            info = await asyncio.to_thread(ytdl.extract, url, cfg.cookie_file, extra, fast)
    except Exception as e:
        return web.json_response({"error": f"extract: {e}"[:300], "log": dbg.lines[-60:] if dbg else None},
                                 status=502)
    extract_s = round(time.monotonic() - t0, 2)
    if request.query.get("only") == "extract":
        return web.json_response({"extract": extract_s, "formats": len(info.get("formats") or []),
                                  "client": info.get("_fast_client") or "default",
                                  "log": dbg.lines[-120:] if dbg else None})
    index = None
    entries = entries_of(info)
    if entries:
        index, info = next(((i + 1, e) for i, e in enumerate(entries) if build_options(e)), (None, info))
    opts = build_options(info)
    if not opts:
        return web.json_response({"error": "no video formats", "extract": extract_s}, status=422)
    opt = next((o for o in opts if want and o.label.startswith(want)), None) or auto_option(opts)
    p = Pending(info.get("webpage_url") or url, info.get("title") or "", info.get("duration"), opts,
                index=index, info=info)
    try:
        tm = await bot.run_job(cfg.owner_id, None, p, opt, reuse=reuse, quiet=True)
    except Exception as e:
        return web.json_response({"error": f"job: {type(e).__name__}: {e}"[:300], "extract": extract_s}, status=500)
    if tm.get("msg"):
        await bot.tg.delete(cfg.owner_id, tm.pop("msg"))
    out = {"extract": extract_s, "client": info.get("_fast_client") or "default", "label": opt.label, "options": [o.label for o in opts], **tm}
    log.info("bench %s", out)
    return web.json_response(out)


WARMUP_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19 s public clip


async def warm_up(app: web.Application):
    """One YouTube extraction at boot fills the yt-dlp cache (player JS, solver), so the
    first real link does not pay for it."""
    await asyncio.sleep(3)
    t0 = time.monotonic()
    try:
        async with app[BOT].extracts:
            await asyncio.to_thread(ytdl.extract, WARMUP_URL, app[CFG].cookie_file, None, False)
        log.info("timing warm-up extract=%.1fs", time.monotonic() - t0)
    except Exception as e:
        log.warning("warm-up failed after %.1fs: %s", time.monotonic() - t0, e)


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
            await tg.call("setWebhook", url=f"{cfg.public_url}/webhook", secret_token=cfg.webhook_secret,
                          allowed_updates=["message", "callback_query"], max_connections=4)
            await tg.call("setMyCommands", commands=[{"command": "start", "description": "راهنما"}])
            log.info("webhook set to %s/webhook", cfg.public_url)
            break
        except Exception as e:
            log.warning("setWebhook attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(5 * (attempt + 1))
    await ensure_photo(tg)


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
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook", webhook)
    app.router.add_get("/dl/{token}", app[STREAMER].handle)
    app.router.add_get("/bench", bench)

    async def on_startup(app):
        app[BOT].spawn(set_webhook(app))
        if cfg.bot_token and os.environ.get("WARMUP", "1") == "1":
            app[BOT].spawn(warm_up(app))

    async def on_cleanup(app):
        await app[STREAMER].close()
        await app[TG].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), access_log=None)


if __name__ == "__main__":
    main()
