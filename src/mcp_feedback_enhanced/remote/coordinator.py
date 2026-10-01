#!/usr/bin/env python3
"""Background coordinator that mirrors one feedback session to a remote provider.

Responsibilities:
- run the provider conversation (open, wait for the reply, close) as background work of
  exactly one session, so the local window never waits for the network;
- isolate every provider failure: nothing raised here may reach the local flow;
- report status changes (connecting, waiting, unavailable, answered) through a callback;
- hand an accepted reply to the session through the shared first-success arbitration;
- finalize within a bounded time on every end path: a short bound, except for a remote
  answer, whose outcome marking gets a longer one (the user is not waiting for the Agent
  there, and the connection may be slow).

Limitations:
- one coordinator serves one session and one conversation; it is never reused;
- it must be created, started and finalized on the same running event loop;
- it never inspects provider internals: provider-specific behavior stays in the provider.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..debug import server_debug_log as debug_log
from .channel import RemoteChannel
from .models import (
    RemoteChannelError,
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    RemoteState,
    RemoteStatus,
)


# Source prefix recorded by the session arbitration for remote replies.
REMOTE_SOURCE_PREFIX = "remote:"

# Backoff between attempts to open the conversation when the provider is unreachable.
DEFAULT_OPEN_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)

# Time budget of finalize(): closing the conversation must never delay the CLI exit
# noticeably, so the provider close is cut off after this many seconds.
DEFAULT_FINALIZE_TIMEOUT_SECONDS = 5.0

# Longer close budget when the answer came from the remote channel: the user is away from
# the computer and relies on the conversation showing the outcome, which can take a while
# behind a slow connection (every request may stall for about 10 s). A local answer keeps
# the short budget above because there the user is waiting for the Agent to continue.
DEFAULT_REMOTE_FINALIZE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SubmitResult:
    """Answer of the session arbitration for a remote reply."""

    accepted: bool
    winner: str | None = None


# Hands a reply to the session: (reply, source) -> arbitration result.
SubmitCallback = Callable[[RemoteReply, str], Awaitable[SubmitResult]]
# Receives every distinct status change.
StatusCallback = Callable[[RemoteStatus], None]


class RemoteSessionCoordinator:
    """Run one remote conversation in the background and finalize it on demand.

    Notes:
    - ``start()`` returns immediately; the connecting status is reported synchronously
      and everything else happens in a task;
    - ``finalize()`` is idempotent and never raises (except cancellation of the caller);
      the provider close is bounded by ``finalize_timeout``, or by the longer
      ``remote_finalize_timeout`` when the answer came from the remote channel.
    """

    def __init__(
        self,
        channel: RemoteChannel,
        request: RemoteRequest,
        *,
        submit: SubmitCallback,
        report_status: StatusCallback,
        finalize_timeout: float = DEFAULT_FINALIZE_TIMEOUT_SECONDS,
        remote_finalize_timeout: float = DEFAULT_REMOTE_FINALIZE_TIMEOUT_SECONDS,
        open_retry_delays: Sequence[float] = DEFAULT_OPEN_RETRY_DELAYS,
    ) -> None:
        self._channel = channel
        self._request = request
        self._submit = submit
        self._report_status = report_status
        self._finalize_timeout = finalize_timeout
        self._remote_finalize_timeout = remote_finalize_timeout
        self._open_retry_delays = tuple(open_retry_delays) or (30.0,)
        self._task: asyncio.Task[None] | None = None
        self._handle: RemoteHandle | None = None
        self._status: RemoteStatus | None = None
        self._finalized = False

    @property
    def status(self) -> RemoteStatus | None:
        """The last reported status (None before start())."""
        return self._status

    def start(self) -> None:
        """Begin the background conversation without waiting for the network."""
        if self._task is not None or self._finalized:
            return
        self._report(RemoteState.CONNECTING)
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name=f"remote-channel-{self._request.short_id}"
        )

    async def finalize(self, outcome: RemoteOutcome) -> None:
        """Stop the background work and close the conversation within a bounded time."""
        if self._finalized:
            return
        self._finalized = True

        task, self._task = self._task, None
        if task is not None:
            if not task.done():
                task.cancel()
            # asyncio.wait never raises the task's exception; it only bounds the wait.
            # Unwinding a cancelled task involves no network, so the short bound applies
            # whatever the outcome is.
            await asyncio.wait({task}, timeout=self._finalize_timeout)
            if task.done() and not task.cancelled():
                task.exception()  # Mark retrieved; _run() already isolated everything

        handle = self._handle
        if handle is not None:
            close_timeout = (
                self._remote_finalize_timeout
                if outcome == RemoteOutcome.REMOTE_ANSWERED
                else self._finalize_timeout
            )
            try:
                await asyncio.wait_for(
                    self._channel.close(handle, outcome), timeout=close_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as close_error:
                # Covers provider errors and the bounded timeout alike.
                debug_log(f"遠端會話收尾失敗（忽略）: {type(close_error).__name__}")

        if outcome == RemoteOutcome.LOCAL_ANSWERED:
            self._report(RemoteState.ANSWERED_LOCALLY)
        elif outcome == RemoteOutcome.REMOTE_ANSWERED:
            self._report(RemoteState.ANSWERED_REMOTELY)

    async def _run(self) -> None:
        """Open, wait and deliver; every failure ends in a reported status."""
        try:
            handle = await self._open_with_retry()
            if handle is None:
                return
            self._handle = handle
            self._report(RemoteState.WAITING)

            reply = await self._channel.wait_reply(handle, self._on_provider_state)
            await self._deliver(reply)
        except asyncio.CancelledError:
            raise
        except RemoteChannelError as error:
            debug_log(f"遠端會話失敗: {error.reason}")
            self._report(RemoteState.UNAVAILABLE, error.reason, str(error))
        except Exception as error:
            debug_log(f"遠端會話發生未預期錯誤: {type(error).__name__}")
            self._report(
                RemoteState.UNAVAILABLE, "internal_error", type(error).__name__
            )

    async def _open_with_retry(self) -> RemoteHandle | None:
        """Open the conversation, retrying transient failures until the deadline."""
        attempt = 0
        while True:
            try:
                return await self._channel.open(self._request)
            except asyncio.CancelledError:
                raise
            except RemoteChannelError as error:
                debug_log(
                    f"開啟遠端會話失敗: {error.reason} permanent={error.permanent}"
                )
                self._report(RemoteState.UNAVAILABLE, error.reason, str(error))
                if error.permanent:
                    return None
            except Exception as error:
                # An unexpected provider bug is not worth retrying blindly.
                debug_log(f"開啟遠端會話發生未預期錯誤: {type(error).__name__}")
                self._report(
                    RemoteState.UNAVAILABLE, "internal_error", type(error).__name__
                )
                return None

            delay = self._open_retry_delays[
                min(attempt, len(self._open_retry_delays) - 1)
            ]
            attempt += 1
            # Jitter avoids lockstep retries; not security sensitive.
            delay *= random.uniform(0.8, 1.2)  # noqa: S311
            if time.time() + delay >= self._request.deadline_epoch:
                return None  # The session would be over before the next attempt
            await asyncio.sleep(delay)

    async def _deliver(self, reply: RemoteReply) -> None:
        """Hand the reply to the session arbitration and report the result."""
        source = f"{REMOTE_SOURCE_PREFIX}{self._channel.provider}"
        try:
            result = await self._submit(reply, source)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            debug_log(f"提交遠端回覆失敗: {type(error).__name__}")
            self._report(RemoteState.UNAVAILABLE, "submit_failed", type(error).__name__)
            return

        if result.accepted:
            self._report(RemoteState.ANSWERED_REMOTELY)
        else:
            # Another source won; finalize() reports the session outcome.
            debug_log(f"遠端回覆未被採納，既有來源: {result.winner}")

    def _on_provider_state(self, state: RemoteState, reason: str | None) -> None:
        """Receive recoverable state changes from the provider's wait loop."""
        self._report(state, reason)

    def _report(
        self,
        state: RemoteState,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Publish a status change; duplicates are dropped and errors isolated."""
        status = RemoteStatus(state, self._channel.provider, reason, detail)
        if status == self._status:
            return
        self._status = status
        try:
            self._report_status(status)
        except Exception as error:
            debug_log(f"推送遠端狀態失敗（忽略）: {type(error).__name__}")
