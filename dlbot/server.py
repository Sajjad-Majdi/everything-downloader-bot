"""aiohttp web server: /health, /webhook, /dl/<token>."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os

from aiohttp import web

from .bot import Bot
from .config import Config
from .stream import Streamer
from .tg import Telegram

log = logging.getLogger("server")
CFG = web.AppKey("cfg", Config)
BOT = web.AppKey("bot", Bot)
STREAMER = web.AppKey("streamer", Streamer)
TG = web.AppKey("tg", Telegram)


async def health(request: web.Request) -> web.Response:
    bot = request.app[BOT]
    return web.json_response({"ok": True, "token": bool(request.app[CFG].bot_token),
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
            return
        except Exception as e:
            log.warning("setWebhook attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(5 * (attempt + 1))


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

    async def on_startup(app):
        app[BOT].spawn(set_webhook(app))

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
