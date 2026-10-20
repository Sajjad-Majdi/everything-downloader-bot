import os

import pytest
from conftest import MB, insta_info, yt_info

from dlbot import bot as botmod
from dlbot import texts as T
from dlbot import ytdl
from dlbot.config import Config
from dlbot.server import BOT, make_app
from dlbot.signing import verify
from dlbot.tg import TgError

OWNER = 394891587


class FakeTG:
    def __init__(self, fail_url_send=False):
        self.calls = []
        self.uploads = []
        self.fail_url_send = fail_url_send
        self.n = 100

    async def call(self, method, **p):
        self.calls.append((method, p))
        if method == "sendVideo":
            if self.fail_url_send and str(p.get("video", "")).startswith("http"):
                raise TgError(method, "failed to get HTTP URL content")
            return {"message_id": 1, "video": {"file_id": "FID-" + str(p["video"])[-6:]}}
        return True

    async def send(self, chat, text, reply_markup=None, reply_to=None):
        self.n += 1
        self.calls.append(("send", {"text": text, "markup": reply_markup}))
        return {"message_id": self.n}

    async def edit(self, chat, mid, text, reply_markup=None):
        self.calls.append(("edit", {"text": text, "markup": reply_markup, "mid": mid}))

    async def delete(self, chat, mid):
        self.calls.append(("delete", {"mid": mid}))

    async def answer(self, cid, text=None):
        self.calls.append(("answer", {}))

    async def send_video_file(self, chat, path, caption="", **extra):
        assert os.path.exists(path)
        self.uploads.append((path, os.path.getsize(path)))
        return {"video": {"file_id": "UPLOADED"}}

    async def close(self):
        pass

    def texts(self):
        return [p.get("text") for m, p in self.calls if m in ("send", "edit")]

    def last_markup(self):
        return [p["markup"] for m, p in self.calls if m in ("send", "edit") and p.get("markup")][-1]


def cfg():
    return Config(bot_token="1:x", public_url="https://bot.onrender.com", webhook_secret="s" * 48,
                  link_key=b"key", allowed_ids={OWNER})


def msg(text, uid=OWNER):
    return {"message": {"message_id": 5, "chat": {"id": uid}, "from": {"id": uid}, "text": text}}


def cb(data, uid=OWNER):
    return {"callback_query": {"id": "c", "from": {"id": uid}, "data": data,
                               "message": {"message_id": 101, "chat": {"id": uid}}}}


@pytest.fixture
def setup(monkeypatch):
    tg = FakeTG()
    b = botmod.Bot(cfg(), tg)
    monkeypatch.setattr(botmod, "EDIT_EVERY", 0.01)
    return b, tg


def data_of(markup, label_start):
    for row in markup["inline_keyboard"]:
        for btn in row:
            if btn["text"].startswith(label_start):
                return btn.get("callback_data") or btn.get("url")
    raise AssertionError(f"no button {label_start}: {markup}")


async def test_stranger_is_refused(setup):
    b, tg = setup
    await b.handle_update(msg("https://youtu.be/abc", uid=42))
    assert tg.texts() == [T.NOT_ALLOWED]


async def test_start_and_no_link(setup):
    b, tg = setup
    await b.handle_update(msg("/start"))
    await b.handle_update(msg("hello"))
    assert tg.texts() == [T.START, T.NO_LINK]


async def test_menu_then_upload_then_cache(setup, monkeypatch):
    b, tg = setup
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: yt_info())
    seen = {}

    def fake_download(url, spec, tmpdir, progress, index=None, cookie_file=None, limit=0):
        seen.update(url=url, spec=spec, tmpdir=tmpdir)
        progress["cur"] = 5 * MB
        p = os.path.join(tmpdir, "video.mp4")
        with open(p, "wb") as fh:
            fh.write(b"\0" * 1000)
        return p

    monkeypatch.setattr(ytdl, "download", fake_download)
    await b.handle_update(msg("look https://www.youtube.com/watch?v=abc please"))
    assert T.CHOOSE in tg.texts()[-1]
    markup = tg.last_markup()
    data = data_of(markup, "360p")
    await b.handle_update(cb(data))
    assert seen["spec"] == "18" and seen["url"] == "https://www.youtube.com/watch?v=abc"
    assert tg.uploads and not os.path.exists(seen["tmpdir"])   # temp dir removed at once
    # same video again: instant file_id, no new download
    seen.clear()
    await b.handle_update(cb(data))
    assert not seen
    assert ("sendVideo", {"chat_id": OWNER, "video": "UPLOADED", "caption": "Test video",
                          "supports_streaming": True}) in tg.calls


async def test_big_youtube_gives_signed_stream_link(setup, monkeypatch):
    b, tg = setup
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: yt_info())
    await b.handle_update(msg("https://youtu.be/abc"))
    await b.handle_update(cb(data_of(tg.last_markup(), "1080p")))
    url = data_of(tg.last_markup(), T.BUTTON_DL)
    assert url.startswith("https://bot.onrender.com/dl/")
    body = verify(b"key", url.rsplit("/", 1)[1])
    assert body["f"] == "137+140" and body["u"] == "https://www.youtube.com/watch?v=abc"


async def test_unknown_size_too_big_falls_back_to_link(setup, monkeypatch):
    b, tg = setup
    info = yt_info()
    for f in info["formats"]:
        f.pop("filesize", None)
        f.pop("filesize_approx", None)
        f.pop("tbr", None)
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: info)
    dirs = []

    def too_big(url, spec, tmpdir, progress, index=None, cookie_file=None, limit=0):
        dirs.append(tmpdir)
        raise ytdl.TooBig()

    monkeypatch.setattr(ytdl, "download", too_big)
    await b.handle_update(msg("https://youtu.be/abc"))
    await b.handle_update(cb(data_of(tg.last_markup(), "720p")))
    assert data_of(tg.last_markup(), T.BUTTON_DL).startswith("https://bot.onrender.com/dl/")
    assert dirs and not os.path.exists(dirs[0])


async def test_instagram_small_is_sent_by_url(setup, monkeypatch):
    b, tg = setup
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: insta_info())
    await b.handle_update(msg("https://www.instagram.com/reel/C1/"))
    await b.handle_update(cb(data_of(tg.last_markup(), "720p")))
    sent = [p for m, p in tg.calls if m == "sendVideo"]
    assert sent and sent[0]["video"].startswith("https://scontent-fra.cdninstagram.com/")
    assert not tg.uploads


async def test_url_send_failure_falls_back_to_upload(monkeypatch):
    tg = FakeTG(fail_url_send=True)
    b = botmod.Bot(cfg(), tg)
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: insta_info())

    def dl(url, spec, tmpdir, progress, index=None, cookie_file=None, limit=0):
        p = os.path.join(tmpdir, "video.mp4")
        open(p, "wb").close()
        return p

    monkeypatch.setattr(ytdl, "download", dl)
    await b.handle_update(msg("https://www.instagram.com/reel/C1/"))
    await b.handle_update(cb(data_of(tg.last_markup(), "720p")))
    assert len(tg.uploads) == 1


async def test_carousel_menu_and_all(setup, monkeypatch):
    b, tg = setup
    item1, item2 = insta_info(), insta_info()
    item2["formats"][2]["url"] = "https://scontent.cdninstagram.com/v/two.mp4"
    image = {"id": "img", "url": "https://scontent.cdninstagram.com/i.jpg", "ext": "jpg", "vcodec": "none",
             "acodec": "none"}
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None: {"_type": "playlist", "title": "Post",
                                                              "entries": [item1, image, item2]})
    await b.handle_update(msg("https://www.instagram.com/p/X/"))
    assert tg.texts()[-1] == T.CAROUSEL.format(n=T.fa(2))
    await b.handle_update(cb(data_of(tg.last_markup(), T.ALL_ITEMS)))
    sent = [p["video"] for m, p in tg.calls if m == "sendVideo"]
    assert len(sent) == 2 and sent[1].endswith("two.mp4")


async def test_youtube_playlist_offers_items(setup, monkeypatch):
    b, tg = setup
    calls = []

    def extract(url, c=None):
        calls.append(url)
        if "playlist" in url:
            return {"_type": "playlist", "title": "PL", "entries": [
                {"_type": "url", "url": f"https://www.youtube.com/watch?v={i}", "title": f"Clip {i}"} for i in range(9)]}
        return yt_info()

    monkeypatch.setattr(ytdl, "extract", extract)
    await b.handle_update(msg("https://www.youtube.com/playlist?list=PL1"))
    assert tg.texts()[-1] == T.PLAYLIST.format(n=T.fa(9))
    assert sum(len(r) for r in tg.last_markup()["inline_keyboard"]) == 6
    await b.handle_update(cb(data_of(tg.last_markup(), "Clip 2")))
    assert calls[-1] == "https://www.youtube.com/watch?v=2"
    assert T.CHOOSE in tg.texts()[-1]


async def test_extract_error_is_persian(setup, monkeypatch):
    b, tg = setup

    def boom(url, c=None):
        raise RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot")

    monkeypatch.setattr(ytdl, "extract", boom)
    await b.handle_update(msg("https://youtu.be/abc"))
    assert "کوکی" in tg.texts()[-1]


async def test_old_button_says_expired(setup):
    b, tg = setup
    await b.handle_update(cb("q:zzz:0"))
    assert tg.texts()[-1] == T.EXPIRED


# ---- web server ----------------------------------------------------------------
async def test_webhook_secret_and_health(aiohttp_client):
    tg = FakeTG()
    app = make_app(cfg(), tg)
    client = await aiohttp_client(app)
    r = await client.get("/health")
    assert r.status == 200 and (await r.json())["ok"]
    r = await client.post("/webhook", json=msg("/start"))
    assert r.status == 403
    r = await client.post("/webhook", json=msg("/start"), headers={"X-Telegram-Bot-Api-Secret-Token": "bad"})
    assert r.status == 403
    r = await client.post("/webhook", json=msg("/start"), headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 48})
    assert r.status == 200
    for t in list(app[BOT].tasks):
        await t
    assert T.START in tg.texts()


async def test_dl_rejects_bad_and_expired_tokens(aiohttp_client):
    from dlbot.signing import sign
    app = make_app(cfg(), FakeTG())
    client = await aiohttp_client(app)
    assert (await client.get("/dl/abc.def")).status == 403
    old = sign(b"key", {"u": "x", "f": "18"}, -10)
    assert (await client.get(f"/dl/{old}")).status == 410


async def test_dl_proxy_streams_with_range(aiohttp_client, monkeypatch):
    from aiohttp import web

    from dlbot.signing import sign

    payload = bytes(range(256)) * 400

    async def origin(request):
        rng = request.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return web.Response(status=206, body=payload[start:], headers={
                "Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}", "Content-Type": "video/mp4"})
        return web.Response(body=payload, content_type="video/mp4")

    src = web.Application()
    src.router.add_get("/v.mp4", origin)
    src_client = await aiohttp_client(src)
    src_url = str(src_client.make_url("/v.mp4"))
    monkeypatch.setattr(ytdl, "resolve", lambda u, f, i=None, c=None: {"url": src_url, "ext": "mp4",
                                                                        "title": "Clip", "http_headers": {}})
    client = await aiohttp_client(make_app(cfg(), FakeTG()))
    tok = sign(b"key", {"u": "https://youtu.be/abc", "f": "18", "t": "کلیپ"}, 3600)
    r = await client.get(f"/dl/{tok}")
    assert r.status == 200 and await r.read() == payload
    assert "attachment" in r.headers["Content-Disposition"]
    r = await client.get(f"/dl/{tok}", headers={"Range": "bytes=100-"})
    assert r.status == 206 and await r.read() == payload[100:]


async def test_bot_photo_set_only_when_missing(tmp_path):
    from dlbot.server import ensure_photo

    icon = tmp_path / "i.jpg"
    icon.write_bytes(b"\xff\xd8jpg")

    class PhotoTG(FakeTG):
        def __init__(self, count):
            super().__init__()
            self.count = count
            self.files = []

        async def call(self, method, **p):
            self.calls.append((method, p))
            return {"getMe": {"id": 777}, "getUserProfilePhotos": {"total_count": self.count, "photos": []}}[method]

        async def call_with_file(self, method, field, path, ctype, **p):
            self.files.append((method, field, path, p))
            return True

    tg = PhotoTG(0)
    await ensure_photo(tg, str(icon))
    assert ("getUserProfilePhotos", {"user_id": 777, "limit": 1}) in tg.calls
    assert tg.files == [("setMyProfilePhoto", "pic", str(icon), {"photo": {"type": "static", "photo": "attach://pic"}})]
    tg = PhotoTG(1)
    await ensure_photo(tg, str(icon))
    assert tg.files == []

    class Broken(FakeTG):
        async def call(self, method, **p):
            raise TgError(method, "boom")

    await ensure_photo(Broken(), str(icon))   # logs, never raises


async def test_call_with_file_sends_multipart(aiohttp_client, tmp_path):
    import json as _json

    from aiohttp import web

    from dlbot.tg import Telegram

    got = {}

    async def method(request):
        form = await request.post()
        got["path"] = request.path
        got["photo"] = _json.loads(form["photo"])
        got["pic"] = form["pic"].file.read()
        return web.json_response({"ok": True, "result": True})

    srv = web.Application()
    srv.router.add_post("/bot1:x/setMyProfilePhoto", method)
    c = await aiohttp_client(srv)
    icon = tmp_path / "i.jpg"
    icon.write_bytes(b"JPEGDATA")
    tg = Telegram("1:x", str(c.make_url("")).rstrip("/"))
    try:
        await tg.call_with_file("setMyProfilePhoto", "pic", str(icon), "image/jpeg",
                                photo={"type": "static", "photo": "attach://pic"})
    finally:
        await tg.close()
    assert got == {"path": "/bot1:x/setMyProfilePhoto", "photo": {"type": "static", "photo": "attach://pic"},
                   "pic": b"JPEGDATA"}
