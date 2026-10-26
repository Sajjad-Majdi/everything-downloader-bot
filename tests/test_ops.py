"""Operations: webhook guard (another instance stole the webhook) and the /logs view."""
import logging

from test_handlers import FakeTG, OWNER, cfg

from dlbot import server
from dlbot.server import STATE, check_webhook, make_app


class HookTG(FakeTG):
    def __init__(self, url):
        super().__init__()
        self.url = url

    async def call(self, method, **p):
        self.calls.append((method, p))
        if method == "getWebhookInfo":
            return {"url": self.url, "pending_update_count": 3}
        if method == "setWebhook":
            self.url = p["url"]
        return True


async def test_webhook_taken_back_from_other_instance():
    tg = HookTG("https://everything-downloader-1r7j.onrender.com/webhook")
    app = make_app(cfg(), tg)
    assert await check_webhook(app) is False
    assert tg.url == "https://bot.onrender.com/webhook"
    sets = [p for m, p in tg.calls if m == "setWebhook"]
    assert sets and sets[0]["secret_token"] == "s" * 48
    assert app[STATE]["webhook_ok"] is True and app[STATE]["webhook_taken_back"] == 1


async def test_webhook_left_alone_when_correct():
    tg = HookTG("https://bot.onrender.com/webhook")
    app = make_app(cfg(), tg)
    assert await check_webhook(app) is True
    assert not [m for m, _ in tg.calls if m == "setWebhook"]


async def test_logs_need_key_and_mask_token(aiohttp_client):
    c = cfg()
    c.owner_id, c.bench_key, c.bot_token = OWNER, "bk", "123:SECRET"
    client = await aiohttp_client(make_app(c, FakeTG()))
    server.LOG_RING.clear()
    h = server.RingHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "call to /bot123:SECRET/sendVideo failed", None, None)
    h.emit(rec)
    h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "timing job ok", None, None))
    assert (await client.get("/logs")).status == 403
    text = await (await client.get("/logs?k=bk")).text()
    assert "SECRET" not in text and "<token>" in text and "timing job ok" in text
    text = await (await client.get("/logs?k=bk&grep=timing")).text()
    assert text.strip() == "timing job ok"
