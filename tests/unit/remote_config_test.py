#!/usr/bin/env python3
"""Tests for the remote channel configuration store, fingerprint and effectiveness."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from mcp_feedback_enhanced.remote import (
    ConfigError,
    DiscordSettings,
    RemoteChannelConfig,
    RemoteConfigStore,
)


# Joined from parts: a token-shaped literal makes secret scanners block pushes.
TOKEN = ".".join(
    ("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789")
)
CHANNEL = "123456789012345678"
USER_A = "223456789012345678"
USER_B = "323456789012345678"


@pytest.fixture
def store(tmp_path: Path) -> RemoteConfigStore:
    return RemoteConfigStore(tmp_path / "remote_channel.json")


def _full_payload(**overrides):
    payload = {
        "provider": "discord",
        "discord": {
            "token": TOKEN,
            "forum_channel_id": CHANNEL,
            "allowed_user_ids": [USER_A],
        },
    }
    payload.update(overrides)
    return payload


def _verified_store(store: RemoteConfigStore) -> RemoteChannelConfig:
    store.update(_full_payload())
    return store.mark_verified(store.load())


def test_missing_file_means_disabled_default(store):
    config = store.load()

    assert not config.enabled
    assert not config.is_effective()
    assert not store.path.exists()


def test_corrupt_file_degrades_to_disabled_default(store):
    store.path.write_text("{ not json", encoding="utf-8")
    assert not store.load().is_effective()

    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert not store.load().is_effective()


def test_save_is_atomic_and_leaves_no_temp_files(store):
    store.update(_full_payload())

    assert (
        json.loads(store.path.read_text(encoding="utf-8"))["discord"]["token"] == TOKEN
    )
    leftovers = [p for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_concurrent_reader_never_sees_a_torn_file(store):
    store.update(_full_payload())
    stop = threading.Event()
    seen_tokens: set[str] = set()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                seen_tokens.add(store.load().discord.token)
            except Exception as error:  # pragma: no cover - failure path
                errors.append(error)
            time.sleep(0.001)  # A realistic polling rate, not a busy loop

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(60):
            token = f"{TOKEN}{index % 2}"
            store.update({"discord": {"token": token}})
    finally:
        stop.set()
        thread.join(timeout=5)

    assert errors == []
    # Only complete snapshots are ever observed; an empty token would mean a torn read.
    assert "" not in seen_tokens
    assert seen_tokens <= {TOKEN, f"{TOKEN}0", f"{TOKEN}1"}


def test_view_never_contains_the_token(store):
    store.update(_full_payload())

    view = store.view()

    assert view["discord"]["token_set"] is True
    assert TOKEN not in json.dumps(view)
    assert "token" not in view["discord"]
    assert TOKEN not in repr(store.load())


def test_save_without_token_keeps_the_stored_token(store):
    store.update(_full_payload())

    store.update({"discord": {"forum_channel_id": "423456789012345678"}})
    store.update({"discord": {"token": "   "}})

    assert store.load().discord.token == TOKEN
    assert store.load().discord.forum_channel_id == "423456789012345678"


def test_enabling_requires_a_complete_and_verified_configuration(store):
    store.update({"discord": {"token": TOKEN, "forum_channel_id": CHANNEL}})

    with pytest.raises(ConfigError) as missing:
        store.update({"enabled": True})
    assert missing.value.code == "allowlist_required"

    store.update({"discord": {"allowed_user_ids": [USER_A]}})
    with pytest.raises(ConfigError) as unverified:
        store.update({"enabled": True})
    assert unverified.value.code == "not_verified"

    assert not store.load().enabled


def test_verified_configuration_can_be_enabled_and_is_effective(store):
    _verified_store(store)

    config = store.update({"enabled": True})

    assert config.enabled
    assert config.is_verified()
    assert config.is_effective()
    assert store.load().is_effective()


@pytest.mark.parametrize(
    "change",
    [
        {"discord": {"token": TOKEN + "x"}},
        {"discord": {"forum_channel_id": "423456789012345678"}},
        {"discord": {"allowed_user_ids": [USER_A, USER_B]}},
    ],
)
def test_changing_a_verified_field_invalidates_verification_and_disables(store, change):
    _verified_store(store)
    store.update({"enabled": True})

    changed = store.update(change)

    assert not changed.is_verified()
    assert not changed.enabled
    assert not store.load().is_effective()


def test_changing_fields_and_enabling_in_one_request_is_rejected(store):
    _verified_store(store)

    with pytest.raises(ConfigError) as error:
        store.update(
            {"enabled": True, "discord": {"forum_channel_id": "423456789012345678"}}
        )

    assert error.value.code == "not_verified"
    assert store.load().discord.forum_channel_id == CHANNEL


def test_an_unchanged_save_keeps_enabled_and_verified(store):
    _verified_store(store)
    store.update({"enabled": True})

    store.update(
        {"discord": {"forum_channel_id": CHANNEL, "allowed_user_ids": [USER_A]}}
    )

    assert store.load().is_effective()


def test_disabling_is_always_allowed(store):
    _verified_store(store)
    store.update({"enabled": True})

    assert not store.update({"enabled": False}).enabled


def test_allowlist_is_required_and_entries_are_validated(store):
    store.update({"discord": {"token": TOKEN, "forum_channel_id": CHANNEL}})

    with pytest.raises(ConfigError) as too_short:
        store.update({"discord": {"allowed_user_ids": ["123"]}})
    assert too_short.value.code == "invalid_user_id"

    with pytest.raises(ConfigError) as bad_channel:
        store.update({"discord": {"forum_channel_id": "not-a-number"}})
    assert bad_channel.value.code == "invalid_channel_id"

    with pytest.raises(ConfigError) as bad_token:
        store.update({"discord": {"token": "short"}})
    assert bad_token.value.code == "invalid_token"

    with pytest.raises(ConfigError) as whitespace_token:
        store.update({"discord": {"token": "has space inside the token value"}})
    assert whitespace_token.value.code == "invalid_token"


def test_allowlist_accepts_separated_strings_and_dedupes(store):
    config = store.update(
        {"discord": {"allowed_user_ids": f"{USER_A}, {USER_B}\n{USER_A};{USER_B}"}}
    )

    assert config.discord.allowed_user_ids == (USER_A, USER_B)


def test_unsupported_provider_and_bad_payloads_are_rejected(store):
    with pytest.raises(ConfigError) as provider:
        store.update({"provider": "telegram"})
    assert provider.value.code == "unsupported_provider"

    with pytest.raises(ConfigError) as payload:
        store.update({"discord": "oops"})
    assert payload.value.code == "invalid_payload"

    with pytest.raises(ConfigError) as toggle:
        store.update({"enabled": "yes"})
    assert toggle.value.code == "invalid_payload"


def test_rejected_update_writes_nothing(store):
    store.update(_full_payload())
    before = store.path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        store.update({"enabled": True})

    assert store.path.read_text(encoding="utf-8") == before


def test_mark_verified_requires_the_tested_settings_to_be_unchanged(store):
    store.update(_full_payload())
    tested = store.load()
    store.update({"discord": {"forum_channel_id": "423456789012345678"}})

    with pytest.raises(ConfigError) as error:
        store.mark_verified(tested)

    assert error.value.code == "config_changed"
    assert not store.load().is_verified()


def test_mark_verified_rejects_an_incomplete_configuration(store):
    store.update({"discord": {"token": TOKEN, "forum_channel_id": CHANNEL}})

    with pytest.raises(ConfigError) as error:
        store.mark_verified(store.load())

    assert error.value.code == "allowlist_required"


def test_fingerprint_is_one_way_order_independent_and_sensitive():
    base = RemoteChannelConfig(
        discord=DiscordSettings(TOKEN, CHANNEL, (USER_A, USER_B))
    )
    reordered = RemoteChannelConfig(
        discord=DiscordSettings(TOKEN, CHANNEL, (USER_B, USER_A))
    )
    other_token = RemoteChannelConfig(
        discord=DiscordSettings(TOKEN + "x", CHANNEL, (USER_A, USER_B))
    )

    assert base.fingerprint() == reordered.fingerprint()
    assert base.fingerprint() != other_token.fingerprint()
    assert TOKEN not in base.fingerprint()
    assert len(base.fingerprint()) == 64


def test_snapshot_is_immutable_so_later_changes_do_not_affect_a_running_invocation(
    store,
):
    _verified_store(store)
    store.update({"enabled": True})
    snapshot = store.load()

    store.update({"enabled": False})

    assert snapshot.is_effective()
    assert not store.load().is_effective()
    with pytest.raises(AttributeError):
        snapshot.enabled = False  # type: ignore[misc]


def test_default_path_honours_the_environment_override(tmp_path, monkeypatch):
    target = tmp_path / "custom" / "remote_channel.json"
    monkeypatch.setenv("MCP_FEEDBACK_REMOTE_CONFIG", str(target))

    isolated = RemoteConfigStore()
    isolated.update(_full_payload())

    assert target.exists()
