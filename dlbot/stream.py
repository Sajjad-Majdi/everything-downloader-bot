"""/dl/<token>: pipe the source to the phone with no disk writes."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

import aiohttp
from aiohttp import web

from . import ytdl
from .cache import LRU
from .config import Config
from .signing import BadToken, verify

log = logging.getLogger("stream")
CHUNK = 64 * 1024
PASS_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type", "Last-Modified", "ETag")


def safe_name(title: str, ext: str = "mp4") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "video").strip()[:80] or "video"
    return f"{name}.{ext}"


def disposition(name: str) -> str:
    return f"attachment; filename=\"video.mp4\"; filename*=UTF-8''{quote(name)}"


def ffmpeg_cmd(formats: list[dict]) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for f in formats:
        hdrs = dict(f.get("http_headers") or {})
        ua = hdrs.pop("User-Agent", None)
        if (f.get("protocol") or "https") in ("http", "https"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if ua:
            cmd += ["-user_agent", ua]
        if hdrs:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())]
        cmd += ["-i", f["url"]]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd


class Streamer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.active = 0
        self.resolved = LRU(maxsize=32, ttl=900)  # token -> info; ranged re-requests skip yt-dlp
        self._session: aiohttp.ClientSession | None = None

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60),
                                                  auto_decompress=False)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        try:
            body = verify(self.cfg.link_key, token)
        except BadToken as e:
            status = 410 if "expired" in str(e) else 403
            return web.Response(status=status, text="This link has expired or is invalid. Ask the bot for a new one.\n"
                                                    "لینک منقضی یا نامعتبره؛ از ربات یه لینک تازه بگیر.")
        if self.active >= self.cfg.max_streams:
            return web.Response(status=429, text="Busy, try again in a minute.\nسرور سرش شلوغه؛ یه دقیقه دیگه امتحان کن.")
        self.active += 1
        try:
            info = self.resolved.get(token)
            if info is None:
                info = await asyncio.to_thread(ytdl.resolve, body["u"], body["f"], body.get("i"), self.cfg.cookie_file)
                self.resolved.put(token, info)
            fmts = info.get("requested_formats") or [info]
            name = safe_name(body.get("t") or info.get("title"))
            if len(fmts) == 1:
                return await self.proxy(request, fmts[0], name)
            return await self.remux(request, fmts, name)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as e:
            log.exception("stream failed")
            return web.Response(status=502, text=f"Source failed: {type(e).__name__}\nمنبع جواب نداد؛ دوباره امتحان کن.")
        finally:
            self.active -= 1

    async def proxy(self, request: web.Request, f: dict, name: str) -> web.StreamResponse:
        headers = dict(f.get("http_headers") or {})
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        s = await self.session()
        method = "HEAD" if request.method == "HEAD" else "GET"
        async with s.request(method, f["url"], headers=headers) as up:
            if up.status >= 400:
                return web.Response(status=502, text=f"Source returned {up.status}")
            resp = web.StreamResponse(status=up.status)
            for h in PASS_HEADERS:
                if h in up.headers:
                    resp.headers[h] = up.headers[h]
            resp.headers.setdefault("Content-Type", "video/mp4")
            resp.headers["Content-Disposition"] = disposition(name.rsplit(".", 1)[0] + "." + (f.get("ext") or "mp4"))
            await resp.prepare(request)
            if method == "GET":
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
            await resp.write_eof()
            return resp

    async def remux(self, request: web.Request, fmts: list[dict], name: str) -> web.StreamResponse:
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "video/mp4"
        resp.headers["Accept-Ranges"] = "none"
        resp.headers["Content-Disposition"] = disposition(name)
        if request.method == "HEAD":
            await resp.prepare(request)
            await resp.write_eof()
            return resp
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd(fmts), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
        try:
            first = await proc.stdout.read(CHUNK)
            if not first:
                err = (await proc.stderr.read())[-300:].decode(errors="replace")
                log.warning("ffmpeg produced nothing: %s", err)
                return web.Response(status=502, text="Remux failed.\nنشد ویدیو رو آماده کنم.")
            await resp.prepare(request)
            await resp.write(first)
            while True:
                chunk = await proc.stdout.read(CHUNK)
                if not chunk:
                    break
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
