#!/usr/bin/env python3
"""Tests for the remote settings routes: availability, local-origin guard, config and check.

Covered rules:
- windows not hosted by ``feedback-cli`` answer 404 on every remote endpoint and render no card;
- the local-origin guard refuses foreign hosts, cross-site posts and mismatching origins
  (and changes nothing) while accepting loopback names, also through a forwarded port;
- configuration responses never contain the bot token;
- the asynchronous connection check reports per-step results, refuses parallel runs and
  stores the verification only for unchanged settings;
- general settings saved by a stale window cannot touch ``remote_channel.json``;
- the main page renders the card, the status badge and their scripts only for
  ``feedback-cli`` windows, and the standalone page hosts the card without any session UI.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_feedback_enhanced.remote import RemoteConfigStore
from mcp_feedback_enhanced.remote.check import ConnectionCheckRun
from mcp_feedback_enhanced.web.main import WebUIManager
from mcp_feedback_enhanced.web.routes import remote_routes


BOT_TOKEN = "fake-bot-token-abcdefghijklmnop"  # noqa: S105
CHANNEL_ID = "123456789012345678"
USER_ID = "223456789012345678"
LOCAL_BASE = "http://127.0.0.1"

COMPLETE_SETTINGS: dict[str, Any] = {
    "provider": "discord",
    "discord": {
        "token": BOT_TOKEN,
        "channel_id": CHANNEL_ID,
        "allowed_user_ids": [USER_ID],
    },
}


class FakeChecker:
    """Connection checker that needs no network and can be held or made to fail.

    ``gate`` (when set) keeps the check inside the ``reply`` step until the test releases
    it from another thread; the app runs on the test client's own event loop thread.
    """

    provider = "discord"
    step_ids = ("token", "channel", "post", "reply", "archive")
    optional_steps = ("archive",)

    def __init__(self) -> None:
        self.fail_step: str | None = None
        self.fail_reason = "auth_failed"
        self.crash_step: str | None = None
        self.gate: threading.Event | None = None
        self.runs = 0

    async def run(self, check: ConnectionCheckRun) -> None:
        self.runs += 1
        check.reply_timeout_seconds = 5
        for step in self.step_ids:
            check.begin(step)
            if step == self.crash_step:
                raise RuntimeError("checker bug")
            if step == self.fail_step:
                check.fail(step, self.fail_reason)
                return
            if step == "reply" and self.gate is not None:
                while not self.gate.is_set():
                    await asyncio.sleep(0.01)
            check.succeed(step)


@dataclass
class RemoteEnv:
    """Everything a route test needs, wired to one CLI-hosted manager."""

    manager: WebUIManager
    client: TestClient
    checker: FakeChecker
    store: RemoteConfigStore


@pytest.fixture
def remote_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[RemoteEnv]:
    """A CLI-hosted manager served through a test client on a loopback host."""
    checker = FakeChecker()
    # The factory is looked up when the routes are registered, so patch it first.
    monkeypatch.setattr(remote_routes, "create_checker", lambda _config: checker)
    manager = WebUIManager(host="127.0.0.1", port=0)
    manager.remote_settings_available = True
    # The context manager keeps one event loop alive, which background checks need.
    with TestClient(manager.app, base_url=LOCAL_BASE) as client:
        yield RemoteEnv(manager, client, checker, RemoteConfigStore())


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[RemoteEnv]:
    """An MCP-hosted manager: the remote settings flag keeps its default (off)."""
    checker = FakeChecker()
    monkeypatch.setattr(remote_routes, "create_checker", lambda _config: checker)
    manager = WebUIManager(host="127.0.0.1", port=0)
    assert manager.remote_settings_available is False
    with TestClient(manager.app, base_url=LOCAL_BASE) as client:
        yield RemoteEnv(manager, client, checker, RemoteConfigStore())


def wait_until_done(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    """Poll a check like the settings card does, until it reports ``done``."""
    deadline = time.monotonic() + timeout
    while True:
        payload = client.get(f"/api/remote-config/test/{run_id}").json()
        if payload["done"]:
            return payload
        assert time.monotonic() < deadline, f"check never finished: {payload}"
        time.sleep(0.02)


def save_complete_settings(env: RemoteEnv) -> dict:
    response = env.client.post("/api/remote-config", json=COMPLETE_SETTINGS)
    assert response.status_code == 200, response.text
    return response.json()["config"]


def verify_settings(env: RemoteEnv) -> dict:
    """Save complete settings and run a passing check; returns the finished run."""
    save_complete_settings(env)
    started = env.client.post("/api/remote-config/test", json={})
    assert started.status_code == 202, started.text
    finished = wait_until_done(env.client, started.json()["run_id"])
    assert finished["verified"] is True
    return finished


# --- Availability (task 3.5 / 6.5) ------------------------------------------------


REMOTE_ENDPOINTS = (
    ("GET", "/remote-settings"),
    ("GET", "/api/remote-config"),
    ("POST", "/api/remote-config"),
    ("POST", "/api/remote-config/test"),
    ("GET", "/api/remote-config/test/0123456789abcdef"),
)


@pytest.mark.parametrize(("method", "path"), REMOTE_ENDPOINTS)
def test_mcp_hosted_manager_answers_404_on_every_remote_endpoint(mcp_env, method, path):
    kwargs: dict[str, Any] = {"json": {}} if method == "POST" else {}

    response = mcp_env.client.request(method, path, **kwargs)

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_mcp_hosted_manager_hides_the_endpoints_even_from_foreign_hosts(mcp_env):
    """Availability is decided before the guard, so nothing reveals the feature exists."""
    response = mcp_env.client.get(
        "/api/remote-config", headers={"Host": "evil.example.com"}
    )

    assert response.status_code == 404


def test_availability_is_read_on_every_request(remote_env):
    assert remote_env.client.get("/api/remote-config").status_code == 200

    remote_env.manager.remote_settings_available = False
    assert remote_env.client.get("/api/remote-config").status_code == 404

    remote_env.manager.remote_settings_available = True
    assert remote_env.client.get("/api/remote-config").status_code == 200


def test_mcp_hosted_manager_never_writes_a_remote_config(mcp_env):
    mcp_env.client.post("/api/remote-config", json=COMPLETE_SETTINGS)

    assert not mcp_env.store.path.exists()


# --- Local-origin guard (task 3.6 / 6.5) ------------------------------------------


def config_bytes(env: RemoteEnv) -> bytes | None:
    path = env.store.path
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        "192.168.1.20:8765",
        "127.0.0.1.evil.com",
        "localhost.evil.com:80",
        "0.0.0.0:8765",
        "",
    ],
)
def test_non_loopback_hosts_are_refused_for_reads_and_writes(remote_env, host):
    before = config_bytes(remote_env)

    read = remote_env.client.get("/api/remote-config", headers={"Host": host})
    write = remote_env.client.post(
        "/api/remote-config", json=COMPLETE_SETTINGS, headers={"Host": host}
    )

    assert read.status_code == 403
    assert read.json()["reason"] == "host"
    assert write.status_code == 403
    assert config_bytes(remote_env) == before


def test_cross_site_text_plain_post_is_refused_and_changes_nothing(remote_env):
    """A plain HTML form can send text/plain without a preflight; it must not get through."""
    before = config_bytes(remote_env)

    response = remote_env.client.post(
        "/api/remote-config",
        content=f'{{"enabled": false, "discord": {{"channel_id": "{CHANNEL_ID}"}}}}',
        headers={
            "Content-Type": "text/plain",
            "Origin": "http://evil.example.com",
        },
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "content_type"
    assert config_bytes(remote_env) == before


def test_form_encoded_post_is_refused(remote_env):
    response = remote_env.client.post(
        "/api/remote-config/test",
        data={"a": "b"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "content_type"


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.com",
        "http://127.0.0.1:1111",  # same host, other port
        "https://127.0.0.1",  # same host, other scheme-port pair
        "null",
        "file://",
    ],
)
def test_mismatching_origin_is_refused(remote_env, origin):
    before = config_bytes(remote_env)

    write = remote_env.client.post(
        "/api/remote-config",
        json=COMPLETE_SETTINGS,
        headers={"Origin": origin, "Host": "127.0.0.1:2222"},
    )
    read = remote_env.client.get(
        "/api/remote-config", headers={"Origin": origin, "Host": "127.0.0.1:2222"}
    )

    assert write.status_code == 403
    assert write.json()["reason"] == "origin"
    assert read.status_code == 403
    assert config_bytes(remote_env) == before


def test_cross_site_fetch_without_origin_is_refused_for_writes(remote_env):
    before = config_bytes(remote_env)

    refused = remote_env.client.post(
        "/api/remote-config",
        json=COMPLETE_SETTINGS,
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    same_site = remote_env.client.post(
        "/api/remote-config",
        json=COMPLETE_SETTINGS,
        headers={"Sec-Fetch-Site": "same-site"},
    )

    assert refused.status_code == 403
    assert same_site.status_code == 403
    assert config_bytes(remote_env) == before


@pytest.mark.parametrize("fetch_site", ["same-origin", "none"])
def test_same_origin_fetch_metadata_is_accepted(remote_env, fetch_site):
    response = remote_env.client.post(
        "/api/remote-config",
        json=COMPLETE_SETTINGS,
        headers={"Sec-Fetch-Site": fetch_site},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("localhost:54321", "http://localhost:54321"),  # e.g. an SSH forwarded port
        ("LOCALHOST:54321", "http://localhost:54321"),
        ("[::1]:8765", "http://[::1]:8765"),
        ("127.0.0.1", "http://127.0.0.1"),
    ],
)
def test_loopback_names_are_accepted_including_forwarded_ports(
    remote_env, host, origin
):
    headers = {"Host": host, "Origin": origin}

    read = remote_env.client.get("/api/remote-config", headers=headers)
    write = remote_env.client.post(
        "/api/remote-config", json=COMPLETE_SETTINGS, headers=headers
    )

    assert read.status_code == 200
    assert write.status_code == 200


def test_clients_that_send_neither_origin_nor_fetch_metadata_are_local_tools(
    remote_env,
):
    """Command line tools send neither header; they are local processes by nature."""
    response = remote_env.client.post("/api/remote-config", json=COMPLETE_SETTINGS)

    assert response.status_code == 200


def test_check_endpoints_are_guarded_too(remote_env):
    started = remote_env.client.post(
        "/api/remote-config/test",
        json={},
        headers={"Origin": "http://evil.example.com"},
    )
    polled = remote_env.client.get(
        "/api/remote-config/test/abc", headers={"Host": "evil.example.com"}
    )

    assert started.status_code == 403
    assert polled.status_code == 403
    assert remote_env.checker.runs == 0


# --- Configuration endpoints (tasks 3.3 / 3.4) ------------------------------------


def test_default_configuration_is_disabled_and_reports_no_token(remote_env):
    response = remote_env.client.get("/api/remote-config")

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["enabled"] is False
    assert config["effective"] is False
    assert config["verified"] is False
    assert config["provider"] == "discord"
    assert config["providers"] == ["discord"]
    assert config["discord"] == {
        "token_set": False,
        "channel_id": "",
        "allowed_user_ids": [],
    }


def test_saved_token_is_write_only(remote_env):
    saved = remote_env.client.post("/api/remote-config", json=COMPLETE_SETTINGS)
    read = remote_env.client.get("/api/remote-config")

    for response in (saved, read):
        assert response.status_code == 200
        assert BOT_TOKEN not in response.text
        assert response.json()["config"]["discord"]["token_set"] is True
    assert remote_env.store.load().discord.token == BOT_TOKEN


def test_saving_without_a_token_keeps_the_stored_one(remote_env):
    save_complete_settings(remote_env)

    response = remote_env.client.post(
        "/api/remote-config",
        json={"discord": {"channel_id": "323456789012345678"}},
    )

    assert response.status_code == 200
    stored = remote_env.store.load()
    assert stored.discord.token == BOT_TOKEN
    assert stored.discord.channel_id == "323456789012345678"


def test_enabling_is_refused_until_the_settings_were_verified(remote_env):
    save_complete_settings(remote_env)

    response = remote_env.client.post("/api/remote-config", json={"enabled": True})

    assert response.status_code == 400
    assert response.json() == {"error": "not_verified"}
    assert remote_env.store.load().enabled is False


def test_enabling_is_refused_without_an_allowlist(remote_env):
    response = remote_env.client.post(
        "/api/remote-config",
        json={
            "enabled": True,
            "discord": {"token": BOT_TOKEN, "channel_id": CHANNEL_ID},
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "allowlist_required"}
    assert remote_env.store.load().enabled is False


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"provider": "telegram"}, "unsupported_provider"),
        ({"discord": {"token": "short"}}, "invalid_token"),
        ({"discord": {"token": "has a space inside it"}}, "invalid_token"),
        ({"discord": {"channel_id": "not-a-snowflake"}}, "invalid_channel_id"),
        ({"discord": {"allowed_user_ids": ["nope"]}}, "invalid_user_id"),
        ({"discord": {"allowed_user_ids": 12}}, "invalid_user_id"),
        ({"discord": "oops"}, "invalid_payload"),
        ({"enabled": "yes"}, "invalid_payload"),
    ],
)
def test_invalid_settings_are_rejected_with_a_code_and_change_nothing(
    remote_env, payload, code
):
    before = config_bytes(remote_env)

    response = remote_env.client.post("/api/remote-config", json=payload)

    assert response.status_code == 400
    assert response.json() == {"error": code}
    assert config_bytes(remote_env) == before


def test_malformed_and_non_object_bodies_are_rejected(remote_env):
    headers = {"Content-Type": "application/json"}

    broken = remote_env.client.post("/api/remote-config", content="{", headers=headers)
    array = remote_env.client.post("/api/remote-config", content="[1]", headers=headers)

    assert broken.status_code == 400
    assert broken.json() == {"error": "invalid_payload"}
    assert array.status_code == 400
    assert array.json() == {"error": "invalid_payload"}


def test_oversized_bodies_are_refused(remote_env):
    body = b'{"x": "' + b"a" * (remote_routes.MAX_BODY_BYTES + 1) + b'"}'

    response = remote_env.client.post(
        "/api/remote-config", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


def test_changing_a_setting_drops_verification_and_switches_the_feature_off(remote_env):
    verify_settings(remote_env)
    enabled = remote_env.client.post("/api/remote-config", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["config"]["effective"] is True

    changed = remote_env.client.post(
        "/api/remote-config",
        json={"discord": {"allowed_user_ids": [USER_ID, "323456789012345678"]}},
    )

    config = changed.json()["config"]
    assert config["verified"] is False
    assert config["enabled"] is False
    assert config["effective"] is False


# --- Connection check (asynchronous test flow) ------------------------------------


def test_check_runs_in_the_background_and_verifies_the_settings(remote_env):
    save_complete_settings(remote_env)

    started = remote_env.client.post("/api/remote-config/test", json={})

    assert started.status_code == 202
    first = started.json()
    assert first["provider"] == "discord"
    assert [step["id"] for step in first["steps"]] == [
        "token",
        "channel",
        "post",
        "reply",
        "archive",
    ]
    finished = wait_until_done(remote_env.client, first["run_id"])
    assert finished["passed"] is True
    assert finished["verified"] is True
    assert finished["verify_error"] is None
    assert finished["reply_timeout_seconds"] == 5
    assert all(step["state"] == "ok" for step in finished["steps"])
    assert BOT_TOKEN not in str(finished)

    config = remote_env.client.get("/api/remote-config").json()["config"]
    assert config["verified"] is True
    enabled = remote_env.client.post("/api/remote-config", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["config"]["effective"] is True


def test_failed_check_reports_the_step_and_does_not_verify(remote_env):
    save_complete_settings(remote_env)
    remote_env.checker.fail_step = "post"
    remote_env.checker.fail_reason = "missing_permission"

    started = remote_env.client.post("/api/remote-config/test", json={})
    finished = wait_until_done(remote_env.client, started.json()["run_id"])

    states = {step["id"]: step["state"] for step in finished["steps"]}
    assert states == {
        "token": "ok",
        "channel": "ok",
        "post": "failed",
        "reply": "skipped",
        "archive": "skipped",
    }
    post_step = next(step for step in finished["steps"] if step["id"] == "post")
    assert post_step["reason"] == "missing_permission"
    assert finished["passed"] is False
    assert finished["verified"] is False
    assert remote_env.store.load().is_verified() is False


def test_checker_crash_is_reported_on_the_running_step(remote_env):
    save_complete_settings(remote_env)
    remote_env.checker.crash_step = "channel"

    started = remote_env.client.post("/api/remote-config/test", json={})
    finished = wait_until_done(remote_env.client, started.json()["run_id"])

    channel_step = next(step for step in finished["steps"] if step["id"] == "channel")
    assert channel_step["state"] == "failed"
    assert channel_step["reason"] == "internal_error"
    assert finished["verified"] is False


def test_second_check_is_refused_while_one_is_running(remote_env):
    save_complete_settings(remote_env)
    gate = threading.Event()
    remote_env.checker.gate = gate
    first = remote_env.client.post("/api/remote-config/test", json={})
    assert first.status_code == 202

    second = remote_env.client.post("/api/remote-config/test", json={})

    assert second.status_code == 409
    assert second.json() == {
        "error": "check_in_progress",
        "run_id": first.json()["run_id"],
    }
    gate.set()
    wait_until_done(remote_env.client, first.json()["run_id"])
    assert remote_env.checker.runs == 1
    # After the first run finished, a new one may start.
    third = remote_env.client.post("/api/remote-config/test", json={})
    assert third.status_code == 202
    wait_until_done(remote_env.client, third.json()["run_id"])


def test_settings_changed_during_a_check_are_not_marked_as_verified(remote_env):
    save_complete_settings(remote_env)
    gate = threading.Event()
    remote_env.checker.gate = gate
    started = remote_env.client.post("/api/remote-config/test", json={})

    remote_env.client.post(
        "/api/remote-config",
        json={"discord": {"channel_id": "323456789012345678"}},
    )
    gate.set()
    finished = wait_until_done(remote_env.client, started.json()["run_id"])

    assert finished["passed"] is True
    assert finished["verified"] is False
    assert finished["verify_error"] == "config_changed"
    assert remote_env.store.load().is_verified() is False


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "token_required"),
        ({"discord": {"token": BOT_TOKEN}}, "channel_required"),
        (
            {"discord": {"token": BOT_TOKEN, "channel_id": CHANNEL_ID}},
            "allowlist_required",
        ),
    ],
)
def test_check_needs_a_complete_configuration(remote_env, payload, code):
    if payload:
        remote_env.client.post("/api/remote-config", json=payload)

    response = remote_env.client.post("/api/remote-config/test", json={})

    assert response.status_code == 400
    assert response.json() == {"error": code}
    assert remote_env.checker.runs == 0


def test_unknown_run_id_is_404(remote_env):
    response = remote_env.client.get("/api/remote-config/test/not-a-real-run")

    assert response.status_code == 404
    assert response.json() == {"error": "unknown_run"}


def test_check_body_must_stay_well_formed(remote_env):
    save_complete_settings(remote_env)

    response = remote_env.client.post(
        "/api/remote-config/test",
        content="{",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert remote_env.checker.runs == 0


# --- Stale windows (task 6.4) ----------------------------------------------------


def test_a_stale_window_saving_general_settings_cannot_touch_the_remote_config(
    remote_env, monkeypatch, tmp_path
):
    """``/api/save-settings`` overwrites the whole ``ui_settings.json``; the remote
    configuration lives in its own file precisely so that this cannot clobber it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    verify_settings(remote_env)
    enabled = remote_env.client.post("/api/remote-config", json={"enabled": True})
    assert enabled.json()["config"]["effective"] is True
    before = config_bytes(remote_env)

    stale_settings = {
        "language": "en",
        "remote": {"enabled": False, "discord": {"token": "stale"}},
    }
    ui_settings = home / ".config" / "mcp-feedback-enhanced" / "ui_settings.json"

    saved = remote_env.client.post("/api/save-settings", json=stale_settings)
    # Written under the patched home: the developer's real settings stay untouched.
    assert saved.status_code == 200
    assert ui_settings.exists()
    cleared = remote_env.client.post("/api/clear-settings")

    assert cleared.status_code == 200
    assert not ui_settings.exists()
    assert config_bytes(remote_env) == before
    assert remote_env.store.load().is_effective() is True


# --- Rendering (tasks 4.1 / 4.2 / 6.5) --------------------------------------------

REMOTE_SCRIPTS = (
    "/static/js/modules/remote/remote-common.js",
    "/static/js/modules/remote/remote-status-badge.js",
    "/static/js/modules/remote/remote-settings-card.js",
)


def render_feedback_page(
    env: RemoteEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> str:
    """Open a session and fetch the main page.

    ``Path.home`` is redirected so the developer's real layout settings are never read.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    env.manager.create_session(str(tmp_path), "render test")

    response = env.client.get("/")

    assert response.status_code == 200
    return response.text


def test_mcp_hosted_feedback_page_renders_no_remote_ui(mcp_env, monkeypatch, tmp_path):
    html = render_feedback_page(mcp_env, monkeypatch, tmp_path)

    for marker in (
        "remoteSettingsCard",
        "remoteStatusBadge",
        "remote-settings.css",
        *REMOTE_SCRIPTS,
    ):
        assert marker not in html


def test_cli_hosted_feedback_page_renders_card_badge_and_modules(
    remote_env, monkeypatch, tmp_path
):
    html = render_feedback_page(remote_env, monkeypatch, tmp_path)

    assert 'id="remoteSettingsCard"' in html
    assert 'id="remoteStatusBadge"' in html
    assert "/static/css/remote-settings.css" in html
    # app.js looks the badge up while it starts, so every module must load before it.
    positions = [html.index(src) for src in REMOTE_SCRIPTS]
    assert positions == sorted(positions)
    assert positions[-1] < html.index("/static/js/app.js")


def test_standalone_page_hosts_the_card_without_any_session_ui(remote_env):
    response = remote_env.client.get("/remote-settings")

    assert response.status_code == 200
    html = response.text
    assert 'id="remoteSettingsCard"' in html
    assert "/static/js/modules/remote/remote-settings-page.js" in html
    # The page must neither need nor create a session.
    assert remote_env.manager.get_current_session() is None
    assert "/static/js/app.js" not in html
    assert 'id="remoteStatusBadge"' not in html


def test_rendered_pages_never_contain_the_token(remote_env, monkeypatch, tmp_path):
    save_complete_settings(remote_env)

    standalone = remote_env.client.get("/remote-settings").text
    feedback = render_feedback_page(remote_env, monkeypatch, tmp_path)

    assert BOT_TOKEN not in standalone
    assert BOT_TOKEN not in feedback
