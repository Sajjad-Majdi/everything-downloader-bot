"""Tiny Telegram Bot API client on aiohttp (no heavy framework)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("tg")


class CountingReader(io.BufferedReader):
    """File reader that counts bytes read (aiohttp reads upload bodies through .read())."""

    def __init__(self, path: str, progress: dict):
        super().__init__(io.FileIO(path, "rb"))
        self._progress = progress
        progress["up"] = 0
        progress["up_total"] = os.path.getsize(path)

    def read(self, n=-1):
        # aiohttp asks for 64 KiB per executor hop; 1 MiB means ~16x fewer hops, which
        # matters on a 0.1 CPU host. aiohttp slices oversize chunks safely.
        data = super().read(max(n, 1 << 20) if n and n > 0 else n)
        self._progress["up"] += len(data)
        if not data or self._progress["up"] >= self._progress["up_total"]:
            self._progress.setdefault("t_up_done", time.monotonic())
        return data


class TgError(Exception):
    def __init__(self, method: str, desc: str, code: int | None = None):
        super().__init__(f"{method}: {desc}")
        self.desc = desc
        self.code = code


class Telegram:
    def __init__(self, token: str, base: str = "https://api.telegram.org", session: aiohttp.ClientSession | None = None):
        self.token = token
        self.base = base
        self._session = session

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=120))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, **params):
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
        s = await self.session()
        async with s.post(f"{self.base}/bot{self.token}/{method}", data=data) as r:
            body = await r.json(content_type=None)
        if not body.get("ok"):
            raise TgError(method, body.get("description", "unknown"), body.get("error_code"))
        return body["result"]

    # Upload watchdog. Measured on Render (2026-10-20): some keep-alive connections to
    # api.telegram.org crawl at ~50 KB/s for minutes (a 22 MB video took 5-8 min) while a
    # fresh connection does ~10 MB/s. So every file upload gets its own connection, and a
    # stalled one is dropped and retried on a new connection.
    STALL_AFTER = 8.0          # judge the rate only after this many seconds
    MIN_RATE = 400 * 1024      # bytes/s over the last window; below this the attempt is retried
    WINDOW = 6.0
    ATTEMPTS = 3

    def _form(self, chat_id, video, is_file, caption, thumb, progress, field, extra, handles):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if field == "video":
            form.add_field("supports_streaming", "true")
        if caption:
            form.add_field("caption", caption[:1024])
        for k, v in extra.items():
            if v is not None:
                form.add_field(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
        if thumb:
            th = open(thumb, "rb")
            handles.append(th)
            form.add_field("thumbnail", th, filename="thumb.jpg", content_type="image/jpeg")
        if is_file:
            fh = CountingReader(video, progress)
            handles.append(fh)
            form.add_field(field, fh, filename=os.path.basename(video), content_type="video/mp4")
        else:
            form.add_field(field, video)
        return form

    async def send_video(self, chat_id: int, video: str, *, is_file: bool, caption: str = "",
                         thumb: str | None = None, progress: dict | None = None, method: str = "sendVideo",
                         **extra):
        """sendVideo as multipart. `video` is a local path (is_file) or a URL / file_id.

        A local file is streamed from disk (never read whole into RAM); `progress["up"]`
        counts the bytes sent so the status message can show a real upload %.
        """
        field = "document" if method == "sendDocument" else "video"
        progress = progress if progress is not None else {}
        url = f"{self.base}/bot{self.token}/{method}"
        if not is_file:
            handles: list = []
            try:
                s = await self.session()
                async with s.post(url, data=self._form(chat_id, video, False, caption, thumb, progress, field,
                                                       extra, handles)) as r:
                    body = await r.json(content_type=None)
            finally:
                for h in handles:
                    h.close()
            return self._result(method, body)
        last_exc: Exception | None = None
        for attempt in range(1, self.ATTEMPTS + 1):
            progress.pop("t_up_done", None)
            progress["attempts"] = attempt
            handles = []
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=300))
            try:
                form = self._form(chat_id, video, True, caption, thumb, progress, field, extra, handles)

                async def post():
                    async with session.post(url, data=form) as r:
                        return await r.json(content_type=None)

                task = asyncio.ensure_future(post())
                stalled = await self._watch(task, progress)
                if not stalled:
                    return self._result(method, await task)
                log.warning("upload stalled at %s/%s bytes (attempt %d); retrying on a new connection",
                            progress.get("up"), progress.get("up_total"), attempt)
                last_exc = TgError(method, "upload stalled")
            finally:
                for h in handles:
                    h.close()
                await session.close()
        raise last_exc or TgError(method, "upload failed")

    async def _watch(self, task: asyncio.Future, progress: dict) -> bool:
        """Wait for the upload; return True (and cancel it) if the body stalls."""
        t0 = time.monotonic()
        samples = [(t0, 0)]
        while not task.done():
            await asyncio.wait({task}, timeout=1.0)
            if task.done() or progress.get("t_up_done"):
                break
            now, up = time.monotonic(), progress.get("up", 0)
            samples.append((now, up))
            samples = [x for x in samples if now - x[0] <= self.WINDOW + 1.0]
            if now - t0 >= self.STALL_AFTER and now - samples[0][0] >= self.WINDOW - 0.5:
                rate = (up - samples[0][1]) / (now - samples[0][0])
                if rate < self.MIN_RATE:
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
                    return True
        return False

    @staticmethod
    def _result(method: str, body: dict):
        if not body.get("ok"):
            raise TgError(method, body.get("description", "unknown"), body.get("error_code"))
        return body["result"]

    async def call_with_file(self, method: str, field: str, path: str, content_type: str, **params):
        """Multipart call with one attached file (sent as attach://<field>)."""
        s = await self.session()
        form = aiohttp.FormData()
        for k, v in params.items():
            if v is not None:
                form.add_field(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
        with open(path, "rb") as fh:
            form.add_field(field, fh, filename=os.path.basename(path), content_type=content_type)
            async with s.post(f"{self.base}/bot{self.token}/{method}", data=form) as r:
                body = await r.json(content_type=None)
        if not body.get("ok"):
            raise TgError(method, body.get("description", "unknown"), body.get("error_code"))
        return body["result"]

    # thin helpers -----------------------------------------------------------
    async def send(self, chat_id, text, reply_markup=None, reply_to=None):
        return await self.call(
            "sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup,
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
            link_preview_options={"is_disabled": True},
        )

    async def edit(self, chat_id, message_id, text, reply_markup=None):
        try:
            return await self.call(
                "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=reply_markup, link_preview_options={"is_disabled": True},
            )
        except TgError as e:
            if "not modified" in e.desc:
                return None
            raise

    async def delete(self, chat_id, message_id):
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TgError:
            pass

    async def answer(self, callback_id, text=None):
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TgError:
            pass


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows of (text, callback_data) or (text, 'url:https://...')."""
    out = []
    for row in rows:
        r = []
        for text, data in row:
            if data.startswith("url:"):
                r.append({"text": text, "url": data[4:]})
            else:
                r.append({"text": text, "callback_data": data})
        out.append(r)
    return {"inline_keyboard": out}
