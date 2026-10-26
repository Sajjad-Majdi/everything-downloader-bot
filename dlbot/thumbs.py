"""Video preview thumbnail for sendVideo: JPEG, at most 320 px and 200 KB."""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger("thumbs")
MAX_SIDE = 320
MAX_BYTES = 200 * 1024
FETCH_LIMIT = 3 * 1024 * 1024


def _is_jpg(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith((".jpg", ".jpeg"))


def pick_thumb(info: dict) -> tuple[str | None, bool]:
    """Return (url, needs_resize). Prefer a ready JPEG no wider than 320 px."""
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    small = [t for t in thumbs if _is_jpg(t["url"]) and 0 < max(t.get("width") or 0, t.get("height") or 0) <= MAX_SIDE]
    if small:
        return max(small, key=lambda t: t.get("width") or 0)["url"], False
    if (info.get("extractor_key") or info.get("extractor") or "").lower().startswith("youtube") and info.get("id"):
        return f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg", False  # 320x180 JPEG
    url = info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
    return url, True


async def make_thumb(session: aiohttp.ClientSession, info: dict, tmpdir: str) -> str | None:
    """Fetch (and shrink if needed) the thumbnail into tmpdir. Never raises."""
    url, resize = pick_thumb(info)
    if not url:
        return None
    raw = os.path.join(tmpdir, "thumb_src")
    out = os.path.join(tmpdir, "thumb.jpg")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            data = await r.content.read(FETCH_LIMIT)
        with open(raw, "wb") as fh:
            fh.write(data)
        if not resize and len(data) <= MAX_BYTES and data[:3] == b"\xff\xd8\xff":
            os.replace(raw, out)
            return out
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", raw,
            "-vf", f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease",
            "-frames:v", "1", "-q:v", "5", out,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 20)
        except asyncio.TimeoutError:
            proc.kill()      # never leave an ffmpeg behind
            await proc.wait()
            return None
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) <= MAX_BYTES:
            return out
    except Exception as e:
        log.info("thumbnail skipped: %s", e)
    return None
