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
