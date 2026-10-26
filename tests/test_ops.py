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


def test_warp_proxy_only_for_youtube_when_ready(monkeypatch):
    from dlbot import warp, ytdl
    monkeypatch.setitem(warp.state, "ready", False)
    assert warp.proxy_for("https://www.youtube.com/watch?v=a") is None
    monkeypatch.setitem(warp.state, "ready", True)
    for u in ("https://www.youtube.com/watch?v=a", "https://youtu.be/a", "https://m.youtube.com/shorts/a",
              "https://rr1---sn-x.googlevideo.com/videoplayback?x=1"):
        assert warp.proxy_for(u) == warp.PROXY_URL, u
    for u in ("https://www.instagram.com/reel/a/", "https://x.com/a/status/1", "https://evil.com/youtube.com/x",
              "https://youtube.com.evil.com/x", None):
        assert warp.proxy_for(u) is None, u
    assert ytdl.base_opts(None, "https://youtu.be/a")["proxy"] == warp.PROXY_URL
    assert "proxy" not in ytdl.base_opts(None, "https://www.instagram.com/reel/a/")


def test_warp_config_adds_http_proxy_section():
    from dlbot import warp
    prof = "[Interface]\nPrivateKey = k\nAddress = 172.16.0.2/32\n\n[Peer]\nPublicKey = p\nEndpoint = engage.cloudflareclient.com:2408\n"
    conf = warp.write_config(prof, 40001)
    assert conf.startswith("[Interface]") and conf.rstrip().endswith("[http]\nBindAddress = 127.0.0.1:40001")


async def test_warp_start_without_binaries_is_harmless(monkeypatch):
    from dlbot import warp
    monkeypatch.setenv("YT_PROXY", "warp")
    monkeypatch.setattr(warp.shutil, "which", lambda name: None)
    assert await warp.start() is False
    assert warp.state["ready"] is False and "not installed" in warp.state["error"]


def test_ffmpeg_uses_proxy_per_input():
    from dlbot.stream import ffmpeg_cmd
    cmd = ffmpeg_cmd([{"url": "https://a"}, {"url": "https://b"}], "http://127.0.0.1:40001")
    assert cmd.count("-http_proxy") == 2 and cmd.index("-http_proxy") < cmd.index("-i")


async def test_fill_sizes_heads_open_cdn_formats(aiohttp_client, monkeypatch):
    import aiohttp
    from aiohttp import web

    from dlbot import bot as botmod
    from dlbot import formats

    async def head(request):
        return web.Response(body=b"x" * (1000 if request.path == "/big.mp4" else 10), content_type="video/mp4")

    origin = web.Application()
    origin.router.add_route("*", "/{name}", head)
    oc = await aiohttp_client(origin)
    monkeypatch.setattr(formats, "DIRECT_HOSTS", ("127.0.0.1",))

    class SessTG(FakeTG):
        def __init__(self):
            super().__init__()
            self.s = aiohttp.ClientSession()

        async def session(self):
            return self.s

    tg = SessTG()
    b = botmod.Bot(cfg(), tg)
    info = {"formats": [{"format_id": n, "ext": "mp4", "protocol": "http", "url": str(oc.make_url(f"/{n}.mp4"))}
                        for n in ("small", "big")]}
    try:
        await b.fill_sizes(info)
    finally:
        await tg.s.close()
    assert [f["filesize"] for f in info["formats"]] == [10, 1000]
    assert formats.build_options(info)[0].spec == "big"
