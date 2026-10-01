#!/usr/bin/env python3
"""Remote communication for ``feedback-cli``.

Mirrors one CLI feedback session to a remote provider (Discord first) so the user can
answer from another device. The package is only used by the CLI runtimes; when remote
communication is not effective nothing here is instantiated and no request is sent.
"""

from .channel import RemoteChannel, StateReporter
from .config import (
    ConfigError,
    DiscordSettings,
    RemoteChannelConfig,
    RemoteConfigStore,
    default_config_path,
    redact_secret,
)
from .coordinator import (
    REMOTE_SOURCE_PREFIX,
    RemoteSessionCoordinator,
    SubmitResult,
)
from .models import (
    RemoteChannelError,
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    RemoteState,
    RemoteStatus,
)


__all__ = [
    "REMOTE_SOURCE_PREFIX",
    "ConfigError",
    "DiscordSettings",
    "RemoteChannel",
    "RemoteChannelConfig",
    "RemoteChannelError",
    "RemoteConfigStore",
    "RemoteHandle",
    "RemoteOutcome",
    "RemoteReply",
    "RemoteRequest",
    "RemoteSessionCoordinator",
    "RemoteState",
    "RemoteStatus",
    "StateReporter",
    "SubmitResult",
    "default_config_path",
    "redact_secret",
]
