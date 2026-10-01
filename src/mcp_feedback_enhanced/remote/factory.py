#!/usr/bin/env python3
"""Create the provider objects that match a remote configuration.

Responsibilities:
- map ``config.provider`` to a ``RemoteChannel`` (session mirroring) and to a
  ``ConnectionChecker`` (settings "test" flow).

Limitations:
- providers are imported lazily so that runs without remote communication never load
  (or pay for) provider code;
- ``create_channel`` is only called after ``RemoteChannelConfig.is_effective()`` held;
  ``create_checker`` only needs a complete configuration.
"""

from __future__ import annotations

from .channel import RemoteChannel
from .check import ConnectionChecker
from .config import RemoteChannelConfig
from .models import RemoteChannelError


def _unsupported(config: RemoteChannelConfig) -> RemoteChannelError:
    return RemoteChannelError(
        f"Unsupported remote provider: {config.provider}",
        permanent=True,
        reason="unsupported_provider",
    )


def create_channel(config: RemoteChannelConfig) -> RemoteChannel:
    """Build the channel for ``config`` (raises a permanent RemoteChannelError if unknown)."""
    if config.provider == "discord":
        # Deliberately lazy: providers pull in HTTP code that must stay unloaded when
        # remote communication is off.
        from .discord_channel import DiscordChannel  # noqa: PLC0415

        return DiscordChannel(config.discord)

    raise _unsupported(config)


def create_checker(config: RemoteChannelConfig) -> ConnectionChecker:
    """Build the connection checker for ``config`` (same lazy import rule as above)."""
    if config.provider == "discord":
        from .discord_check import DiscordConnectionChecker  # noqa: PLC0415

        return DiscordConnectionChecker(config.discord)

    raise _unsupported(config)
