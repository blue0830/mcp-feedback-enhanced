#!/usr/bin/env python3
"""Data types shared by the remote channel coordinator and its providers.

Responsibilities:
- define the vocabulary (outcomes, UI states, request/handle/reply types) used on both
  sides of the ``RemoteChannel`` interface;
- carry provider failures as ``RemoteChannelError`` with a retry classification.

Limitations:
- plain data only: no I/O, no provider-specific behavior;
- nothing here may hold or expose secrets (tokens never appear in these types).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RemoteOutcome(StrEnum):
    """How a session ended, as shown in the remote conversation."""

    REMOTE_ANSWERED = "remote_answered"
    LOCAL_ANSWERED = "local_answered"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class RemoteState(StrEnum):
    """Remote status shown by the local window badge."""

    OFF = "off"
    CONNECTING = "connecting"
    WAITING = "waiting"
    UNAVAILABLE = "unavailable"
    ANSWERED_REMOTELY = "answered_remotely"
    ANSWERED_LOCALLY = "answered_locally"


@dataclass(frozen=True)
class RemoteStatus:
    """One status update pushed to the local window.

    ``reason`` is a short machine-readable code (for example ``auth_failed``) that the
    page maps to localized text; ``detail`` is optional English diagnostics and must
    never contain secrets.
    """

    state: RemoteState
    provider: str | None = None
    reason: str | None = None
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the websocket push and the session replay."""
        return {
            "state": self.state.value,
            "provider": self.provider,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RemoteRequest:
    """What a provider needs to open one remote conversation for one session."""

    session_id: str
    project_directory: str
    summary: str
    deadline_epoch: float  # Wall-clock deadline (epoch seconds) shown to the user
    timeout_seconds: int

    @property
    def short_id(self) -> str:
        """Short session id used in titles and messages for human correlation."""
        return self.session_id[:8]


@dataclass
class RemoteHandle:
    """Opaque provider state for one open conversation.

    The coordinator only stores and hands it back; providers keep whatever they need
    (conversation id, message ids, cursors) in ``state``.
    """

    provider: str
    conversation_id: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteReply:
    """The final reply extracted from the remote conversation."""

    text: str
    author_id: str | None = None
    ignored_attachments: int = 0


class RemoteChannelError(Exception):
    """A provider failure with a retry classification.

    ``permanent`` means retrying cannot help (rejected credentials, missing permission,
    invalid configuration); the coordinator stops contacting the provider then.
    ``reason`` is a short machine-readable code for the status badge. The message must
    never contain secrets.
    """

    def __init__(
        self, message: str, *, permanent: bool = False, reason: str = "error"
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.reason = reason
