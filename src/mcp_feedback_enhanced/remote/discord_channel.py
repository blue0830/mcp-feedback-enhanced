#!/usr/bin/env python3
"""Discord provider: one thread per feedback session, in a forum or a text channel.

Responsibilities:
- ``open``: start the conversation (title, first message with summary, mentions and
  deadline); the thread id doubles as the conversation id. The kind of the configured
  channel decides how: a forum channel gets one forum post, an ordinary text channel gets
  one public thread with the first message posted inside it;
- ``wait_reply``: poll the thread for the first usable reply of an allowlisted user and
  keep the first message's last-alive timestamp fresh (about once per minute);
- ``close``: show the final outcome in the first message, post a short explanation when
  the outcome is not a remote reply, then archive the thread.

Limitations:
- HTTP polling only: no Gateway connection, no resident process, no inbound port;
- one channel object serves exactly one session and one thread;
- threads in a text channel are public: only a private channel keeps them private;
- a thread whose first message could not be posted is archived but not deleted (the
  creator may archive its threads, deleting needs the Manage Threads permission);
- a forcibly killed process cannot run ``close``: the deadline and the stalled last-alive
  timestamp in the first message make such a thread recognizable;
- the liveness refresh and ``close`` are best effort; their failures never affect the
  reply flow;
- the bot token lives only inside ``DiscordApiClient`` and is never logged or reported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
# Bound for cleanup work that runs while a failure or a cancellation is being handled.
CLEANUP_TIMEOUT_SECONDS = 4.0

# Discord channel types a conversation can be started in.
GUILD_TEXT_TYPE = 0
GUILD_FORUM_TYPE = 15

ClientFactory = Callable[[str], DiscordApiClient]


def default_client_factory(token: str) -> DiscordApiClient:
    return DiscordApiClient(token)


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


async def archive_quietly(client: DiscordApiClient, thread_id: str) -> None:
    """Archive a thread that must not stay open; failures are ignored, the wait is bounded."""
    try:
        await asyncio.wait_for(
            try_request(client, "PATCH", f"/channels/{thread_id}", {"archived": True}),
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        debug_log("Discord 歸檔討論串逾時（忽略）")


@dataclass(frozen=True)
class CreatedPost:
    """Ids of a conversation that Discord just created.

    ``thread_id`` addresses the thread (or forum post) in every later request;
    ``first_message_id`` is the message holding the summary: it is edited to show the
    status and marks the cursor after which replies are read.
    """

    thread_id: str
    first_message_id: str


def unsupported_channel_error() -> RemoteChannelError:
    """The configured channel is neither a text channel nor a forum channel."""
    return RemoteChannelError(
        "The channel is neither a text channel nor a forum channel",
        permanent=True,
        reason="unsupported_channel",
    )


def _bad_response(action: str) -> RemoteChannelError:
    """Discord answered success but the body cannot be used (transient by assumption)."""
    return RemoteChannelError(
        f"Discord returned an unexpected response when {action}",
        permanent=False,
        reason="bad_response",
    )


def _id_of(payload: Any, action: str) -> str:
    """Id found in a creation response, or ``bad_response`` when there is none."""
    identifier = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    if not identifier.isdigit():
        raise _bad_response(action)
    return identifier


async def fetch_channel(client: DiscordApiClient, channel_id: str) -> dict[str, Any]:
    """Look up the configured channel; its ``type`` decides how a conversation starts.

    Raises:
        DiscordApiError: the request was rejected or could not be completed.
        RemoteChannelError: Discord answered success with an unusable body.
    """
    channel = await client.request("GET", f"/channels/{channel_id}")
    if not isinstance(channel, dict):
        raise _bad_response("looking up the channel")
    return channel


def first_message_id_of(response: dict[str, Any]) -> str:
    """Id of the post's first message (equals the post id for forum posts)."""
    message = response.get("message")
    if isinstance(message, dict) and str(message.get("id", "")).isdigit():
        return str(message["id"])
    return str(response["id"])


async def create_forum_post(
    client: DiscordApiClient,
    channel_id: str,
    payload: PostPayload,
    channel: dict[str, Any],
) -> CreatedPost:
    """Create the forum post (title and first message travel in one request).

    A forum that requires tags is handled by retrying once with the first tag listed in
    ``channel`` (the already fetched channel object).
    """
    path = f"/channels/{channel_id}/threads"
    try:
        response = await client.request(
            "POST", path, json_body=payload.body, files=payload.files or None
        )
    except DiscordApiError as error:
        if error.reason != "forum_tag_required":
            raise
        tags = channel.get("available_tags")
        if not tags or not isinstance(tags[0], dict) or "id" not in tags[0]:
            raise
        response = await client.request(
            "POST",
            path,
            json_body={**payload.body, "applied_tags": [str(tags[0]["id"])]},
            files=payload.files or None,
        )

    thread_id = _id_of(response, "creating the post")
    return CreatedPost(thread_id, first_message_id_of(response))


async def create_text_thread(
    client: DiscordApiClient, channel_id: str, payload: PostPayload
) -> CreatedPost:
    """Create a public thread in a text channel and post the first message inside it.

    The thread is created without a starter message so the summary card lives in the
    thread itself: replies are then read with the same cursor logic as in a forum post.
    If the card cannot be posted the empty thread is archived (best effort) before the
    failure is raised, so a retry does not leave an active empty thread behind.
    """
    created = await client.request(
        "POST", f"/channels/{channel_id}/threads", json_body=payload.thread_body
    )
    thread_id = _id_of(created, "creating the thread")
    try:
        message = await client.request(
            "POST",
            f"/channels/{thread_id}/messages",
            json_body=payload.message_body,
            files=payload.files or None,
        )
        first_message_id = _id_of(message, "posting the first message")
    except BaseException:
        await archive_quietly(client, thread_id)
        raise
    return CreatedPost(thread_id, first_message_id)


async def create_post(
    client: DiscordApiClient,
    channel_id: str,
    payload: PostPayload,
    channel: dict[str, Any] | None = None,
) -> CreatedPost:
    """Start the conversation in ``channel_id`` the way its kind requires.

    A forum channel gets one forum post, an ordinary text channel gets one public thread.
    ``channel`` is the channel object when the caller already fetched it (it saves one
    request); otherwise it is looked up here.

    Raises:
        DiscordApiError: the request was rejected or could not be completed.
        RemoteChannelError: the channel kind is unsupported (permanent) or Discord
            answered success with an unusable body.
    """
    if channel is None:
        channel = await fetch_channel(client, channel_id)
    kind = channel.get("type")
    if kind == GUILD_FORUM_TYPE:
        return await create_forum_post(client, channel_id, payload, channel)
    if kind == GUILD_TEXT_TYPE:
        return await create_text_thread(client, channel_id, payload)
    raise unsupported_channel_error()


class DiscordChannel:
    """``RemoteChannel`` implementation backed by Discord threads.

    Notes:
    - one instance serves one session: ``open`` creates the thread, ``close`` archives it;
    - the configured channel may be a forum or a text channel, detected when opening;
    - a failed ``open`` releases its HTTP client, so the coordinator may call it again.
    """

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
        """Create the thread; the client is released again if that fails."""
        client = self._client_factory(self._settings.token)
        try:
            payload = build_post(
                request, self._settings.allowed_user_ids, waiting_status(self._clock())
            )
            created = await create_post(client, self._settings.channel_id, payload)
        except BaseException:
            await client.aclose()
            raise

        self._client = client
        return RemoteHandle(
            provider=PROVIDER_NAME,
            conversation_id=created.thread_id,
            state={
                "first_message_id": created.first_message_id,
                "embed": payload.embed,
                "last_alive_at": self._clock(),
            },
        )

    async def wait_reply(
        self, handle: RemoteHandle, report: StateReporter
    ) -> RemoteReply:
        """Poll the thread until an allowlisted user sends a usable reply."""
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
        """Mark the outcome, explain it when needed and archive the thread (best effort)."""
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
            # Everything is posted before archiving: a thread must not depend on being
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
