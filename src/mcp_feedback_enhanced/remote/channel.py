#!/usr/bin/env python3
"""The minimal provider interface used by the remote session coordinator.

Responsibilities:
- define the three operations a remote provider must offer: open a conversation,
  wait for the final reply (cancellable) and close the conversation with an outcome.

Limitations:
- provider-specific behavior (liveness refresh, receipts, archiving, rate limits) stays
  inside the provider; the coordinator must never special-case a provider;
- ``wait_reply`` must tolerate cancellation at any await point and must handle its own
  transient failures (reporting state changes through ``report``), raising
  ``RemoteChannelError`` only for permanent failures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .models import (
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    RemoteState,
)


# Callback a provider uses inside wait_reply() to report recoverable state changes,
# for example (UNAVAILABLE, "network_error") and later (WAITING, None) after recovery.
StateReporter = Callable[[RemoteState, str | None], None]


@runtime_checkable
class RemoteChannel(Protocol):
    """A remote provider driven through exactly three operations."""

    provider: str

    async def open(self, request: RemoteRequest) -> RemoteHandle:
        """Create the remote conversation for one session.

        Raises RemoteChannelError; ``permanent`` tells whether retrying can help.
        """
        ...

    async def wait_reply(
        self, handle: RemoteHandle, report: StateReporter
    ) -> RemoteReply:
        """Block until an authorized, usable reply arrives and return it.

        Transient failures are retried internally with backoff. Permanent failures raise
        RemoteChannelError. The coroutine is cancelled when the session ends.
        """
        ...

    async def close(self, handle: RemoteHandle, outcome: RemoteOutcome) -> None:
        """Mark the conversation with the final outcome and archive it (best effort).

        The coordinator bounds the call with a timeout and ignores failures.
        """
        ...
