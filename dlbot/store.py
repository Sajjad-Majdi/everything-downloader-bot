"""Public seats and the monthly outbound guard, kept in the `downloader-store` Worker (D1).

Render free wipes the disk on every deploy or restart, so the durable copy lives in Cloudflare
(`store/worker.js`). This process keeps the live copy in memory and syncs it:
- seats: the first `seats` distinct users (besides the owner and ALLOWED_USER_IDS) get one,
  first come first served (Sajjad, 2026-10-28: 5 people in total including him).
- outbound: Render free includes ~5 GB outbound per month for the whole workspace (HadithYab
  too); over it every free service is suspended until the 1st. Bytes this bot sends out
  (Telegram uploads, /dl relay) are counted; at `soft` only the owner is served, at `hard`
  nobody gets a video.
The Worker being down for a while changes nothing once the state is loaded: seats and bytes are
granted/counted in memory, written to a local cache file and synced when the Worker answers again.
Before the first load, new users are told to retry later (the seat count is unknown then).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("store")
GB = 1000 ** 3              # decimal GB: the smaller unit, so the guard trips early rather than late
SYNC_EVERY = 60.0           # background loop period (s)
FLUSH_BYTES = 20 * 1000 ** 2  # write the byte counter when this much is pending ...
FLUSH_AGE = 600.0           # ... or when bytes have waited this long (D1 free: 100k writes/day)
RETRY_LOAD = 30.0

OK, FULL, UNAVAILABLE = "ok", "full", "unavailable"


def month_now() -> str:
    return time.strftime("%Y-%m", time.gmtime())


class Store:
    def __init__(self, url: str, secret: str, seats: int, soft_gb: float, hard_gb: float,
                 cache_path: str | None = None, session: aiohttp.ClientSession | None = None):
        self.url = url.rstrip("/")
        self.secret = secret
        self.cfg_seats, self.cfg_soft, self.cfg_hard = seats, soft_gb, hard_gb
        self.cache_path = cache_path
        self._session = session
        self.loaded = False                  # seats/bytes known (from the Worker or the local cache)
        self.fetched = False                 # the Worker's state was read at least once
        self._cache_used = 0
        self.ok: bool | None = None          # last call to the Worker worked
        self.last_error = ""
        self.seats: list[int] = []
        self.limits: dict = {}
        self.month = month_now()
        self.base = 0                        # Worker's byte total for self.month
        self.pending = 0                     # counted here, not yet sent to the Worker
        self.inflight = 0                    # sent, waiting for the answer
        self.pending_seats: list[int] = []
        self._pending_since: float | None = None
        self._lock = asyncio.Lock()
        self._read_cache()

    # limits ------------------------------------------------------------------------
    @property
    def max_seats(self) -> int:
        return int(self.limits.get("seats", self.cfg_seats))

    @property
    def soft(self) -> int:
        return int(float(self.limits.get("soft_gb", self.cfg_soft)) * GB)

    @property
    def hard(self) -> int:
        return int(float(self.limits.get("hard_gb", self.cfg_hard)) * GB)

    # outbound ----------------------------------------------------------------------
    def _roll(self):
        m = month_now()
        if m != self.month:     # new month: the old month's unsent bytes are dropped with it
            self.month, self.base, self.pending, self.inflight = m, 0, 0, 0

    def used(self) -> int:
        self._roll()
        return self.base + self.pending + self.inflight

    def add_bytes(self, n: int):
        if n <= 0:
            return
        self._roll()
        self.pending += int(n)
        if self._pending_since is None:
            self._pending_since = time.monotonic()

    def level(self) -> str:
        used = self.used()
        return "hard" if used >= self.hard else "soft" if used >= self.soft else "ok"

    def blocked(self, is_owner: bool, size: int | None = None) -> bool:
        """True when this user may not get a video now (size: the file about to go out)."""
        after = self.used() + (size or 0)
        return after >= self.hard or (not is_owner and after >= self.soft)

    # seats -------------------------------------------------------------------------
    def has_seat(self, uid: int) -> bool:
        return uid in self.seats

    async def seat(self, uid: int) -> str:
        if uid in self.seats:
            return OK
        if not self.loaded:
            return UNAVAILABLE
        if len(self.seats) >= self.max_seats:
            return FULL
        self.seats.append(uid)          # memory is the live copy; the Worker enforces the cap too
        self.pending_seats.append(uid)
        log.info("seat %d/%d given to user %s", len(self.seats), self.max_seats, uid)
        self._write_cache()
        try:
            await asyncio.wait_for(self.sync(), 8)
        except Exception as e:
            log.warning("seat sync failed, will retry: %s", e)
        return OK if uid in self.seats else FULL

    # Worker calls ------------------------------------------------------------------
    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        s = await self.session()
        headers = {"x-store-secret": self.secret, "user-agent": "dlbot-store"}
        try:
            async with s.request(method, self.url + path, json=body, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"store {path} HTTP {r.status}")
                data = await r.json(content_type=None)
            self.ok = True
            return data
        except Exception as e:
            self.ok, self.last_error = False, f"{type(e).__name__}: {e}"[:200]
            raise

    def _merge(self, data: dict, month: str | None = None):
        """Take the Worker's state. Seats granted here and not yet sent stay; on the first read,
        seats and bytes the local cache knows but the Worker does not become pending (sent later)."""
        remote = [int(x) for x in data.get("seats") or []]
        if not self.fetched:
            self.pending_seats += [u for u in self.seats if u not in remote and u not in self.pending_seats]
        self.limits = data.get("limits") or {}
        self.seats = remote + [u for u in self.seats if u not in remote and u in self.pending_seats]
        self._roll()
        month = month or self.month
        if month == self.month:
            self.base = int((data.get("usage") or {}).get(month, 0))
        if not self.fetched:
            self.add_bytes(self._cache_used - self.base)   # bytes the cache saw that never reached the Worker
            self._cache_used = 0
        self.loaded = self.fetched = True
        self._write_cache()

    async def load(self) -> bool:
        try:
            data = await self._req("GET", "/state")
        except Exception as e:
            log.warning("store load failed: %s", e)
            return False
        async with self._lock:
            self._merge(data)
        log.info("store loaded: %d/%d seats, %s used %.1f MB", len(self.seats), self.max_seats, self.month,
                 self.used() / 1e6)
        return True

    async def sync(self):
        """Send new seats and pending bytes; take the Worker's totals back."""
        async with self._lock:
            self._roll()
            month, add, seats = self.month, self.pending, list(self.pending_seats)
            self.pending, self.inflight = 0, add
            try:
                data = await self._req("POST", "/sync", {"seats_add": seats, "max": self.max_seats,
                                                         "month": month, "add": add})
            except BaseException:          # also a timeout cancel: the bytes must not get lost
                if self.month == month:
                    self.pending += add
                self.inflight = 0
                raise
            self.inflight = 0
            self._pending_since = time.monotonic() if self.pending else None
            self.pending_seats = [u for u in self.pending_seats if u not in seats]
            refused = [u for u in seats if u not in (data.get("seats") or [])]
            if refused:
                log.warning("store refused seats %s (cap already reached there)", refused)
            self._merge(data, month)

    def due(self) -> bool:
        if self.pending_seats:
            return True
        if not self.pending:
            return False
        return self.pending >= FLUSH_BYTES or time.monotonic() - (self._pending_since or 0) >= FLUSH_AGE

    async def run(self):
        """Background: load (retrying), then sync when something is due."""
        while not await self.load():
            await asyncio.sleep(RETRY_LOAD)
        while True:
            await asyncio.sleep(SYNC_EVERY)
            if self.due():
                try:
                    await self.sync()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("store sync failed: %s", e)

    async def close(self):
        """Last flush on shutdown (Render sends SIGTERM before a deploy swaps the instance)."""
        if self.fetched and (self.pending or self.pending_seats):
            try:
                await asyncio.wait_for(self.sync(), 8)
            except Exception as e:
                log.warning("final store sync failed: %s", e)
        if self._session and not self._session.closed:
            await self._session.close()

    # local cache -------------------------------------------------------------------
    def _write_cache(self):
        if not self.cache_path:
            return
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"seats": self.seats, "month": self.month, "used": self.used(), "limits": self.limits}, fh)
            os.replace(tmp, self.cache_path)
        except Exception as e:
            log.warning("store cache write failed: %s", e)

    def _read_cache(self):
        """A process restart inside the same container keeps /tmp: start from that snapshot (the
        Worker's state, merged in at load, wins for seats; bytes take the larger total)."""
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path) as fh:
                c = json.load(fh)
            self.seats = [int(x) for x in c.get("seats") or []]
            self.limits = c.get("limits") or {}
            if c.get("month") == self.month:
                self.base = self._cache_used = int(c.get("used") or 0)
            self.loaded = True
            log.info("store cache read: %d seats, %.1f MB", len(self.seats), self.base / 1e6)
        except Exception as e:
            log.warning("store cache unreadable: %s", e)

    def report(self) -> dict:
        return {"store": self.fetched, "store_ok": self.ok, "seats": f"{len(self.seats)}/{self.max_seats}",
                "month": self.month, "out_mb": round(self.used() / 1e6, 1),
                "out_soft_mb": round(self.soft / 1e6), "out_hard_mb": round(self.hard / 1e6),
                "out_level": self.level()}
