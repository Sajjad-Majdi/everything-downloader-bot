"""Optional Cloudflare WARP egress for YouTube.

Measured 2026-10-26: YouTube answers HTTP 403 to every player API call from Render's
Frankfurt IP (all clients, with and without cookies), so only the 240p progressive format
was left and most videos failed. WARP (free) gives the requests a Cloudflare IP instead.

At startup `start()` registers a free WARP device with `wgcf`, writes a `wireproxy` config
(userspace WireGuard, no root needed) and runs it as a local HTTP proxy. Only YouTube
traffic uses it (extraction, media download, /dl streaming); everything else goes direct.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger("warp")
DIR = os.environ.get("WARP_DIR") or "/tmp/warp"
PORT = int(os.environ.get("WARP_HTTP_PORT", "40001"))
PROXY_URL = f"http://127.0.0.1:{PORT}"
YT_HOST_RE = re.compile(r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com|googlevideo\.com|ytimg\.com)(/|:|$)",
                        re.I)
state: dict = {"ready": False, "error": None, "trace": None, "setup_s": None}
_proc: subprocess.Popen | None = None


def wanted() -> bool:
    return os.environ.get("YT_PROXY", "warp") == "warp"


def available() -> bool:
    return bool(shutil.which("wgcf") and shutil.which("wireproxy"))


def proxy_for(url: str | None) -> str | None:
    """The proxy to use for this URL: WARP for YouTube when it is up, else None (direct)."""
    if state["ready"] and url and YT_HOST_RE.match(url):
        return PROXY_URL
    return None


def _run(cmd: list[str], timeout: float = 60):
    r = subprocess.run(cmd, cwd=DIR, timeout=timeout, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1]} failed: {(r.stderr or r.stdout)[-300:]}")


def write_config(profile_text: str, port: int = PORT) -> str:
    """wireproxy config = the wgcf WireGuard profile + a local HTTP proxy section."""
    return profile_text.rstrip() + f"\n\n[http]\nBindAddress = 127.0.0.1:{port}\n"


def _setup_blocking() -> None:
    global _proc
    os.makedirs(DIR, exist_ok=True)
    profile = os.path.join(DIR, "wgcf-profile.conf")
    if not os.path.exists(profile):
        if not os.path.exists(os.path.join(DIR, "wgcf-account.toml")):
            _run(["wgcf", "register", "--accept-tos"])
        _run(["wgcf", "generate"])
    conf = os.path.join(DIR, "wireproxy.conf")
    with open(profile, encoding="utf-8") as fh, open(conf, "w", encoding="utf-8") as out:
        out.write(write_config(fh.read()))
    logf = open(os.path.join(DIR, "wireproxy.log"), "ab")
    _proc = subprocess.Popen(["wireproxy", "-c", conf], stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)


def _trace() -> str:
    """Cloudflare's trace through the proxy; contains warp=on when the tunnel works."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
    with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=15) as r:
        return r.read().decode(errors="replace")


async def start() -> bool:
    """Bring the tunnel up (never raises). Returns True when YouTube traffic will use it."""
    if not wanted():
        state["error"] = "disabled (YT_PROXY)"
        return False
    if not available():
        state["error"] = "wgcf/wireproxy not installed"
        log.warning("WARP not available: %s", state["error"])
        return False
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(_setup_blocking)
        last = None
        for _ in range(30):
            await asyncio.sleep(1)
            if _proc is not None and _proc.poll() is not None:
                raise RuntimeError(f"wireproxy exited with {_proc.returncode}")
            try:
                trace = await asyncio.to_thread(_trace)
                if "warp=on" in trace or "warp=plus" in trace:
                    state.update(ready=True, error=None, setup_s=round(time.monotonic() - t0, 1),
                                 trace={k: v for k, v in (ln.split("=", 1) for ln in trace.splitlines() if "=" in ln)
                                        if k in ("ip", "colo", "loc", "warp")})
                    log.info("WARP up in %.1fs: %s", time.monotonic() - t0, state["trace"])
                    return True
                last = "trace without warp=on"
            except Exception as e:   # tunnel not ready yet
                last = str(e)[:200]
        raise RuntimeError(f"tunnel never came up: {last}")
    except Exception as e:
        state.update(ready=False, error=str(e)[:300])
        log.warning("WARP setup failed after %.1fs: %s", time.monotonic() - t0, e)
        stop()
        return False


def stop():
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
    _proc = None
