"""yt-dlp wrappers. All functions are blocking; call them with asyncio.to_thread."""
from __future__ import annotations

import os

from yt_dlp import YoutubeDL

from .config import TG_UPLOAD_LIMIT


class TooBig(Exception):
    pass


def base_opts(cookie_file: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
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


def extract(url: str, cookie_file: str | None = None) -> dict:
    with YoutubeDL(base_opts(cookie_file)) as ydl:
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
             cookie_file: str | None = None, limit: int = TG_UPLOAD_LIMIT) -> str:
    """Download (stream-copy merge, no re-encode) into tmpdir and return the file path.

    `progress` is a shared dict the async side reads: done, cur, total.
    Raises TooBig as soon as the bytes pass `limit`.
    """
    progress.update(done=0, cur=0, too_big=False)

    def hook(d):
        if d.get("status") == "downloading":
            progress["cur"] = d.get("downloaded_bytes") or 0
            if progress["done"] + progress["cur"] > limit:
                progress["too_big"] = True
                raise TooBig()
        elif d.get("status") == "finished":
            progress["done"] += d.get("total_bytes") or d.get("downloaded_bytes") or 0
            progress["cur"] = 0

    opts = base_opts(cookie_file) | {
        "format": spec,
        "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "progress_hooks": [hook],
        "max_filesize": limit,
        "http_chunk_size": 10 * 1024 * 1024,
        "concurrent_fragment_downloads": 2,
    }
    if index:
        opts |= {"noplaylist": False, "playlist_items": str(index), "extract_flat": False}
    try:
        with YoutubeDL(opts) as ydl:
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
    return path
