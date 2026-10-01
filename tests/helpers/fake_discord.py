#!/usr/bin/env python3
"""In-process fake of the Discord REST API and CDN for provider tests.

Responsibilities:
- serve the endpoints the Discord remote provider uses (bot identity, channel lookup,
  forum post creation, message listing/creation/editing, thread archiving) on a local
  ephemeral port, recording every request;
- serve attachment downloads with configurable behavior (redirects, errors, big bodies);
- inject failures (429, 5xx, 401, 403) into matching requests.

Limitations:
- behavior is reduced to what the provider relies on; it is not a Discord emulator;
- message ids are increasing integers, so ordering rules can be tested;
- everything runs on the test's event loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer


API_PREFIX = "/api/v10"
# Joined from parts: a token-shaped literal makes secret scanners block pushes.
BOT_TOKEN = ".".join(
    ("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789")
)
BOT_ID = "900000000000000001"
FORUM_ID = "800000000000000001"
ALLOWED_USER = "700000000000000001"
OTHER_USER = "700000000000000002"
_FIRST_ID = 1_100_000_000_000_000_000


@dataclass
class Recorded:
    """One request received by the fake."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    json: Any = None
    files: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class Injection:
    """A canned response for the next matching requests."""

    method: str
    pattern: str
    status: int
    headers: dict[str, str]
    body: Any
    times: int


@dataclass
class Attachment:
    """A downloadable attachment registered with the CDN part of the fake."""

    data: bytes = b""
    status: int = 200
    redirect_to: str | None = None
    content_length_lie: int | None = None


class FakeDiscord:
    """A local fake of Discord (REST under ``/api/v10``, CDN under ``/cdn``)."""

    def __init__(self, token: str = BOT_TOKEN, forum_type: int = 15) -> None:
        self.token = token
        self.forum_type = forum_type
        self.requests: list[Recorded] = []
        self.threads: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, Attachment] = {}
        self.injections: list[Injection] = []
        self.available_tags: list[dict[str, Any]] = []
        self.require_tag = False
        self.cdn_requests: list[Recorded] = []
        self._next_id = _FIRST_ID
        self._server: TestServer | None = None

    # ---- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", API_PREFIX + "/{tail:.*}", self._api)
        app.router.add_get("/cdn/{name:.*}", self._cdn)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()
            self._server = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.port}{API_PREFIX}"

    def cdn_url(self, name: str) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.port}/cdn/{name}"

    # ---- test helpers ----------------------------------------------------------
    def new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def inject(
        self,
        method: str,
        pattern: str,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        times: int = 1,
    ) -> None:
        """Answer the next ``times`` requests matching ``method`` and ``pattern``."""
        self.injections.append(
            Injection(method, pattern, status, headers or {}, body, times)
        )

    def add_user_message(
        self,
        thread_id: str,
        content: str,
        *,
        author_id: str = ALLOWED_USER,
        bot: bool = False,
        message_type: int = 0,
        attachments: list[dict[str, Any]] | None = None,
        webhook_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a message written by someone else than the provider."""
        message: dict[str, Any] = {
            "id": self.new_id(),
            "type": message_type,
            "content": content,
            "author": {
                "id": author_id,
                "bot": bot,
                "username": f"user{author_id[-2:]}",
            },
            "attachments": attachments or [],
        }
        if webhook_id:
            message["webhook_id"] = webhook_id
        self.threads[thread_id]["messages"].append(message)
        return message

    def attachment_meta(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str | None = "text/plain; charset=utf-8",
        filename: str | None = None,
        declared_size: int | None = None,
        url: str | None = None,
        status: int = 200,
        redirect_to: str | None = None,
    ) -> dict[str, Any]:
        """Register CDN content and return the attachment object for a message."""
        self.attachments[name] = Attachment(
            data=data, status=status, redirect_to=redirect_to
        )
        meta: dict[str, Any] = {
            "id": self.new_id(),
            "filename": filename or name,
            "size": len(data) if declared_size is None else declared_size,
            "url": url or self.cdn_url(name),
        }
        if content_type is not None:
            meta["content_type"] = content_type
        return meta

    def last_thread_id(self) -> str | None:
        return next(reversed(self.threads), None)

    def calls(self, method: str, pattern: str) -> list[Recorded]:
        """Recorded API requests matching ``method`` and a regex on the path."""
        return [
            r
            for r in self.requests
            if r.method == method and re.search(pattern, r.path)
        ]

    def bot_messages(self, thread_id: str) -> list[dict[str, Any]]:
        return [
            m
            for m in self.threads[thread_id]["messages"]
            if m["author"]["id"] == BOT_ID
        ]

    # ---- request handling ------------------------------------------------------
    async def _record(self, request: web.Request, prefix: str) -> Recorded:
        path = request.path[len(prefix) :]
        recorded = Recorded(
            request.method, path, dict(request.query), dict(request.headers)
        )
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            try:
                recorded.json = await request.json()
            except ValueError:
                recorded.json = None
        elif content_type.startswith("multipart/"):
            form = await request.post()
            for key, value in form.items():
                # A part arrives as a file object, raw bytes or text depending on its
                # headers; normalize all of them to bytes.
                if hasattr(value, "file"):
                    data = value.file.read()
                elif isinstance(value, (bytes, bytearray)):
                    data = bytes(value)
                else:
                    data = str(value).encode("utf-8")
                if key == "payload_json":
                    recorded.json = json.loads(data.decode("utf-8"))
                else:
                    recorded.files.append((getattr(value, "filename", key), data))
        return recorded

    def _take_injection(self, recorded: Recorded) -> Injection | None:
        for injection in self.injections:
            if (
                injection.times > 0
                and injection.method == recorded.method
                and re.search(injection.pattern, recorded.path)
            ):
                injection.times -= 1
                return injection
        return None

    async def _api(self, request: web.Request) -> web.StreamResponse:
        recorded = await self._record(request, API_PREFIX)
        self.requests.append(recorded)

        injection = self._take_injection(recorded)
        if injection is not None:
            return web.json_response(
                injection.body, status=injection.status, headers=injection.headers
            )
        if request.headers.get("Authorization") != f"Bot {self.token}":
            return web.json_response(
                {"message": "401: Unauthorized", "code": 0}, status=401
            )

        path = recorded.path
        method = recorded.method
        if method == "GET" and path == "/users/@me":
            return web.json_response({"id": BOT_ID, "username": "testbot", "bot": True})

        match = re.fullmatch(r"/channels/(\d+)", path)
        if match:
            return self._channel(method, match[1], recorded)

        match = re.fullmatch(r"/channels/(\d+)/threads", path)
        if match and method == "POST":
            return self._create_post(match[1], recorded)

        match = re.fullmatch(r"/channels/(\d+)/messages", path)
        if match:
            return self._messages(method, match[1], recorded)

        match = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
        if match and method == "PATCH":
            return self._edit_message(match[1], match[2], recorded)

        return web.json_response({"message": "Unknown route", "code": 0}, status=404)

    def _channel(
        self, method: str, channel_id: str, recorded: Recorded
    ) -> web.Response:
        if method == "GET" and channel_id == FORUM_ID:
            return web.json_response(
                {
                    "id": FORUM_ID,
                    "type": self.forum_type,
                    "name": "feedback",
                    "available_tags": self.available_tags,
                    "flags": 16 if self.require_tag else 0,
                }
            )
        thread = self.threads.get(channel_id)
        if thread is None:
            return web.json_response(
                {"message": "Unknown Channel", "code": 10003}, status=404
            )
        if method == "PATCH":
            body = recorded.json or {}
            if "archived" in body:
                thread["archived"] = bool(body["archived"])
            return web.json_response({"id": channel_id, "archived": thread["archived"]})
        return web.json_response({"id": channel_id, "type": 11})

    def _create_post(self, forum_id: str, recorded: Recorded) -> web.Response:
        if forum_id != FORUM_ID:
            return web.json_response(
                {"message": "Unknown Channel", "code": 10003}, status=404
            )
        body = recorded.json or {}
        if self.require_tag and not body.get("applied_tags"):
            return web.json_response(
                {
                    "message": "A tag is required to create a forum post in this channel",
                    "code": 40067,
                },
                status=400,
            )
        thread_id = self.new_id()
        first = dict(body.get("message") or {})
        first.update(
            {
                "id": thread_id,  # For forum posts the first message id equals the post id
                "type": 0,
                "author": {"id": BOT_ID, "bot": True, "username": "testbot"},
            }
        )
        self.threads[thread_id] = {
            "name": body.get("name"),
            "auto_archive_duration": body.get("auto_archive_duration"),
            "applied_tags": body.get("applied_tags"),
            "archived": False,
            "messages": [first],
            "request": recorded,
        }
        return web.json_response(
            {"id": thread_id, "type": 11, "name": body.get("name"), "message": first}
        )

    def _messages(
        self, method: str, thread_id: str, recorded: Recorded
    ) -> web.Response:
        thread = self.threads.get(thread_id)
        if thread is None:
            return web.json_response(
                {"message": "Unknown Channel", "code": 10003}, status=404
            )
        if method == "GET":
            after = int(recorded.query.get("after", "0"))
            limit = int(recorded.query.get("limit", "50"))
            newer = [m for m in thread["messages"] if int(m["id"]) > after][:limit]
            return web.json_response(list(reversed(newer)))
        body = recorded.json or {}
        message = {
            "id": self.new_id(),
            "type": 19 if body.get("message_reference") else 0,
            "content": body.get("content", ""),
            "author": {"id": BOT_ID, "bot": True, "username": "testbot"},
            "attachments": [],
            "request": body,
        }
        thread["messages"].append(message)
        return web.json_response(message)

    def _edit_message(
        self, thread_id: str, message_id: str, recorded: Recorded
    ) -> web.Response:
        thread = self.threads.get(thread_id)
        if thread is None:
            return web.json_response(
                {"message": "Unknown Channel", "code": 10003}, status=404
            )
        for message in thread["messages"]:
            if message["id"] == message_id:
                body = recorded.json or {}
                if "embeds" in body:
                    message["embeds"] = body["embeds"]
                return web.json_response(message)
        return web.json_response(
            {"message": "Unknown Message", "code": 10008}, status=404
        )

    async def _cdn(self, request: web.Request) -> web.StreamResponse:
        recorded = Recorded(
            request.method, request.path, dict(request.query), dict(request.headers)
        )
        self.cdn_requests.append(recorded)
        attachment = self.attachments.get(request.match_info["name"])
        if attachment is None:
            return web.Response(status=404)
        if attachment.redirect_to:
            return web.Response(
                status=302, headers={"Location": attachment.redirect_to}
            )
        if attachment.status != 200:
            return web.Response(status=attachment.status)
        return web.Response(body=attachment.data)
