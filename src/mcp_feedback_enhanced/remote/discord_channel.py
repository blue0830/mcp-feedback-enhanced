#!/usr/bin/env python3
"""Discord provider: one forum post per feedback session.

Responsibilities:
- ``open``: create the forum post (title, first message with summary, mentions and
  deadline); the post id doubles as the conversation id;
- ``wait_reply``: poll the post for the first usable reply of an allowlisted user and keep
  the first message's last-alive timestamp fresh (about once per minute);
- ``close``: show the final outcome in the first message, post a short explanation when
  the outcome is not a remote reply, then archive the post.

Limitations:
- HTTP polling only: no Gateway connection, no resident process, no inbound port;
- one channel object serves exactly one session and one post;
- a forcibly killed process cannot run ``close``: the deadline and the stalled last-alive
  timestamp in the first message make such a post recognizable;
- the liveness refresh and ``close`` are best effort; their failures never affect the
  reply flow;
- the bot token lives only inside ``DiscordApiClient`` and is never logged or reported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..debug import server_debug_log as debug_log
from .channel import StateReporter
from .config import DiscordSettings
from .discord_attachments import AttachmentReader
from .discord_client import DiscordApiClient, DiscordApiError
from .discord_format import (
    PostPayload,
    build_post,
    outcome_note,
    outcome_status,
    waiting_status,
    with_status,
)
from .discord_replies import POLL_INTERVAL_SECONDS, ReplyInspector, ThreadPoller
from .models import (
    RemoteChannelError,
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
)


PROVIDER_NAME = "discord"
LIVENESS_INTERVAL_SECONDS = 60.0

ClientFactory = Callable[[str], DiscordApiClient]


def default_client_factory(token: str) -> DiscordApiClient:
    return DiscordApiClient(token)


async def create_forum_post(
    client: DiscordApiClient, forum_channel_id: str, payload: PostPayload
) -> dict[str, Any]:
    """Create the forum post and return the decoded response.

    A forum that requires tags is handled by retrying once with its first tag.

    Raises:
        DiscordApiError: the request was rejected or could not be completed.
        RemoteChannelError: Discord answered success with an unusable body.
    """
    path = f"/channels/{forum_channel_id}/threads"
    try:
        response = await client.request(
            "POST", path, json_body=payload.body, files=payload.files or None
        )
    except DiscordApiError as error:
        if error.reason != "forum_tag_required":
            raise
        channel = await client.request("GET", f"/channels/{forum_channel_id}")
        tags = channel.get("available_tags") if isinstance(channel, dict) else None
        if not tags or not isinstance(tags[0], dict) or "id" not in tags[0]:
            raise
        response = await client.request(
            "POST",
            path,
            json_body={**payload.body, "applied_tags": [str(tags[0]["id"])]},
            files=payload.files or None,
        )

    if not isinstance(response, dict) or not str(response.get("id", "")).isdigit():
        raise RemoteChannelError(
            "Discord returned an unexpected response when creating the post",
            permanent=False,
            reason="bad_response",
        )
    return response


def first_message_id_of(response: dict[str, Any]) -> str:
    """Id of the post's first message (equals the post id for forum posts)."""
    message = response.get("message")
    if isinstance(message, dict) and str(message.get("id", "")).isdigit():
        return str(message["id"])
    return str(response["id"])


async def try_request(
    client: DiscordApiClient,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> None:
    """Send a non-critical request; every failure except cancellation is only logged."""
    try:
        await client.request(method, path, json_body=body)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        debug_log(f"Discord 非關鍵請求失敗（忽略）: {type(error).__name__}")


class DiscordChannel:
    """``RemoteChannel`` implementation backed by Discord forum posts."""

    provider = PROVIDER_NAME

    def __init__(
        self,
        settings: DiscordSettings,
        *,
        client_factory: ClientFactory = default_client_factory,
        reader: AttachmentReader | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        liveness_interval: float = LIVENESS_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._reader = reader or AttachmentReader()
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._liveness_interval = liveness_interval
        self._clock = clock
        self._client: DiscordApiClient | None = None

    async def open(self, request: RemoteRequest) -> RemoteHandle:
        """Create the forum post; the client is released again if that fails."""
        client = self._client_factory(self._settings.token)
        try:
            payload = build_post(
                request, self._settings.allowed_user_ids, waiting_status(self._clock())
            )
            response = await create_forum_post(
                client, self._settings.forum_channel_id, payload
            )
        except BaseException:
            await client.aclose()
            raise

        self._client = client
        return RemoteHandle(
            provider=PROVIDER_NAME,
            conversation_id=str(response["id"]),
            state={
                "first_message_id": first_message_id_of(response),
                "embed": payload.embed,
                "last_alive_at": self._clock(),
            },
        )

    async def wait_reply(
        self, handle: RemoteHandle, report: StateReporter
    ) -> RemoteReply:
        """Poll the post until an allowlisted user sends a usable reply."""
        poller = ThreadPoller(
            self._require_client(),
            handle.conversation_id,
            handle.state["first_message_id"],
            ReplyInspector(self._settings.allowed_user_ids, self._reader),
            poll_interval=self._poll_interval,
            sleep=self._sleep,
            on_state=report,
            on_tick=lambda: self._refresh_liveness(handle),
        )
        return await poller.next_reply()

    async def close(self, handle: RemoteHandle, outcome: RemoteOutcome) -> None:
        """Mark the outcome, explain it when needed and archive the post (best effort)."""
        client, self._client = self._client, None
        if client is None:
            return
        thread_id = handle.conversation_id
        messages_path = f"/channels/{thread_id}/messages"
        try:
            jobs = [
                try_request(
                    client,
                    "PATCH",
                    f"{messages_path}/{handle.state['first_message_id']}",
                    {
                        "embeds": [
                            with_status(handle.state["embed"], outcome_status(outcome))
                        ]
                    },
                )
            ]
            note = outcome_note(outcome)
            if note:
                jobs.append(
                    try_request(
                        client,
                        "POST",
                        messages_path,
                        {"content": note, "allowed_mentions": {"parse": []}},
                    )
                )
            # Everything is posted before archiving: a post must not depend on being
            # writable once it is archived.
            await asyncio.gather(*jobs)
            await try_request(
                client, "PATCH", f"/channels/{thread_id}", {"archived": True}
            )
        finally:
            await client.aclose()

    def _require_client(self) -> DiscordApiClient:
        if self._client is None:
            raise RemoteChannelError(
                "The Discord conversation is not open",
                permanent=True,
                reason="not_open",
            )
        return self._client

    async def _refresh_liveness(self, handle: RemoteHandle) -> None:
        """Edit the first message about once a minute; failures are ignored."""
        now = self._clock()
        if now - handle.state["last_alive_at"] < self._liveness_interval:
            return
        # Advance first so a failing edit is not retried on every poll.
        handle.state["last_alive_at"] = now
        client = self._client
        if client is None:
            return
        await try_request(
            client,
            "PATCH",
            f"/channels/{handle.conversation_id}/messages/{handle.state['first_message_id']}",
            {"embeds": [with_status(handle.state["embed"], waiting_status(now))]},
        )
