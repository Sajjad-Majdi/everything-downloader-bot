"""/dl/<token>: pipe the source to the phone with no disk writes."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import web

from . import warp, ytdl
from .cache import LRU
from .config import Config
from .signing import BadToken, verify

log = logging.getLogger("stream")
CHUNK = 64 * 1024
# YouTube throttles one long GET to about playback speed (measured 2026-10-26: 0.28 MB/s for a
# 720p /dl link through WARP, while yt-dlp's 10 MB range requests got ~6 MB/s). googlevideo
# sources are therefore fetched in RANGE_CHUNK pieces, like yt-dlp's http_chunk_size.
RANGE_CHUNK = int(os.environ.get("RANGE_CHUNK_MB", "10")) * 1024 * 1024
FIRST_CHUNK = 1024 * 1024   # small first piece: ffmpeg (and the phone) get the first bytes sooner
LOCAL_HOSTS = ("127.0.0.1", "::1")
PASS_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type", "Last-Modified", "ETag")


def safe_name(title: str, ext: str = "mp4") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "video").strip()[:80] or "video"
    return f"{name}.{ext}"


def disposition(name: str) -> str:
    return f"attachment; filename=\"video.mp4\"; filename*=UTF-8''{quote(name)}"


def chunked_host(url: str | None) -> bool:
    """Sources that throttle single long requests (YouTube's googlevideo)."""
    host = (urlparse(url or "").hostname or "").lower()
    return host == "googlevideo.com" or host.endswith(".googlevideo.com")


def parse_range(value: str | None) -> tuple[int, int | None] | None:
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (value or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):                       # suffix range "bytes=-N" is not used by players; serve all
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


def ffmpeg_cmd(formats: list[dict], proxy: str | None = None, local: list[str | None] | None = None) -> list[str]:
    """`local[i]`, when set, is our own ranged relay for format i (it adds headers and the proxy itself)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for i, f in enumerate(formats):
        if local and local[i]:
            cmd += ["-i", local[i]]
            continue
        if proxy:   # googlevideo URLs only work from the IP that extracted them (the WARP tunnel)
            cmd += ["-http_proxy", proxy]
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
        self.sources = LRU(maxsize=64, ttl=6 * 3600)  # relay id -> (url, headers, proxy) for ffmpeg
        self.local_base = f"http://127.0.0.1:{os.environ.get('PORT', '10000')}"
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
                                                    "لینک منقضی یا نامعتبر است. لینک تازه‌ای از ربات بگیرید.")
        if self.active >= self.cfg.max_streams:
            return web.Response(status=429, text="Busy, try again in a minute.\nسرور مشغول است. یک دقیقه بعد دوباره تلاش کنید.")
        self.active += 1
        try:
            info = self.resolved.get(token)
            if info is None:
                info = await asyncio.to_thread(ytdl.resolve, body["u"], body["f"], body.get("i"), self.cfg.cookie_file)
                self.resolved.put(token, info)
            fmts = info.get("requested_formats") or [info]
            name = safe_name(body.get("t") or info.get("title"))
            via = warp.proxy_for(body["u"])
            if len(fmts) == 1:
                return await self.proxy(request, fmts[0], name, via)
            return await self.remux(request, fmts, name, via)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as e:
            log.exception("stream failed")
            return web.Response(status=502, text=f"Source failed: {type(e).__name__}\nمنبع پاسخ نداد. دوباره تلاش کنید.")
        finally:
            self.active -= 1

    def register(self, f: dict, via: str | None) -> str:
        sid = secrets.token_urlsafe(12)
        self.sources.put(sid, (f["url"], dict(f.get("http_headers") or {}), via))
        return f"{self.local_base}/_src/{sid}"

    async def relay(self, request: web.Request) -> web.StreamResponse:
        """Local only: a googlevideo source served with Range support, fetched in RANGE_CHUNK pieces."""
        if request.remote not in LOCAL_HOSTS:
            return web.Response(status=403)
        src = self.sources.get(request.match_info["sid"])
        if src is None:
            return web.Response(status=404)
        url, headers, via = src
        return await self.ranged(request, url, headers, via, {})

    async def ranged(self, request: web.Request, url: str, headers: dict, via: str | None,
                     extra_headers: dict) -> web.StreamResponse:
        """Answer `request` (with or without Range) by fetching `url` in RANGE_CHUNK range requests."""
        want = parse_range(request.headers.get("Range"))
        start, end = (want if want else (0, None))
        s = await self.session()
        resp: web.StreamResponse | None = None
        pos, total = start, None
        while True:
            stop = pos + (min(FIRST_CHUNK, RANGE_CHUNK) if resp is None else RANGE_CHUNK) - 1
            if end is not None:
                stop = min(stop, end)
            if total is not None:
                stop = min(stop, total - 1)
            async with s.get(url, headers=dict(headers, Range=f"bytes={pos}-{stop}"), proxy=via) as up:
                if up.status == 416 and resp is None:
                    return web.Response(status=416)
                if up.status >= 400:
                    if resp is None:
                        return web.Response(status=502, text=f"Source returned {up.status}")
                    break
                if resp is None:
                    cr = up.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)$", cr)
                    total = int(m.group(1)) if m else None
                    if up.status == 200 or total is None:   # source ignores ranges: pass it through once
                        resp = web.StreamResponse(status=200)
                        for h in ("Content-Length", "Content-Type"):
                            if h in up.headers:
                                resp.headers[h] = up.headers[h]
                        resp.headers.update(extra_headers)
                        await resp.prepare(request)
                        if request.method != "HEAD":
                            async for chunk in up.content.iter_chunked(CHUNK):
                                await resp.write(chunk)
                        await resp.write_eof()
                        return resp
                    last = total - 1 if end is None else min(end, total - 1)
                    resp = web.StreamResponse(status=206 if want else 200)
                    resp.headers["Content-Length"] = str(last - start + 1)
                    resp.headers["Accept-Ranges"] = "bytes"
                    resp.headers["Content-Type"] = up.headers.get("Content-Type", "video/mp4")
                    if want:
                        resp.headers["Content-Range"] = f"bytes {start}-{last}/{total}"
                    resp.headers.update(extra_headers)
                    await resp.prepare(request)
                    if request.method == "HEAD":
                        await resp.write_eof()
                        return resp
                    end = last
                async for chunk in up.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
            pos = stop + 1
            if end is not None and pos > end:
                break
        await resp.write_eof()
        return resp

    async def proxy(self, request: web.Request, f: dict, name: str, via: str | None = None) -> web.StreamResponse:
        if chunked_host(f.get("url")):
            disp = {"Content-Disposition": disposition(name.rsplit(".", 1)[0] + "." + (f.get("ext") or "mp4"))}
            return await self.ranged(request, f["url"], dict(f.get("http_headers") or {}), via, disp)
        headers = dict(f.get("http_headers") or {})
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        s = await self.session()
        method = "HEAD" if request.method == "HEAD" else "GET"
        async with s.request(method, f["url"], headers=headers, proxy=via) as up:
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

    async def remux(self, request: web.Request, fmts: list[dict], name: str, via: str | None = None) -> web.StreamResponse:
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "video/mp4"
        resp.headers["Accept-Ranges"] = "none"
        resp.headers["Content-Disposition"] = disposition(name)
        if request.method == "HEAD":
            await resp.prepare(request)
            await resp.write_eof()
            return resp
        local = [self.register(f, via) if chunked_host(f.get("url")) else None for f in fmts]
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd(fmts, via, local), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
        try:
            first = await proc.stdout.read(CHUNK)
            if not first:
                err = (await proc.stderr.read())[-300:].decode(errors="replace")
                log.warning("ffmpeg produced nothing: %s", err)
                return web.Response(status=502, text="Remux failed.\nآماده‌سازی ویدیو ناموفق بود.")
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
