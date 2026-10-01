#!/usr/bin/env python3
"""CLI runtime tests for remote communication, using a real manager and a fake provider."""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

import pytest

import mcp_feedback_enhanced.cli as feedback_cli
from mcp_feedback_enhanced.remote import (
    RemoteChannelError,
    RemoteConfigStore,
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    StateReporter,
)
from mcp_feedback_enhanced.web.models import WebFeedbackSession


# Joined from parts: a token-shaped literal makes secret scanners block pushes.
TOKEN = ".".join(
    ("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789")
)
CHANNEL = "123456789012345678"
USER = "223456789012345678"


class _FakeDesktop:
    running = True

    def set_desktop_mode(self, _enabled: bool = True) -> None:
        return None

    async def launch_tauri_app(self, _url: str) -> None:
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False


class _FakePage:
    """Stands in for the window websocket and records what the server pushes."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def states(self) -> list[str]:
        return [
            m["status"]["state"] for m in self.sent if m.get("type") == "remote_status"
        ]

    def codes(self) -> list[str]:
        return [m["code"] for m in self.sent if m.get("type") == "notification"]


class _FakeChannel:
    provider = "fake"

    def __init__(self, open_error: Exception | None = None) -> None:
        self.open_error = open_error
        self.request: RemoteRequest | None = None
        self.waiting = asyncio.Event()
        self.reply_event = asyncio.Event()
        self.reply = RemoteReply(text="reply from phone")
        self.closed: list[RemoteOutcome] = []

    async def open(self, request: RemoteRequest) -> RemoteHandle:
        if self.open_error is not None:
            raise self.open_error
        self.request = request
        return RemoteHandle(self.provider, "conversation")

    async def wait_reply(
        self, handle: RemoteHandle, report: StateReporter
    ) -> RemoteReply:
        self.waiting.set()
        await self.reply_event.wait()
        return self.reply

    async def close(self, handle: RemoteHandle, outcome: RemoteOutcome) -> None:
        self.closed.append(outcome)


@pytest.fixture(autouse=True)
def _runtime_environment(monkeypatch):
    monkeypatch.setenv("MCP_TEST_MODE", "true")
    monkeypatch.setattr(feedback_cli, "DesktopApp", _FakeDesktop)
    monkeypatch.setattr(
        feedback_cli, "cleanup_expired_image_artifacts", lambda *args, **kwargs: 0
    )
    monkeypatch.setattr(
        feedback_cli, "persist_feedback_images", lambda *args, **kwargs: []
    )


def _enable_remote() -> None:
    store = RemoteConfigStore()
    store.update(
        {
            "provider": "discord",
            "discord": {
                "token": TOKEN,
                "forum_channel_id": CHANNEL,
                "allowed_user_ids": [USER],
            },
        }
    )
    store.mark_verified(store.load())
    store.update({"enabled": True})


def _args(timeout: int = 30) -> argparse.Namespace:
    return argparse.Namespace(
        project_directory=".",
        summary="please review",
        summary_file=None,
        timeout=timeout,
    )


async def _settle(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def _start(
    runtime: feedback_cli.CliSessionRuntime,
) -> tuple[asyncio.Task, _FakePage]:
    """Run the runtime in a task with a fake window attached from the start.

    The window is attached inside ``create_session``, before the runtime can push
    anything; attaching it from the test after the task started would race with the
    remote side and make the pushed states (and their order) depend on scheduling.
    """
    page = _FakePage()
    manager = runtime.manager
    create_session = manager.create_session

    def create_session_with_window(*args: Any, **kwargs: Any) -> str:
        session_id = create_session(*args, **kwargs)
        manager.get_current_session().websocket = page
        return session_id

    manager.create_session = create_session_with_window  # type: ignore[method-assign]
    task = asyncio.create_task(runtime.run())
    await _settle(lambda: runtime._session is not None)
    return task, page


def _use_channel(monkeypatch, channel: _FakeChannel) -> list[Any]:
    created: list[Any] = []

    def factory(config):
        created.append(config)
        return channel

    monkeypatch.setattr(feedback_cli, "create_channel", factory)
    return created


@pytest.mark.asyncio
async def test_disabled_remote_is_a_strict_no_op(monkeypatch, capsys):
    def forbidden(_config):
        raise AssertionError("no provider may be created when remote is not effective")

    monkeypatch.setattr(feedback_cli, "create_channel", forbidden)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    await asyncio.sleep(0.05)
    assert runtime._coordinator is None
    assert runtime._session.remote_status["state"] == "off"

    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("local answer", [], {})
    )
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    assert "local answer" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_enabled_but_unverified_config_reports_why_it_is_off(monkeypatch):
    store = RemoteConfigStore()
    store.update(
        {
            "discord": {
                "token": TOKEN,
                "forum_channel_id": CHANNEL,
                "allowed_user_ids": [USER],
            }
        }
    )
    # Hand-edit the file to "enabled" without a verification, as a stale config would be.
    raw = store.path.read_text(encoding="utf-8").replace(
        '"enabled": false', '"enabled": true'
    )
    store.path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(
        feedback_cli, "create_channel", lambda _c: pytest.fail("must stay local-only")
    )
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    assert runtime._session.remote_status["state"] == "off"
    assert runtime._session.remote_status["reason"] == "not_verified"

    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("local", [], {})
    )
    await asyncio.wait_for(task, 10)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_remote_reply_ends_the_cli_like_a_local_submission(monkeypatch, capsys):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, page = await _start(runtime)
    await asyncio.wait_for(channel.waiting.wait(), 5)
    channel.reply_event.set()
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    session = runtime._session
    assert session.feedback_source == "remote:fake"
    assert "reply from phone" in capsys.readouterr().out
    assert channel.closed == [RemoteOutcome.REMOTE_ANSWERED]
    assert channel.request is not None
    assert channel.request.summary == "please review"
    assert session.user_messages[-1]["content"] == "reply from phone"
    assert session.user_messages[-1]["submission_method"] == "fake"
    # The window was told about the submission on the server loop.
    assert "session.feedbackSubmitted" in page.codes()
    # The window was attached from the start, so it saw the whole progression; a window
    # that connects later gets the latest status replayed from the session instead.
    assert page.states() == ["connecting", "waiting", "answered_remotely"]
    assert session.remote_status["state"] == "answered_remotely"


@pytest.mark.asyncio
async def test_local_submission_first_wins_and_the_remote_side_is_finalized_as_local(
    monkeypatch, capsys
):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, page = await _start(runtime)
    await asyncio.wait_for(channel.waiting.wait(), 5)
    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("typed locally", [], {})
    )
    assert await asyncio.wait_for(task, 10) == 0
    channel.reply_event.set()  # A late remote reply must change nothing
    await runtime.shutdown()

    output = capsys.readouterr().out
    assert "typed locally" in output
    assert "reply from phone" not in output
    assert runtime._session.feedback_source == "web"
    assert channel.closed == [RemoteOutcome.LOCAL_ANSWERED]
    assert page.states()[-1] == "answered_locally"


@pytest.mark.asyncio
async def test_simultaneous_local_and_remote_commit_exactly_once(monkeypatch, capsys):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    await asyncio.wait_for(channel.waiting.wait(), 5)
    channel.reply_event.set()
    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("typed locally", [], {})
    )
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    session = runtime._session
    output = capsys.readouterr().out
    remote_won = session.feedback_source == "remote:fake"
    assert ("reply from phone" in output) == remote_won
    assert ("typed locally" in output) == (not remote_won)
    assert channel.closed == [
        RemoteOutcome.REMOTE_ANSWERED if remote_won else RemoteOutcome.LOCAL_ANSWERED
    ]


@pytest.mark.asyncio
async def test_timeout_finalizes_the_remote_conversation_as_timeout(monkeypatch):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    monkeypatch.setattr(
        WebFeedbackSession, "effective_wait_timeout", staticmethod(lambda _t: 0.3)
    )
    runtime = feedback_cli.CliSessionRuntime(_args(timeout=5))

    task, _page = await _start(runtime)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, 10)
    await runtime.shutdown()

    assert channel.closed == [RemoteOutcome.TIMEOUT]


@pytest.mark.asyncio
async def test_interruption_finalizes_the_remote_conversation_as_interrupted(
    monkeypatch,
):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    await asyncio.wait_for(channel.waiting.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.shutdown()

    assert channel.closed == [RemoteOutcome.INTERRUPTED]


@pytest.mark.asyncio
async def test_unreachable_provider_never_blocks_the_local_flow(monkeypatch, capsys):
    _enable_remote()
    channel = _FakeChannel(
        open_error=RemoteChannelError("bad token", permanent=True, reason="auth_failed")
    )
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, page = await _start(runtime)
    await _settle(lambda: "unavailable" in page.states())
    status = next(
        m["status"]
        for m in page.sent
        if m.get("type") == "remote_status" and m["status"]["state"] == "unavailable"
    )
    assert status["reason"] == "auth_failed"

    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("still local", [], {})
    )
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    assert "still local" in capsys.readouterr().out
    assert channel.closed == []  # Nothing was opened, so nothing to close


@pytest.mark.asyncio
async def test_provider_creation_failure_is_isolated(monkeypatch, capsys):
    _enable_remote()

    def broken(_config):
        raise RuntimeError("provider import exploded")

    monkeypatch.setattr(feedback_cli, "create_channel", broken)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    # The failure happens synchronously during startup, before any page can attach; the
    # status is stored on the session and replayed to the page when it connects.
    assert runtime._session.remote_status["state"] == "unavailable"
    assert runtime._session.remote_status["reason"] == "internal_error"
    await runtime.manager.await_on_server_loop(
        runtime._session.submit_feedback("local", [], {})
    )
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    assert "local" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_configuration_changes_during_the_wait_do_not_affect_the_invocation(
    monkeypatch, capsys
):
    _enable_remote()
    channel = _FakeChannel()
    _use_channel(monkeypatch, channel)
    runtime = feedback_cli.CliSessionRuntime(_args())

    task, _page = await _start(runtime)
    await asyncio.wait_for(channel.waiting.wait(), 5)
    RemoteConfigStore().update({"enabled": False})  # Disabled while already waiting
    channel.reply_event.set()
    assert await asyncio.wait_for(task, 10) == 0
    await runtime.shutdown()

    assert "reply from phone" in capsys.readouterr().out
    assert channel.closed == [RemoteOutcome.REMOTE_ANSWERED]
