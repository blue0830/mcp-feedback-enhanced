#!/usr/bin/env python3
"""Discord connection check behind the settings "test" button.

Responsibilities:
- verify, in order and with a result per step: the bot token, the forum channel (exists
  and is a forum), the ability to create a post, and that an allowlisted user can reply
  with readable text within the time limit;
- archive the test post afterwards, also when a step failed or the check was cancelled.

Limitations:
- the check needs a human: it waits (up to ``reply_timeout``, 120 s by default) for a
  reply from an allowlisted account in the test post;
- the final archive step is optional: failing it leaves an open test post but does not
  fail the check;
- it reuses the session provider's reply rules (``ReplyInspector`` / ``ThreadPoller``), so
  a passing check means real sessions will recognize the same replies;
- it never stores anything itself: the verification fingerprint is written by the
  caller after a fully passing run.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from ..debug import server_debug_log as debug_log
from .check import ConnectionCheckRun
from .config import DiscordSettings
from .discord_attachments import AttachmentReader
from .discord_channel import (
    ClientFactory,
    create_forum_post,
    default_client_factory,
    first_message_id_of,
    try_request,
)
from .discord_client import DiscordApiClient
from .discord_format import (
    TEST_FINISHED_NOTE,
    TEST_FINISHED_STATUS,
    TEST_RECEIPT,
    TEST_RECEIPT_PARTIAL,
    TEST_SUMMARY,
    build_post,
    waiting_status,
    with_status,
)
from .discord_replies import ReplyInspector, ThreadPoller
from .models import RemoteChannelError, RemoteRequest, RemoteState


REPLY_TIMEOUT_SECONDS = 120.0
TEST_POLL_INTERVAL_SECONDS = 2.0
# Discord channel type of a forum channel.
GUILD_FORUM_TYPE = 15
# Bound for the cleanup that runs even when the check failed or was cancelled.
CLEANUP_TIMEOUT_SECONDS = 4.0

STEP_CREDENTIALS = "token"
STEP_CHANNEL = "channel"
STEP_POST = "post"
STEP_REPLY = "reply"
STEP_ARCHIVE = "archive"


class DiscordConnectionChecker:
    """``ConnectionChecker`` for Discord."""

    provider = "discord"
    step_ids = (STEP_CREDENTIALS, STEP_CHANNEL, STEP_POST, STEP_REPLY, STEP_ARCHIVE)
    optional_steps = (STEP_ARCHIVE,)

    def __init__(
        self,
        settings: DiscordSettings,
        *,
        client_factory: ClientFactory = default_client_factory,
        reader: AttachmentReader | None = None,
        reply_timeout: float = REPLY_TIMEOUT_SECONDS,
        poll_interval: float = TEST_POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._reader = reader or AttachmentReader()
        self._reply_timeout = reply_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    async def run(self, check: ConnectionCheckRun) -> None:
        """Run all steps; progress and failures are recorded on ``check``."""
        client = self._client_factory(self._settings.token)
        thread_id: str | None = None
        first_message_id = ""
        embed: dict | None = None
        try:
            if not await self._check_token(client, check):
                return
            if not await self._check_channel(client, check):
                return

            created = await self._create_post(client, check)
            if created is None:
                return
            thread_id, first_message_id, embed = created

            if not await self._await_reply(client, check, thread_id, first_message_id):
                return
            await self._archive(client, check, thread_id, first_message_id, embed)
            thread_id = None  # Archived: nothing left for the cleanup below
        finally:
            try:
                if thread_id is not None:
                    await asyncio.wait_for(
                        self._cleanup(client, thread_id, first_message_id, embed),
                        timeout=CLEANUP_TIMEOUT_SECONDS,
                    )
            except Exception as cleanup_error:
                debug_log(f"清理測試帖失敗（忽略）: {type(cleanup_error).__name__}")
            finally:
                await client.aclose()

    async def _check_token(
        self, client: DiscordApiClient, check: ConnectionCheckRun
    ) -> bool:
        check.begin(STEP_CREDENTIALS)
        try:
            me = await client.request("GET", "/users/@me")
        except RemoteChannelError as error:
            check.fail(STEP_CREDENTIALS, error.reason, str(error))
            return False
        name = me.get("username") if isinstance(me, dict) else None
        check.succeed(STEP_CREDENTIALS, str(name) if name else None)
        return True

    async def _check_channel(
        self, client: DiscordApiClient, check: ConnectionCheckRun
    ) -> bool:
        check.begin(STEP_CHANNEL)
        try:
            channel = await client.request(
                "GET", f"/channels/{self._settings.forum_channel_id}"
            )
        except RemoteChannelError as error:
            check.fail(STEP_CHANNEL, error.reason, str(error))
            return False
        if not isinstance(channel, dict) or channel.get("type") != GUILD_FORUM_TYPE:
            check.fail(STEP_CHANNEL, "not_forum")
            return False
        name = channel.get("name")
        check.succeed(STEP_CHANNEL, str(name) if name else None)
        return True

    async def _create_post(
        self, client: DiscordApiClient, check: ConnectionCheckRun
    ) -> tuple[str, str, dict] | None:
        check.begin(STEP_POST)
        now = self._clock()
        request = RemoteRequest(
            session_id=uuid.uuid4().hex,
            project_directory="remote settings",
            summary=TEST_SUMMARY,
            deadline_epoch=now + self._reply_timeout,
            timeout_seconds=int(self._reply_timeout),
        )
        payload = build_post(
            request, self._settings.allowed_user_ids, waiting_status(now)
        )
        try:
            response = await create_forum_post(
                client, self._settings.forum_channel_id, payload
            )
        except RemoteChannelError as error:
            check.fail(STEP_POST, error.reason, str(error))
            return None
        check.succeed(STEP_POST)
        return str(response["id"]), first_message_id_of(response), payload.embed

    async def _await_reply(
        self,
        client: DiscordApiClient,
        check: ConnectionCheckRun,
        thread_id: str,
        first_message_id: str,
    ) -> bool:
        check.begin(STEP_REPLY)
        check.reply_timeout_seconds = int(self._reply_timeout)
        last_issue: list[str | None] = [None]

        def remember(state: RemoteState, reason: str | None) -> None:
            last_issue[0] = reason if state == RemoteState.UNAVAILABLE else None

        poller = ThreadPoller(
            client,
            thread_id,
            first_message_id,
            ReplyInspector(self._settings.allowed_user_ids, self._reader),
            poll_interval=self._poll_interval,
            sleep=self._sleep,
            on_state=remember,
            fail_on_empty_content=True,
            receipts=(TEST_RECEIPT, TEST_RECEIPT_PARTIAL),
        )
        try:
            await asyncio.wait_for(poller.next_reply(), timeout=self._reply_timeout)
        except TimeoutError:
            # A wait that ended while Discord was failing is a connectivity problem.
            check.fail(STEP_REPLY, last_issue[0] or "timeout")
            return False
        except RemoteChannelError as error:
            check.fail(STEP_REPLY, error.reason, str(error))
            return False
        check.succeed(STEP_REPLY)
        return True

    async def _archive(
        self,
        client: DiscordApiClient,
        check: ConnectionCheckRun,
        thread_id: str,
        first_message_id: str,
        embed: dict,
    ) -> None:
        check.begin(STEP_ARCHIVE)
        try:
            await self._finish_post(client, thread_id, first_message_id, embed)
        except RemoteChannelError as error:
            check.fail(STEP_ARCHIVE, error.reason, str(error))
            return
        check.succeed(STEP_ARCHIVE)

    @staticmethod
    async def _finish_post(
        client: DiscordApiClient,
        thread_id: str,
        first_message_id: str,
        embed: dict | None,
    ) -> None:
        """Mark the test as finished, then archive. Raises if archiving fails."""
        messages_path = f"/channels/{thread_id}/messages"
        jobs = [
            try_request(
                client,
                "POST",
                messages_path,
                {"content": TEST_FINISHED_NOTE, "allowed_mentions": {"parse": []}},
            )
        ]
        if embed is not None and first_message_id:
            jobs.append(
                try_request(
                    client,
                    "PATCH",
                    f"{messages_path}/{first_message_id}",
                    {"embeds": [with_status(embed, TEST_FINISHED_STATUS)]},
                )
            )
        await asyncio.gather(*jobs)
        await client.request(
            "PATCH", f"/channels/{thread_id}", json_body={"archived": True}
        )

    async def _cleanup(
        self,
        client: DiscordApiClient,
        thread_id: str,
        first_message_id: str,
        embed: dict | None,
    ) -> None:
        """Archive the test post after a failure or cancellation; errors are ignored."""
        try:
            await self._finish_post(client, thread_id, first_message_id, embed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            debug_log(f"歸檔測試帖失敗（忽略）: {type(error).__name__}")
