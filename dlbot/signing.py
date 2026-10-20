"""Signed, time-limited tokens for /dl/<token> stream links."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class BadToken(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: bytes, payload: dict, ttl: int, now: float | None = None) -> str:
    body = dict(payload, e=int((now or time.time()) + ttl))
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    mac = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    return f"{_b64(raw)}.{_b64(mac)}"


def verify(key: bytes, token: str, now: float | None = None) -> dict:
    try:
        raw_part, mac_part = token.split(".", 1)
        raw, mac = _unb64(raw_part), _unb64(mac_part)
    except Exception as exc:  # malformed
        raise BadToken("malformed") from exc
    good = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    if not key or not hmac.compare_digest(good, mac):
        raise BadToken("bad signature")
    body = json.loads(raw)
    if body.get("e", 0) < (now or time.time()):
        raise BadToken("expired")
    return body
