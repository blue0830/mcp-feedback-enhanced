#!/usr/bin/env python3
"""Finding the final reply in a Discord post by polling.

Responsibilities:
- ``ReplyInspector`` decides whether one message is the final reply (allowlisted human
  author, normal message, usable text), must get a hint, or is ignored;
- ``ThreadPoller`` polls a post over HTTP with a cursor, answers receipts and hints, and
  returns the first usable reply. The session channel and the settings connection check
  share it, so "what counts as a reply" cannot drift apart.

Limitations:
- polling only (no Gateway): about one request every 3 seconds per post, with jitter;
- transient failures are retried with exponential backoff (capped at 30 s) and reported as
  unavailable until a poll succeeds again; permanent failures propagate;
- receipts and hints are best effort: failing to post them never loses or blocks a reply;
- message text is only readable when the bot has the Message Content setting enabled,
  otherwise Discord returns empty text and attachments (reported as a dedicated hint).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..debug import server_debug_log as debug_log
from .discord_attachments import AttachmentError, AttachmentReader
from .discord_client import DiscordApiClient
from .discord_format import HINTS_BY_REASON, RECEIPT, RECEIPT_PARTIAL
from .models import RemoteChannelError, RemoteReply, RemoteState


POLL_INTERVAL_SECONDS = 3.0
POLL_JITTER_RATIO = 0.2
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
PAGE_SIZE = 100

# Discord message types: 0 = default, 19 = reply. Everything else (joins, pins, thread
# notices, commands) is never a reply.
_NORMAL_MESSAGE_TYPES = frozenset({0, 19})

ACTION_IGNORE = "ignore"
ACTION_HINT = "hint"
ACTION_REPLY = "reply"

REASON_NO_TEXT = "no_text"
REASON_MESSAGE_CONTENT = "message_content"


@dataclass(frozen=True)
class Inspection:
    """Verdict on one message: ignore it, answer with a hint, or accept it as the reply."""

    action: str
    text: str = ""
    reason: str | None = None
    ignored_attachments: int = 0
    author_id: str | None = None
    message_id: str | None = None


_IGNORED = Inspection(ACTION_IGNORE)


class ReplyInspector:
    """Apply the authorized-reply rules to single messages."""

    def __init__(
        self, allowed_user_ids: Iterable[str], reader: AttachmentReader
    ) -> None:
        self._allowed = frozenset(allowed_user_ids)
        self._reader = reader

    async def inspect(self, message: Mapping[str, Any]) -> Inspection:
        """Classify one message object returned by the Discord API."""
        author = message.get("author")
        if not isinstance(author, Mapping):
            return _IGNORED
        author_id = str(author.get("id", ""))
        if (
            author_id not in self._allowed
            or author.get("bot")
            or message.get("webhook_id")
            or message.get("type", 0) not in _NORMAL_MESSAGE_TYPES
        ):
            return _IGNORED

        message_id = str(message.get("id", "")) or None
        text = str(message.get("content") or "").strip()
        raw_attachments = message.get("attachments")
        attachments = raw_attachments if isinstance(raw_attachments, list) else []

        try:
            read = await self._reader.read(attachments)
        except AttachmentError as error:
            return Inspection(
                ACTION_HINT,
                reason=error.reason,
                author_id=author_id,
                message_id=message_id,
            )

        usable = "\n\n".join(([text] if text else []) + list(read.texts))
        if not usable.strip():
            # With the Message Content setting off Discord blanks text and attachments
            # alike, which looks exactly like a message with nothing in it.
            nothing_visible = (
                not text and not attachments and not message.get("sticker_items")
            )
            return Inspection(
                ACTION_HINT,
                reason=REASON_MESSAGE_CONTENT if nothing_visible else REASON_NO_TEXT,
                author_id=author_id,
                message_id=message_id,
            )
        return Inspection(
            ACTION_REPLY,
            text=usable,
            ignored_attachments=read.ignored,
            author_id=author_id,
            message_id=message_id,
        )


StateCallback = Callable[[RemoteState, str | None], None]
TickCallback = Callable[[], Awaitable[None]]


class ThreadPoller:
    """Poll one post for the first usable reply of an allowed user.

    ``on_tick`` runs before every poll (the channel uses it for the liveness refresh) and
    must not raise. With ``fail_on_empty_content`` an authorized message with blank text
    and attachments ends the wait with a permanent ``message_content`` failure, which is
    what the settings check wants; the session channel keeps waiting instead.
    """

    def __init__(
        self,
        client: DiscordApiClient,
        thread_id: str,
        first_message_id: str,
        inspector: ReplyInspector,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_state: StateCallback | None = None,
        on_tick: TickCallback | None = None,
        fail_on_empty_content: bool = False,
        receipts: tuple[str, str] = (RECEIPT, RECEIPT_PARTIAL),
    ) -> None:
        self._client = client
        self._thread_id = thread_id
        self._cursor = int(first_message_id)
        self._inspector = inspector
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._on_state = on_state
        self._on_tick = on_tick
        self._fail_on_empty_content = fail_on_empty_content
        # (receipt, receipt when some attachments were ignored)
        self._receipts = receipts

    async def next_reply(self) -> RemoteReply:
        """Return the first usable reply; runs until one arrives or the task is cancelled."""
        failures = 0
        while True:
            await self._sleep(self._next_delay(failures))
            if self._on_tick is not None:
                await self._on_tick()

            try:
                messages = await self._fetch()
            except RemoteChannelError as error:
                if error.permanent:
                    raise
                failures += 1
                self._notify(RemoteState.UNAVAILABLE, error.reason)
                continue

            if failures:
                failures = 0
                self._notify(RemoteState.WAITING, None)

            for message in messages:
                self._cursor = max(self._cursor, int(message["id"]))
                inspection = await self._inspector.inspect(message)
                if inspection.action == ACTION_IGNORE:
                    continue
                if inspection.action == ACTION_HINT:
                    await self._say(inspection, _hint_text(inspection.reason))
                    if (
                        self._fail_on_empty_content
                        and inspection.reason == REASON_MESSAGE_CONTENT
                    ):
                        raise RemoteChannelError(
                            "The message text is empty: enable Message Content for the bot",
                            permanent=True,
                            reason=REASON_MESSAGE_CONTENT,
                        )
                    continue

                receipt, partial_receipt = self._receipts
                await self._say(
                    inspection,
                    partial_receipt if inspection.ignored_attachments else receipt,
                )
                return RemoteReply(
                    text=inspection.text,
                    author_id=inspection.author_id,
                    ignored_attachments=inspection.ignored_attachments,
                )

    def _next_delay(self, failures: int) -> float:
        """Jittered poll interval, or the exponential backoff after failed polls."""
        if failures:
            return BACKOFF_SECONDS[min(failures - 1, len(BACKOFF_SECONDS) - 1)]
        jitter = self._poll_interval * POLL_JITTER_RATIO
        return self._poll_interval + random.uniform(-jitter, jitter)  # noqa: S311

    async def _fetch(self) -> list[dict[str, Any]]:
        """Messages after the cursor, oldest first."""
        page = await self._client.request(
            "GET",
            f"/channels/{self._thread_id}/messages",
            params={"after": str(self._cursor), "limit": PAGE_SIZE},
        )
        if not isinstance(page, list):
            return []
        valid = [
            m for m in page if isinstance(m, dict) and str(m.get("id", "")).isdigit()
        ]
        return sorted(valid, key=lambda m: int(m["id"]))

    def _notify(self, state: RemoteState, reason: str | None) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(state, reason)
        except Exception as callback_error:
            debug_log(f"狀態回呼失敗（忽略）: {type(callback_error).__name__}")

    async def _say(self, inspection: Inspection, text: str) -> None:
        """Post a receipt or hint under the user's message; failures are only logged."""
        body: dict[str, Any] = {"content": text, "allowed_mentions": {"parse": []}}
        if inspection.message_id:
            body["message_reference"] = {
                "message_id": inspection.message_id,
                "fail_if_not_exists": False,
            }
        try:
            await self._client.request(
                "POST", f"/channels/{self._thread_id}/messages", json_body=body
            )
        except asyncio.CancelledError:
            raise
        except Exception as post_error:
            debug_log(f"回覆訊息失敗（忽略）: {type(post_error).__name__}")


def _hint_text(reason: str | None) -> str:
    return HINTS_BY_REASON.get(reason or "", HINTS_BY_REASON[REASON_NO_TEXT])
