"""Real ffmpeg + yt-dlp runs against a local HTTP server (no internet needed)."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile

import pytest
from aiohttp import web

from dlbot import ytdl
from dlbot.server import make_app
from dlbot.signing import sign
from test_handlers import FakeTG, cfg

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
NOWIN = 0x08000000 if os.name == "nt" else 0


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # the laptop's system proxy must not see 127.0.0.1 test traffic
    orig = ytdl.base_opts
    monkeypatch.setattr(ytdl, "base_opts", lambda c=None: orig(c) | {"proxy": ""})


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NOWIN)


@pytest.fixture(scope="module")
def media():
    d = tempfile.mkdtemp(prefix="dltest-")
    v, a, m = (os.path.join(d, n) for n in ("v.mp4", "a.m4a", "muxed.mp4"))
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "3", "-c:v", "libx264",
         "-g", "25", "-pix_fmt", "yuv420p", "-an", v])
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:a", "aac", a])
    run(["ffmpeg", "-y", "-i", v, "-i", a, "-c", "copy", m])
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def serve(aiohttp_client, folder):
    app = web.Application()
    app.router.add_static("/", folder)
    client = await aiohttp_client(app)
    return lambda name: str(client.make_url("/" + name))


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], capture_output=True,
                         check=True, creationflags=NOWIN).stdout
    return sorted(s["codec_type"] for s in json.loads(out)["streams"])


async def test_remux_stream_video_plus_audio(aiohttp_client, monkeypatch, media):
    url = await serve(aiohttp_client, media)
    monkeypatch.setattr(ytdl, "resolve", lambda u, f, i=None, c=None: {
        "title": "Clip", "requested_formats": [{"url": url("v.mp4"), "protocol": "http", "http_headers": {"User-Agent": "t"}},
                                               {"url": url("a.m4a"), "protocol": "http", "http_headers": {}}]})
    client = await aiohttp_client(make_app(cfg(), FakeTG()))
    tok = sign(b"key", {"u": "https://youtu.be/x", "f": "1+2", "t": "Clip"}, 3600)
    r = await client.get(f"/dl/{tok}")
    assert r.status == 200 and r.headers["Content-Type"] == "video/mp4"
    body = await r.read()
    out = os.path.join(media, "out.mp4")
    with open(out, "wb") as fh:
        fh.write(body)
    assert probe(out) == ["audio", "video"]


async def test_ytdlp_download_merges_and_cleans(aiohttp_client, media):
    url = await serve(aiohttp_client, media)
    tmp = tempfile.mkdtemp(prefix="dl-")
    try:
        progress = {}
        path = await asyncio.to_thread(ytdl.download, url("muxed.mp4"), "b", tmp, progress)
        assert path.endswith(".mp4") and probe(path) == ["audio", "video"]
        assert progress["done"] > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_ytdlp_download_aborts_over_limit(aiohttp_client, media):
    url = await serve(aiohttp_client, media)
    tmp = tempfile.mkdtemp(prefix="dl-")
    try:
        with pytest.raises(ytdl.TooBig):
            await asyncio.to_thread(ytdl.download, url("muxed.mp4"), "b", tmp, {}, limit=1000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_extract_generic_gives_options(aiohttp_client, media):
    from dlbot.formats import build_options
    url = await serve(aiohttp_client, media)
    info = await asyncio.to_thread(ytdl.extract, url("muxed.mp4"))
    opts = build_options(info)
    assert opts and opts[0].spec


async def test_ytdlp_download_reuses_extracted_info(aiohttp_client, media):
    hits = []

    async def counted(request):
        hits.append(request.method)
        return web.FileResponse(os.path.join(media, "muxed.mp4"))

    app = web.Application()
    app.router.add_route("*", "/clip.mp4", counted)
    c = await aiohttp_client(app)
    url = str(c.make_url("/clip.mp4"))
    info = await asyncio.to_thread(ytdl.extract, url)
    n_extract = len(hits)
    tmp = tempfile.mkdtemp(prefix="dl-")
    try:
        progress = {}
        path = await asyncio.to_thread(ytdl.download, url, "b", tmp, progress, None, None, info=info)
        assert probe(path) == ["audio", "video"]
        assert len(hits) - n_extract == 1          # only the media fetch, no second extraction
        assert progress["t_first_byte"] >= progress["t_start"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_make_thumb_resizes_and_passes_small_jpeg(aiohttp_client, media):
    import aiohttp

    from dlbot.thumbs import make_thumb, pick_thumb

    big = os.path.join(media, "big.jpg")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1080x1920", "-frames:v", "1", big])
    small = os.path.join(media, "small.jpg")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180", "-frames:v", "1", small])
    url = await serve(aiohttp_client, media)
    tmp = tempfile.mkdtemp(prefix="th-")
    try:
        async with aiohttp.ClientSession() as s:
            out = await make_thumb(s, {"thumbnail": url("big.jpg")}, tmp)
            assert out and os.path.getsize(out) <= 200 * 1024
            dims = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                                   out], capture_output=True, text=True, creationflags=NOWIN).stdout.strip()
            assert dims == "180,320"
            os.remove(out)
            info = {"thumbnails": [{"url": url("small.jpg"), "width": 320, "height": 180},
                                   {"url": url("big.jpg"), "width": 1080}]}
            assert pick_thumb(info) == (url("small.jpg"), False)
            out = await make_thumb(s, info, tmp)
            assert open(out, "rb").read() == open(small, "rb").read()
            assert await make_thumb(s, {"thumbnail": url("missing.jpg")}, tmp) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_send_video_multipart_with_thumb_and_progress(aiohttp_client, media):
    from dlbot.tg import Telegram

    got = {}

    async def send_video(request):
        form = await request.post()
        got.update(fields={k: form[k] for k in ("chat_id", "supports_streaming", "width", "duration")},
                   video=len(form["video"].file.read()), thumb=form["thumbnail"].file.read()[:3])
        return web.json_response({"ok": True, "result": {"message_id": 9, "video": {"file_id": "F"}}})

    srv = web.Application()
    srv.router.add_post("/bot1:x/sendVideo", send_video)
    c = await aiohttp_client(srv)
    thumb = os.path.join(media, "t.jpg")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90", "-frames:v", "1", thumb])
    tg = Telegram("1:x", str(c.make_url("")).rstrip("/"))
    progress = {}
    try:
        res = await tg.send_video(5, os.path.join(media, "muxed.mp4"), is_file=True, caption="c", thumb=thumb,
                                  progress=progress, width=320, duration=3)
    finally:
        await tg.close()
    size = os.path.getsize(os.path.join(media, "muxed.mp4"))
    assert res["message_id"] == 9 and got["video"] == size and got["thumb"] == b"\xff\xd8\xff"
    assert got["fields"] == {"chat_id": "5", "supports_streaming": "true", "width": "320", "duration": "3"}
    assert progress["up"] == progress["up_total"] == size and "t_up_done" in progress
