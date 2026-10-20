"""Pure logic: turn a yt-dlp info dict into quality options and pick a delivery mode."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .config import SAFE_UPLOAD_LIMIT, URL_SEND_LIMIT

STANDARD_P = [144, 240, 360, 480, 540, 720, 1080, 1440, 2160, 4320]
# CDNs whose media URLs work from any IP (not locked to our server like googlevideo).
DIRECT_HOSTS = ("cdninstagram.com", "fbcdn.net", "twimg.com")
AUDIO_EXTS = {"m4a", "mp3", "opus", "ogg", "aac", "wav", "flac", "weba"}
MAX_OPTIONS = 7


@dataclass
class Option:
    label: str
    spec: str
    height: int
    size: int | None
    approx: bool
    video: dict
    audio: dict | None = None
    best: bool = False


def is_video(f: dict) -> bool:
    vc = f.get("vcodec")
    if vc == "none" or f.get("ext") in ("mhtml",) or f.get("format_note") == "storyboard":
        return False
    if vc is None and (f.get("ext") in AUDIO_EXTS or (f.get("acodec") not in (None, "none") and not f.get("height"))):
        return False
    return bool(f.get("url") or f.get("manifest_url") or f.get("format_id"))


def is_audio_only(f: dict) -> bool:
    return f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")


def has_audio(f: dict) -> bool:
    # unknown acodec on a video format means "probably muxed" (generic/twitter extractors)
    return f.get("acodec") != "none"


def p_of(f: dict) -> int:
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h or 0


def bucket(p: int) -> int:
    if not p:
        return 0
    return min(STANDARD_P, key=lambda s: abs(s - p))


def codec_score(f: dict) -> int:
    vc = (f.get("vcodec") or "").lower()
    if vc.startswith(("avc", "h264")) or (not vc and f.get("ext") == "mp4"):
        return 3
    if vc.startswith(("hev", "hvc", "h265")):
        return 2
    if vc.startswith("vp"):
        return 1
    return 0


def proto_score(f: dict) -> int:
    return 1 if (f.get("protocol") or "https") in ("http", "https") else 0


def fmt_size(f: dict | None, duration: float | None) -> tuple[int | None, bool]:
    if f is None:
        return 0, False
    if f.get("filesize"):
        return int(f["filesize"]), False
    if f.get("filesize_approx"):
        return int(f["filesize_approx"]), True
    if f.get("tbr") and duration:
        return int(f["tbr"] * duration * 125), True
    return None, True


def _formats(info: dict) -> list[dict]:
    fmts = info.get("formats")
    if fmts:
        return fmts
    return [info] if info.get("url") else []


def _best_audio(fmts: list[dict]) -> dict | None:
    auds = [f for f in fmts if is_audio_only(f)]
    if not auds:
        return None
    return max(auds, key=lambda f: (f.get("ext") == "m4a", not is_drc(f), proto_score(f),
                                    f.get("abr") or f.get("tbr") or 0))


def is_drc(f: dict) -> bool:
    """YouTube's dynamic-range-compressed audio twin (e.g. 140-drc)."""
    return "drc" in str(f.get("format_id", "")).lower() or "drc" in str(f.get("format_note", "")).lower()


def _make(label: str, v: dict, audio: dict | None, duration, best=False) -> Option:
    a = None if has_audio(v) else audio
    vs, va = fmt_size(v, duration)
    as_, aa = fmt_size(a, duration)
    size = None if vs is None or as_ is None else vs + as_
    spec = f"{v['format_id']}+{a['format_id']}" if a else str(v.get("format_id") or "b")
    return Option(label=label, spec=spec, height=p_of(v), size=size, approx=va or aa, video=v, audio=a, best=best)


def build_options(info: dict) -> list[Option]:
    fmts = _formats(info)
    duration = info.get("duration")
    vids = [f for f in fmts if is_video(f)]
    if not vids:
        return []
    audio = _best_audio(fmts)
    groups: dict[int, list[dict]] = {}
    for f in vids:
        groups.setdefault(bucket(p_of(f)), []).append(f)
    options: list[Option] = []
    for b in sorted(groups):
        pick = max(groups[b], key=lambda f: (codec_score(f), has_audio(f), proto_score(f), f.get("tbr") or 0))
        options.append(_make(f"{b}p" if b else "ویدیو", pick, audio, duration))
    options = options[-MAX_OPTIONS:]
    top = max(vids, key=lambda f: (p_of(f), f.get("tbr") or 0, proto_score(f)))
    best = _make("بهترین", top, audio, duration, best=True)
    if best.spec != options[-1].spec and best.height >= options[-1].height:
        best.label = f"بهترین {bucket(best.height)}p"
        options.append(best)
    return options


def direct_url(opt: Option) -> str | None:
    """A media URL the phone can fetch itself, or None if it must go through us."""
    if opt.audio is not None:
        return None
    v = opt.video
    if proto_score(v) == 0 or not v.get("url"):
        return None
    host = (urlparse(v["url"]).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in DIRECT_HOSTS):
        return v["url"]
    return None


def plan(opt: Option) -> str:
    """Return one of: url_send, upload, direct_link, stream_link."""
    direct = direct_url(opt)
    if opt.size is None:
        return "upload"  # downloader aborts at the limit and falls back to a link
    if opt.size <= SAFE_UPLOAD_LIMIT:
        if direct and opt.size <= URL_SEND_LIMIT:
            return "url_send"
        return "upload"
    return "direct_link" if direct else "stream_link"


def entries_of(info: dict) -> list[dict]:
    if info.get("_type") in ("playlist", "multi_video"):
        return [e for e in (info.get("entries") or []) if e]
    return []


def entry_is_resolved(e: dict) -> bool:
    return bool(e.get("formats") or (e.get("url") and e.get("_type") not in ("url", "url_transparent")))
