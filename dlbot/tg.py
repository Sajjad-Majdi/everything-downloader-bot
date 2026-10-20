"""Tiny Telegram Bot API client on aiohttp (no heavy framework)."""
from __future__ import annotations

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

    async def send_video(self, chat_id: int, video: str, *, is_file: bool, caption: str = "",
                         thumb: str | None = None, progress: dict | None = None, method: str = "sendVideo",
                         **extra):
        """sendVideo as multipart. `video` is a local path (is_file) or a URL / file_id.

        A local file is streamed from disk (never read whole into RAM); `progress["up"]`
        counts the bytes sent so the status message can show a real upload %.
        """
        s = await self.session()
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        field = "document" if method == "sendDocument" else "video"
        if field == "video":
            form.add_field("supports_streaming", "true")
        if caption:
            form.add_field("caption", caption[:1024])
        for k, v in extra.items():
            if v is not None:
                form.add_field(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
        handles = []
        try:
            if thumb:
                th = open(thumb, "rb")
                handles.append(th)
                form.add_field("thumbnail", th, filename="thumb.jpg", content_type="image/jpeg")
            if is_file:
                fh = CountingReader(video, progress if progress is not None else {})
                handles.append(fh)
                form.add_field(field, fh, filename=os.path.basename(video), content_type="video/mp4")
            else:
                form.add_field(field, video)
            async with s.post(f"{self.base}/bot{self.token}/{method}", data=form) as r:
                body = await r.json(content_type=None)
        finally:
            for h in handles:
                h.close()
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
