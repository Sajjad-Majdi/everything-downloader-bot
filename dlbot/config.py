"""Settings, all from environment variables."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
from dataclasses import dataclass, field

TG_UPLOAD_LIMIT = 50 * 1024 * 1024        # Telegram bot API upload cap
SAFE_UPLOAD_LIMIT = 49 * 1024 * 1024      # leave room for container overhead
URL_SEND_LIMIT = 20 * 1024 * 1024         # Telegram fetches URLs itself up to ~20 MB
LINK_TTL = 3600                           # signed /dl links live 1 hour


def _derive(token: str, label: str) -> str:
    return hmac.new(token.encode(), label.encode(), hashlib.sha256).hexdigest()


@dataclass
class Config:
    bot_token: str = ""
    public_url: str = ""
    webhook_secret: str = ""
    link_key: bytes = b""
    allowed_ids: set[int] = field(default_factory=set)
    api_base: str = "https://api.telegram.org"
    max_jobs: int = 1
    max_streams: int = 2
    cookie_file: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        public = (env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        secret = env.get("WEBHOOK_SECRET", "").strip() or (_derive(token, "webhook")[:48] if token else "")
        key = env.get("LINK_SECRET", "").strip() or (_derive(token, "links") if token else "")
        ids = {int(x) for x in env.get("ALLOWED_USER_IDS", "394891587").replace(" ", "").split(",") if x}
        cfg = cls(
            bot_token=token,
            public_url=public,
            webhook_secret=secret,
            link_key=key.encode(),
            allowed_ids=ids,
            api_base=env.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            max_jobs=max(1, int(env.get("MAX_JOBS", "1"))),
            max_streams=max(1, int(env.get("MAX_STREAMS", "2"))),
        )
        cookies = env.get("COOKIES_B64", "").strip()
        if cookies:
            fd, path = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.b64decode(cookies))
            cfg.cookie_file = path
        return cfg
