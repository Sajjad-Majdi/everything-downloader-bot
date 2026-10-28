"""Public seats, the monthly outbound guard and the durable store (fake Worker on 127.0.0.1)."""
import asyncio
import os

import pytest
from aiohttp import web
from conftest import MB, yt_info
from test_handlers import OWNER, FakeTG, cb, cfg, fake_thumb, msg

from dlbot import bot as botmod
from dlbot import store as S
from dlbot import stream as streammod
from dlbot import texts as T
from dlbot import ytdl
from dlbot.config import Config
from dlbot.server import BOT, make_app
from dlbot.signing import sign

SECRET = "sec"
GBy = S.GB


class FakeWorker:
    """Same API as store/worker.js: GET /state, POST /sync (seat cap enforced, bytes added)."""

    def __init__(self):
        self.seats: list[int] = []
        self.usage: dict[str, int] = {}
        self.limits: dict = {}
        self.down = False
        self.syncs: list[dict] = []

    def state(self):
        return {"seats": list(self.seats), "usage": dict(self.usage), "limits": dict(self.limits)}

    def app(self):
        async def guard(request):
            if self.down:
                return web.Response(status=503)
            if request.headers.get("x-store-secret") != SECRET:
                return web.json_response({"error": "forbidden"}, status=403)
            return None

        async def get_state(request):
            return await guard(request) or web.json_response(self.state())

        async def sync(request):
            bad = await guard(request)
            if bad:
                return bad
            body = await request.json()
            self.syncs.append(body)
            for uid in body.get("seats_add") or []:
                if uid not in self.seats and len(self.seats) < body["max"]:
                    self.seats.append(uid)
            if body.get("add"):
                self.usage[body["month"]] = self.usage.get(body["month"], 0) + body["add"]
            return web.json_response(self.state() | {"granted": [u for u in body.get("seats_add") or []
                                                                 if u in self.seats]})

        app = web.Application()
        app.router.add_get("/state", get_state)
        app.router.add_post("/sync", sync)
        return app


@pytest.fixture
async def worker(aiohttp_client):
    w = FakeWorker()
    client = await aiohttp_client(w.app())
    w.url = str(client.make_url("")).rstrip("/")
    return w


def make_store(w, seats=4, soft=4.5, hard=4.8, cache=None, secret=SECRET):
    return S.Store(w.url, secret, seats, soft, hard, cache)


@pytest.fixture
def no_thumbs(monkeypatch):
    monkeypatch.setattr(botmod, "EDIT_EVERY", 0.01)
    monkeypatch.setattr(botmod, "make_thumb", fake_thumb)


async def bot_with(w, **kw):
    st = make_store(w, **kw)
    assert await st.load()
    tg = FakeTG()
    return botmod.Bot(cfg(), tg, st), tg, st


# ---- seats -------------------------------------------------------------------------
async def test_four_seats_first_come_then_full(worker):
    b, tg, st = await bot_with(worker)
    for uid in (11, 12, 13, 14):
        await b.handle_update(msg("/start", uid=uid))
    assert tg.texts() == [T.START] * 4
    assert worker.seats == [11, 12, 13, 14]          # each seat went to the Worker at once
    await b.handle_update(msg("/start", uid=15))
    assert tg.texts()[-1] == T.CAPACITY_FULL and 15 not in worker.seats
    # seat holders keep access, the owner never needs a seat
    await b.handle_update(msg("/start", uid=12))
    await b.handle_update(msg("/start", uid=OWNER))
    assert tg.texts()[-2:] == [T.START, T.START]
    assert OWNER not in worker.seats


async def test_allowed_ids_never_take_a_seat(worker):
    st = make_store(worker, seats=1)
    assert await st.load()
    c = cfg()
    c.allowed_ids = {OWNER, 777}
    tg = FakeTG()
    b = botmod.Bot(c, tg, st)
    await b.handle_update(msg("/start", uid=777))
    await b.handle_update(msg("/start", uid=21))
    await b.handle_update(msg("/start", uid=22))
    assert tg.texts() == [T.START, T.START, T.CAPACITY_FULL]
    assert worker.seats == [21]


async def test_seat_count_is_config_and_worker_limit_overrides(worker):
    worker.limits = {"seats": 2}
    b, tg, st = await bot_with(worker, seats=9)
    assert st.max_seats == 2
    for uid in (1, 2, 3):
        await b.handle_update(msg("/start", uid=uid))
    assert tg.texts() == [T.START, T.START, T.CAPACITY_FULL]
    assert Config.from_env({}).seats == 4 and Config.from_env({"SEATS": "9"}).seats == 9


async def test_seats_survive_restart(worker):
    b, tg, st = await bot_with(worker)
    await b.handle_update(msg("/start", uid=31))
    # a new process (Render deploy wiped the disk) reads them back from the Worker
    st2 = make_store(worker)
    assert await st2.load()
    b2 = botmod.Bot(cfg(), FakeTG(), st2)
    assert b2.allowed({"id": 31}) and not b2.allowed({"id": 32})


async def test_callback_from_stranger_is_ignored(worker):
    b, tg, st = await bot_with(worker)
    await b.handle_update(cb("q:zzz:0", uid=99))
    assert tg.texts() == [] and worker.seats == []


# ---- store down / fallback -----------------------------------------------------------
async def test_store_down_at_boot_new_user_waits_owner_works(worker):
    worker.down = True
    st = make_store(worker)
    assert await st.load() is False and not st.loaded
    tg = FakeTG()
    b = botmod.Bot(cfg(), tg, st)
    await b.handle_update(msg("/start", uid=41))
    await b.handle_update(msg("/start", uid=OWNER))
    assert tg.texts() == [T.TRY_LATER, T.START]
    assert st.report()["store"] is False and st.report()["store_ok"] is False


async def test_store_blip_after_load_seats_and_bytes_queue_then_sync(worker):
    b, tg, st = await bot_with(worker)
    worker.down = True
    await b.handle_update(msg("/start", uid=51))      # granted in memory while the Worker is down
    st.add_bytes(5 * MB)
    with pytest.raises(Exception):
        await st.sync()
    assert tg.texts() == [T.START] and b.allowed({"id": 51})
    assert st.used() == 5 * MB and st.pending == 5 * MB and st.pending_seats == [51]
    worker.down = False
    assert st.due()
    await st.sync()
    assert worker.seats == [51] and worker.usage[S.month_now()] == 5 * MB
    assert st.pending == 0 and st.pending_seats == [] and st.used() == 5 * MB


async def test_cancelled_sync_keeps_bytes(worker, monkeypatch):
    st = make_store(worker)
    assert await st.load()
    st.add_bytes(3 * MB)

    async def slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(st, "_req", slow)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(st.sync(), 0.05)
    assert st.pending == 3 * MB and st.inflight == 0 and st.used() == 3 * MB


async def test_local_cache_used_when_worker_down_then_merged(worker, tmp_path):
    cache = str(tmp_path / "store.json")
    st = make_store(worker, cache=cache)
    assert await st.load()
    await st.seat(61)
    st.add_bytes(7 * MB)
    st._write_cache()                                  # bytes not yet sent to the Worker
    # process restarts in the same container, Worker unreachable
    worker.down = True
    st2 = make_store(worker, cache=cache)
    assert st2.loaded and st2.has_seat(61) and st2.used() == 7 * MB
    assert await st2.seat(62) == S.OK                  # still works from the cache
    worker.down = False
    assert await st2.load()
    await st2.sync()
    assert worker.seats == [61, 62] and worker.usage[S.month_now()] == 7 * MB


async def test_wrong_secret_is_not_loaded(worker):
    st = make_store(worker, secret="nope")
    assert await st.load() is False and "403" in st.last_error


async def test_worker_refuses_seat_when_full_elsewhere(worker):
    st = make_store(worker, seats=1)
    assert await st.load()
    worker.seats = [999]                               # another instance gave the last seat
    assert await st.seat(71) == S.FULL and not st.has_seat(71) and st.has_seat(999)


# ---- monthly outbound guard ------------------------------------------------------------
def fake_download(url, spec, tmpdir, progress, index=None, cookie_file=None, limit=0, info=None):
    p = os.path.join(tmpdir, "video.mp4")
    with open(p, "wb") as fh:
        fh.write(b"\0" * 1000)
    return p


async def test_upload_bytes_are_counted(worker, no_thumbs, monkeypatch):
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None, e=None, f=True: yt_info())
    monkeypatch.setattr(ytdl, "download", fake_download)
    b, tg, st = await bot_with(worker)
    await b.handle_update(msg("https://youtu.be/abc"))
    data = [btn["callback_data"] for row in tg.last_markup()["inline_keyboard"] for btn in row
            if btn["text"].startswith("360p")][0]
    await b.handle_update(cb(data))
    assert tg.uploads and st.used() == 1000 + 3    # video + thumbnail


async def test_soft_limit_blocks_others_not_owner(worker, no_thumbs, monkeypatch):
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None, e=None, f=True: yt_info())
    monkeypatch.setattr(ytdl, "download", fake_download)
    worker.usage = {S.month_now(): int(4.6 * GBy)}
    worker.seats = [81]
    b, tg, st = await bot_with(worker)
    assert st.level() == "soft"
    await b.handle_update(msg("https://youtu.be/abc", uid=81))
    assert tg.texts() == [T.MONTH_LIMIT]
    await b.handle_update(msg("/start", uid=81))           # tiny replies still work
    assert tg.texts()[-1] == T.START
    await b.handle_update(msg("https://youtu.be/abc"))      # the owner still gets the menu
    assert tg.texts()[-1].endswith(T.CHOOSE)


async def test_hard_limit_blocks_everyone(worker, monkeypatch):
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None, e=None, f=True: yt_info())
    worker.usage = {S.month_now(): int(4.85 * GBy)}
    b, tg, st = await bot_with(worker)
    await b.handle_update(msg("https://youtu.be/abc"))
    await b.handle_update(msg("/start"))
    assert tg.texts() == [T.MONTH_LIMIT, T.START]


async def test_big_file_that_would_cross_the_line_is_blocked(worker, no_thumbs, monkeypatch):
    monkeypatch.setattr(ytdl, "extract", lambda url, c=None, e=None, f=True: yt_info())
    worker.usage = {S.month_now(): int(4.5 * GBy) - 20 * MB}
    worker.seats = [82]
    b, tg, st = await bot_with(worker)
    await b.handle_update(msg("https://youtu.be/abc", uid=82))   # under the line: menu shows
    data = [btn["callback_data"] for row in tg.last_markup()["inline_keyboard"] for btn in row
            if btn["text"].startswith("720p")][0]                 # 60+10 MB would cross 4.5 GB
    await b.handle_update(cb(data, uid=82))
    assert tg.texts()[-1] == T.MONTH_LIMIT


async def test_month_rollover_resets_usage(worker, monkeypatch):
    worker.usage = {"2026-10": int(4.9 * GBy)}
    monkeypatch.setattr(S, "month_now", lambda: "2026-10")
    st = make_store(worker)
    assert await st.load() and st.level() == "hard"
    monkeypatch.setattr(S, "month_now", lambda: "2026-11")
    assert st.used() == 0 and st.level() == "ok"


async def test_dl_link_guard_and_meter(worker, aiohttp_client, monkeypatch):
    st = make_store(worker)
    assert await st.load()
    c = cfg()
    streamer = streammod.Streamer(c, st)

    async def fake_proxy(request, f, name, via=None):
        resp = web.StreamResponse()
        await resp.prepare(request)
        await streamer.write(request, resp, b"x" * 5000)
        await resp.write_eof()
        return resp

    monkeypatch.setattr(streamer, "proxy", fake_proxy)
    monkeypatch.setattr(ytdl, "resolve", lambda u, f, i=None, c=None: {"url": "https://cdn.example/v.mp4"})
    monkeypatch.setattr(streammod, "LOCAL_HOSTS", ())     # the test client is local; count it here
    app = web.Application()
    app.router.add_get("/dl/{token}", streamer.handle)
    client = await aiohttp_client(app)
    tok = lambda w: sign(c.link_key, {"u": "https://youtu.be/abc", "f": "18", "i": None, "t": "t", "w": w}, 60)
    r = await client.get(f"/dl/{tok(5)}")
    assert r.status == 200 and len(await r.read()) == 5000 and st.used() == 5000
    worker.usage = {S.month_now(): int(4.6 * GBy)}
    await st.load()
    assert (await client.get(f"/dl/{tok(5)}")).status == 503        # soft: not for others
    assert (await client.get(f"/dl/{tok(OWNER)}")).status == 200    # the owner still can


async def test_health_shows_store(worker, aiohttp_client, monkeypatch):
    c = cfg()
    c.store_url, c.store_secret = worker.url, SECRET
    app = make_app(c, FakeTG())
    client = await aiohttp_client(app)
    st = app[BOT].store
    assert await st.load()
    h = await (await client.get("/health")).json()
    assert h["store"] is True and h["seats"] == "0/4" and h["out_level"] == "ok"
    assert h["out_soft_mb"] == 4500 and h["out_hard_mb"] == 4800 and h["month"] == S.month_now()


def test_config_owner_always_allowed_and_store_switch():
    c = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:abc", "ALLOWED_USER_IDS": "5"})
    assert c.owner_id == OWNER and c.allowed_ids == {OWNER, 5}
    assert c.store_url.startswith("https://downloader-store.") and len(c.store_secret) == 64
    assert Config.from_env({"STORE_URL": "off"}).store_url == ""
    assert (c.out_soft_gb, c.out_hard_gb) == (4.5, 4.8)
