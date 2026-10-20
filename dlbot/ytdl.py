"""yt-dlp wrappers. All functions are blocking; call them with asyncio.to_thread."""
from __future__ import annotations

import copy
import logging
import os
import re
import tempfile
import time

from yt_dlp import YoutubeDL

from .config import TG_UPLOAD_LIMIT

log = logging.getLogger("ytdl")
# Persistent (per container) cache: YouTube player JS + preprocessed challenge solver.
CACHE_DIR = os.environ.get("YTDLP_CACHE") or os.path.join(tempfile.gettempdir(), "yt-dlp-cache")


def enable_jsc_cache() -> bool:
    """Cache YouTube's preprocessed player for the JS challenge solver.

    Measured on Render (0.1 CPU): every extraction spent ~11 s in deno re-parsing the full
    2-3 MB player. yt-dlp ships a preprocessed-player cache but keeps it off (no rotation of
    old players); one small file in /tmp per player version is fine for us.
    """
    if os.environ.get("YT_JSC_CACHE", "1") != "1":
        return False
    try:
        from yt_dlp.extractor.youtube.jsc._builtin.ejs import EJSBaseJCP
        EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
        return True
    except Exception as e:  # layout changed in a newer yt-dlp
        log.warning("could not enable JS challenge cache: %s", e)
        return False


JSC_CACHE = enable_jsc_cache()


class TooBig(Exception):
    pass


class _Logger:
    """Send yt-dlp warnings/errors to our log (they were silenced before)."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp error: %s", msg)


def base_opts(cookie_file: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": _Logger(),
        "cachedir": CACHE_DIR,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "playlistend": 20,
        "skip_unavailable_fragments": False,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


class DebugLog:
    """Collects yt-dlp debug lines with time offsets (for /bench diagnostics)."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.lines: list[str] = []

    def _add(self, msg):
        low = msg.lower()
        if "cookie" in low or "authorization" in low or "http headers" in low:
            return
        self.lines.append(f"{time.monotonic() - self.t0:6.2f} {msg[:160]}")

    debug = info = warning = error = _add


YT_RE = re.compile(r"^https?://([\w-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)
# A YouTube client that needs no JS player (no deno run) and no cookies; tried first.
FAST_CLIENT = os.environ.get("YT_FAST_CLIENT", "visionos").strip()


def _usable(info: dict) -> bool:
    return any(f.get("url") and f.get("vcodec") != "none" for f in info.get("formats") or [])


def extract(url: str, cookie_file: str | None = None, extra: dict | None = None, fast: bool = True) -> dict:
    """Extract info. For a single YouTube video, first try the fast JS-less client without
    cookies (~2-3 s at 0.1 CPU); fall back to the full path (cookies + deno) when it fails."""
    extra = extra or {}
    if fast and FAST_CLIENT and YT_RE.match(url) and "list=" not in url and "/playlist" not in url:
        opts = base_opts(None) | extra | {
            "extractor_args": {"youtube": {"player_client": FAST_CLIENT.split(",")}}}
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=False))
            if _usable(info):
                info["_fast_client"] = FAST_CLIENT
                return info
            log.info("fast client gave no usable formats; full extraction")
        except Exception as e:
            log.info("fast client failed (%s); full extraction", str(e)[:120])
    with YoutubeDL(base_opts(cookie_file) | extra) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def resolve(url: str, spec: str, index: int | None = None, cookie_file: str | None = None) -> dict:
    """Fresh media URLs (and headers) for one chosen format spec."""
    opts = base_opts(cookie_file) | {"format": spec}
    if index:
        opts |= {"noplaylist": False, "playlist_items": str(index), "extract_flat": False}
    with YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise RuntimeError("item not available")
        info = entries[0]
    return info


def download(url: str, spec: str, tmpdir: str, progress: dict, index: int | None = None,
             cookie_file: str | None = None, limit: int = TG_UPLOAD_LIMIT, info: dict | None = None) -> str:
    """Download (stream-copy merge, no re-encode) into tmpdir and return the file path.

    With `info` (the dict from extract()), yt-dlp reuses it and skips a second extraction.
    `progress` is a shared dict the async side reads: done, cur, total, plus timing marks
    (t_start, t_first_byte, t_dl_end, t_pp, files).
    Raises TooBig as soon as the bytes pass `limit`.
    """
    progress.update(done=0, cur=0, too_big=False, t_start=time.monotonic(), files=[])

    def hook(d):
        now = time.monotonic()
        if d.get("status") == "downloading":
            progress.setdefault("t_first_byte", now)
            progress["cur"] = d.get("downloaded_bytes") or 0
            if progress["done"] + progress["cur"] > limit:
                progress["too_big"] = True
                raise TooBig()
        elif d.get("status") == "finished":
            got = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            progress["done"] += got
            progress["cur"] = 0
            progress["files"].append((got, round(d.get("elapsed") or 0, 2)))
            progress["t_dl_end"] = now

    def pp_hook(d):
        if d.get("status") == "started":
            progress.setdefault("t_pp", time.monotonic())
        elif d.get("status") == "finished":
            progress["t_pp_end"] = time.monotonic()

    opts = base_opts(cookie_file) | {
        "format": spec,
        "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "max_filesize": limit,
        "http_chunk_size": 10 * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
    }
    if index and info is None:
        opts |= {"noplaylist": False, "playlist_items": str(index), "extract_flat": False}
    try:
        with YoutubeDL(opts) as ydl:
            if info is not None:
                ydl.process_ie_result(ydl.sanitize_info(copy.deepcopy(info), True), download=True)
            else:
                ydl.download([url])
    except Exception:
        if progress.get("too_big"):
            raise TooBig()
        raise
    files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
    files = [f for f in files if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
    if not files:
        # max_filesize skips a too-large file without raising
        raise TooBig()
    path = max(files, key=os.path.getsize)
    if os.path.getsize(path) > limit:
        raise TooBig()
    progress["t_end"] = time.monotonic()
    return path
