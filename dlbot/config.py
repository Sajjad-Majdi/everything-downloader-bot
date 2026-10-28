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
OWNER_ID = 394891587                      # Sajjad: always allowed, never limited by seats
STORE_URL = "https://downloader-store.ource--ode.workers.dev"


def _derive(token: str, label: str) -> str:
    return hmac.new(token.encode(), label.encode(), hashlib.sha256).hexdigest()


@dataclass
class Config:
    bot_token: str = ""
    public_url: str = ""
    webhook_secret: str = ""
    link_key: bytes = b""
    allowed_ids: set[int] = field(default_factory=set)
    owner_id: int | None = OWNER_ID
    bench_key: str = ""
    api_base: str = "https://api.telegram.org"
    max_jobs: int = 2
    max_streams: int = 2
    cookie_file: str | None = None
    # public seats + monthly outbound guard (dlbot/store.py); no store_url = private mode
    store_url: str = ""
    store_secret: str = ""
    seats: int = 4                         # 5 people in total with the owner (Sajjad, 2026-10-28)
    out_soft_gb: float = 4.5               # Render free: ~5 GB outbound per month per workspace
    out_hard_gb: float = 4.8
    store_cache: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        public = (env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        secret = env.get("WEBHOOK_SECRET", "").strip() or (_derive(token, "webhook")[:48] if token else "")
        key = env.get("LINK_SECRET", "").strip() or (_derive(token, "links") if token else "")
        id_list = [int(x) for x in env.get("ALLOWED_USER_IDS", str(OWNER_ID)).replace(" ", "").split(",") if x]
        owner = int(env.get("OWNER_ID", OWNER_ID))
        ids = set(id_list) | {owner}
        cfg = cls(
            bot_token=token,
            public_url=public,
            webhook_secret=secret,
            link_key=key.encode(),
            allowed_ids=ids,
            owner_id=owner,
            bench_key=env.get("BENCH_KEY", "").strip(),
            api_base=env.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            # 2 jobs: a short clip no longer waits behind a long download (peak ~320 of 512 MB, 2026-10-26)
            max_jobs=max(1, int(env.get("MAX_JOBS", "2"))),
            max_streams=max(1, int(env.get("MAX_STREAMS", "2"))),
            store_url="" if env.get("STORE_URL", STORE_URL).strip() in ("", "off") else env.get("STORE_URL", STORE_URL).strip(),
            store_secret=env.get("STORE_SECRET", "").strip() or (_derive(token, "store") if token else ""),
            seats=max(0, int(env.get("SEATS", "4"))),
            out_soft_gb=float(env.get("OUTBOUND_SOFT_GB", "4.5")),
            out_hard_gb=float(env.get("OUTBOUND_HARD_GB", "4.8")),
            store_cache=env.get("STORE_CACHE", os.path.join(tempfile.gettempdir(), "dlbot-store.json")),
        )
        cookies = env.get("COOKIES_B64", "").strip()
        if cookies:
            fd, path = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.b64decode(cookies))
            cfg.cookie_file = path
        return cfg
