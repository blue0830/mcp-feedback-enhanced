#!/usr/bin/env python3
"""Remote channel configuration: storage, validation, fingerprint and effectiveness.

Responsibilities:
- persist the remote channel settings in a dedicated ``remote_channel.json`` (never in
  ``ui_settings.json``), written atomically and readable at any time by other processes;
- keep the bot token write-only: redacted views never contain it and a save that omits
  the token keeps the stored one;
- decide whether remote communication is *effective*: enabled, complete and verified
  (the stored verification fingerprint equals the fingerprint of the current settings).

Limitations:
- the token is stored in plain text in the user's config directory (best effort 0600 on
  POSIX); a system credential store is a possible later upgrade;
- concurrent writers from different processes are last-write-wins; readers always see
  either the old or the new file thanks to the atomic replace;
- reading never raises: any problem yields a disabled default so callers degrade to the
  local-only flow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


CONFIG_DIR_PARTS = (".config", "mcp-feedback-enhanced")
CONFIG_FILE_NAME = "remote_channel.json"
# Overrides the config file location (used by tests and for isolated setups).
CONFIG_PATH_ENV = "MCP_FEEDBACK_REMOTE_CONFIG"

SUPPORTED_PROVIDERS = ("discord",)
DEFAULT_PROVIDER = "discord"

# Discord snowflake ids are 17-20 digit decimal strings.
_SNOWFLAKE_PATTERN = re.compile(r"^\d{17,20}$")
_TOKEN_MIN_LENGTH = 10
_TOKEN_MAX_LENGTH = 256

# Windows denies a read or an os.replace while the other side has the file open, so both
# retry briefly (about 1 second in total for a write, 50 ms for a read).
_READ_ATTEMPTS = 5
_REPLACE_ATTEMPTS = 40
_RETRY_SLEEP_SECONDS = 0.025


class ConfigError(ValueError):
    """A rejected configuration change with a machine-readable code for the UI."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class DiscordSettings:
    """Discord-specific settings.

    ``channel_id`` is either a forum channel (needs a Community server) or an ordinary
    text channel; the provider detects which one it is when it connects.
    """

    token: str = ""
    channel_id: str = ""
    allowed_user_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        # Keep the token out of accidental logging of the dataclass.
        token_state = "<set>" if self.token else "<unset>"
        return (
            f"DiscordSettings(token={token_state}, "
            f"channel_id={self.channel_id!r}, "
            f"users={len(self.allowed_user_ids)})"
        )


@dataclass(frozen=True)
class RemoteChannelConfig:
    """Immutable snapshot of the remote channel settings.

    A snapshot is read once per ``feedback-cli`` invocation and never mutated, so a
    configuration change made while an invocation is waiting cannot affect it.
    """

    enabled: bool = False
    provider: str = DEFAULT_PROVIDER
    verified_fingerprint: str = ""
    discord: DiscordSettings = field(default_factory=DiscordSettings)

    def fingerprint(self) -> str:
        """One-way digest of (provider, token, channel id, sorted allowlist)."""
        material = json.dumps(
            {
                "provider": self.provider,
                "token": self.discord.token,
                "channel": self.discord.channel_id,
                "users": sorted(self.discord.allowed_user_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def missing_field(self) -> str | None:
        """Return the code of the first missing required field, or None when complete."""
        if self.provider not in SUPPORTED_PROVIDERS:
            return "unsupported_provider"
        if not self.discord.token:
            return "token_required"
        if not self.discord.channel_id:
            return "channel_required"
        if not self.discord.allowed_user_ids:
            return "allowlist_required"
        return None

    def is_complete(self) -> bool:
        """All required fields are present."""
        return self.missing_field() is None

    def is_verified(self) -> bool:
        """The stored verification belongs to exactly these settings."""
        if not self.verified_fingerprint:
            return False
        return hmac.compare_digest(self.verified_fingerprint, self.fingerprint())

    def is_effective(self) -> bool:
        """Remote communication applies to an invocation only in this state."""
        return self.enabled and self.is_complete() and self.is_verified()


def default_config_path() -> Path:
    """Resolve the config file location (environment override first)."""
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*CONFIG_DIR_PARTS) / CONFIG_FILE_NAME


def redact_secret(text: str, *secrets: str) -> str:
    """Replace every occurrence of the given secrets in ``text`` with a mask."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _normalize_user_ids(value: Any) -> tuple[str, ...]:
    """Turn a list or a separated string into a de-duplicated tuple of valid ids."""
    if value is None:
        return ()
    if isinstance(value, str):
        items: list[Any] = [part for part in re.split(r"[\s,;]+", value) if part]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ConfigError("invalid_user_id")

    users: list[str] = []
    for item in items:
        user_id = str(item).strip()
        if not user_id:
            continue
        if not _SNOWFLAKE_PATTERN.match(user_id):
            raise ConfigError("invalid_user_id")
        if user_id not in users:
            users.append(user_id)
    return tuple(users)


class RemoteConfigStore:
    """Read, validate and atomically write ``remote_channel.json``.

    Notes:
    - the path is resolved on every access so tests and environment overrides apply
      immediately;
    - ``update()`` is serialized per process with a lock; other processes are protected
      only by the atomic replace.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The config file this store reads and writes."""
        return self._path_override or default_config_path()

    def _read_text(self) -> str | None:
        """Read the file, retrying briefly when a concurrent atomic replace denies access."""
        for attempt in range(_READ_ATTEMPTS):
            try:
                return self.path.read_text(encoding="utf-8")
            except PermissionError:
                # Windows can deny a read for a moment while os.replace swaps the file.
                if attempt < _READ_ATTEMPTS - 1:
                    time.sleep(_RETRY_SLEEP_SECONDS)
            except OSError:
                return None
        return None

    def load(self) -> RemoteChannelConfig:
        """Read the latest settings; any problem yields a disabled default."""
        text = self._read_text()
        if text is None:
            return RemoteChannelConfig()
        try:
            raw = json.loads(text)
        except ValueError:
            return RemoteChannelConfig()
        if not isinstance(raw, dict):
            return RemoteChannelConfig()

        discord_raw = raw.get("discord")
        if not isinstance(discord_raw, dict):
            discord_raw = {}
        token = discord_raw.get("token")
        channel = discord_raw.get("channel_id")
        users_raw = discord_raw.get("allowed_user_ids")
        users = tuple(str(u) for u in users_raw) if isinstance(users_raw, list) else ()
        provider = raw.get("provider")
        fingerprint = raw.get("verified_fingerprint")

        return RemoteChannelConfig(
            enabled=raw.get("enabled") is True,
            provider=provider if isinstance(provider, str) else DEFAULT_PROVIDER,
            verified_fingerprint=fingerprint if isinstance(fingerprint, str) else "",
            discord=DiscordSettings(
                token=token if isinstance(token, str) else "",
                channel_id=channel if isinstance(channel, str) else "",
                allowed_user_ids=users,
            ),
        )

    def save(self, config: RemoteChannelConfig) -> None:
        """Write the settings atomically (temp file + replace)."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "enabled": config.enabled,
            "provider": config.provider,
            "verified_fingerprint": config.verified_fingerprint,
            "discord": {
                "token": config.discord.token,
                "channel_id": config.discord.channel_id,
                "allowed_user_ids": list(config.discord.allowed_user_ids),
            },
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            # 0600 from the start on POSIX (no-op semantics on Windows).
            descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            self._replace_with_retry(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _replace_with_retry(source: Path, target: Path) -> None:
        """os.replace with a bounded retry: Windows denies it while a reader has the file open."""
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_RETRY_SLEEP_SECONDS)

    def view(self, config: RemoteChannelConfig | None = None) -> dict[str, Any]:
        """Redacted representation for the UI: the token is reported as ``token_set`` only."""
        current = config if config is not None else self.load()
        return {
            "enabled": current.enabled,
            "effective": current.is_effective(),
            "provider": current.provider,
            "providers": list(SUPPORTED_PROVIDERS),
            "verified": current.is_verified(),
            "discord": {
                "token_set": bool(current.discord.token),
                "channel_id": current.discord.channel_id,
                "allowed_user_ids": list(current.discord.allowed_user_ids),
            },
        }

    def update(self, payload: dict[str, Any]) -> RemoteChannelConfig:
        """Apply a user edit, enforce the invariants and persist the result.

        Invariants:
        - a missing or empty token keeps the stored token;
        - any change of provider, token, channel or allowlist invalidates the
          verification and (unless the request itself asks for enabling, which is then
          rejected) switches the feature off;
        - enabling requires a complete configuration and a matching verification.

        Raises:
            ConfigError: the edit is rejected; nothing is written.
        """
        if not isinstance(payload, dict):
            raise ConfigError("invalid_payload")

        with self._lock:
            current = self.load()

            provider = payload.get("provider", current.provider)
            if provider not in SUPPORTED_PROVIDERS:
                raise ConfigError("unsupported_provider")

            discord_payload = payload.get("discord", {})
            if not isinstance(discord_payload, dict):
                raise ConfigError("invalid_payload")

            token = current.discord.token
            new_token = discord_payload.get("token")
            if isinstance(new_token, str) and new_token.strip():
                new_token = new_token.strip()
                if not _TOKEN_MIN_LENGTH <= len(new_token) <= _TOKEN_MAX_LENGTH or any(
                    char.isspace() for char in new_token
                ):
                    raise ConfigError("invalid_token")
                token = new_token

            channel = current.discord.channel_id
            if "channel_id" in discord_payload:
                raw_channel = discord_payload["channel_id"]
                channel = str(raw_channel).strip() if raw_channel is not None else ""
                if channel and not _SNOWFLAKE_PATTERN.match(channel):
                    raise ConfigError("invalid_channel_id")

            users = current.discord.allowed_user_ids
            if "allowed_user_ids" in discord_payload:
                users = _normalize_user_ids(discord_payload["allowed_user_ids"])

            candidate = replace(
                current,
                provider=provider,
                discord=DiscordSettings(
                    token=token, channel_id=channel, allowed_user_ids=users
                ),
            )

            changed = candidate.fingerprint() != current.fingerprint()
            verified = "" if changed else current.verified_fingerprint

            enabled_requested = payload.get("enabled")
            if enabled_requested is not None and not isinstance(
                enabled_requested, bool
            ):
                raise ConfigError("invalid_payload")

            if enabled_requested is True:
                missing = candidate.missing_field()
                if missing:
                    raise ConfigError(missing)
                if verified != candidate.fingerprint():
                    raise ConfigError("not_verified")
                enabled = True
            elif enabled_requested is False:
                enabled = False
            else:
                # Untouched toggle: keep it, but never stay on after losing verification.
                enabled = current.enabled and not changed

            result = replace(candidate, enabled=enabled, verified_fingerprint=verified)
            self.save(result)
            return result

    def mark_verified(self, tested: RemoteChannelConfig) -> RemoteChannelConfig:
        """Store the verification fingerprint for a configuration that just passed the test.

        The fingerprint is stored only if the file still holds the tested settings, so an
        edit made while the test was running cannot be marked as verified by accident.

        Raises:
            ConfigError: ``config_changed`` when the settings changed during the test,
            or the code of a missing required field.
        """
        with self._lock:
            current = self.load()
            missing = tested.missing_field()
            if missing:
                raise ConfigError(missing)
            if current.fingerprint() != tested.fingerprint():
                raise ConfigError("config_changed")
            result = replace(current, verified_fingerprint=current.fingerprint())
            self.save(result)
            return result
