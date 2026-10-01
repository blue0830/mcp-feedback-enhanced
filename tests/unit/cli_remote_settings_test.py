#!/usr/bin/env python3
"""Tests for the standalone remote settings mode (``feedback-cli --remote-settings``).

Covered rules:
- the flag is mutually exclusive with ``--summary`` / ``--summary-file`` and a conflict
  fails at argument parsing, before any server exists;
- the mode creates no feedback session and never starts the remote coordinator;
- it stays alive until the desktop window closes, ``--timeout`` elapses or Ctrl+C, and
  every one of those ends with exit code 0;
- shutdown stops the window, cancels a running connection check and only then the server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import pytest

import mcp_feedback_enhanced.cli as feedback_cli


class _FakeManager:
    """Stand-in for ``WebUIManager`` that records what the runtime asks of it."""

    instances: list[_FakeManager] = []

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = 9300 + len(self.instances)
        self.remote_settings_available = False
        self.events: list[str] = []
        self.instances.append(self)

    def create_session(self, *_args: Any, **_kwargs: Any) -> str:
        self.events.append("create_session")
        raise AssertionError("the settings mode must not create a feedback session")

    def start_server(self) -> None:
        self.events.append("start_server")

    def get_server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def open_browser(self, url: str) -> None:
        self.events.append(f"open_browser:{url}")

    async def shutdown_remote_checks(self) -> None:
        self.events.append("shutdown_remote_checks")

    def stop(self) -> None:
        self.events.append("stop")


class _FakeDesktop:
    """Stand-in for ``DesktopApp``: a window that stays open until the test closes it."""

    instances: list[_FakeDesktop] = []
    launch_fails = False

    def __init__(self) -> None:
        self.running = False
        self.launched_urls: list[str] = []
        self.stopped = False
        self.instances.append(self)

    def set_desktop_mode(self, _enabled: bool = True) -> None:
        return None

    async def launch_tauri_app(self, url: str) -> None:
        if self.launch_fails:
            raise RuntimeError("desktop startup failed")
        self.launched_urls.append(url)
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False
        self.stopped = True


def _forbid(name: str):
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(f"{name} must not be used by the settings mode")

    return _raise


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch):
    _FakeManager.instances = []
    _FakeDesktop.instances = []
    _FakeDesktop.launch_fails = False
    monkeypatch.setattr(feedback_cli, "WebUIManager", _FakeManager)
    monkeypatch.setattr(feedback_cli, "DesktopApp", _FakeDesktop)
    # Nothing may be forwarded remotely in this mode.
    monkeypatch.setattr(feedback_cli, "create_channel", _forbid("create_channel"))
    monkeypatch.setattr(
        feedback_cli, "RemoteSessionCoordinator", _forbid("RemoteSessionCoordinator")
    )
    monkeypatch.setattr(
        feedback_cli.RemoteSettingsRuntime, "DESKTOP_POLL_SECONDS", 0.01
    )


def _args(timeout: int = 30) -> argparse.Namespace:
    return argparse.Namespace(
        project_directory=".",
        summary=None,
        summary_file=None,
        remote_settings=True,
        timeout=timeout,
    )


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, (
            "condition never became true"
        )
        await asyncio.sleep(0.01)


# --- Argument parsing ---------------------------------------------------------------


def test_flag_defaults_to_off_and_turns_on_when_given():
    parser = feedback_cli.build_parser()

    assert parser.parse_args([]).remote_settings is False
    assert parser.parse_args(["--remote-settings"]).remote_settings is True


@pytest.mark.parametrize(
    "extra",
    [["--summary", "text"], ["--summary-file", "summary.md"]],
)
def test_flag_conflicts_with_summary_options(extra, capsys):
    parser = feedback_cli.build_parser()

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--remote-settings", *extra])

    assert exit_info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_timeout_is_still_accepted_together_with_the_flag():
    args = feedback_cli.build_parser().parse_args(
        ["--remote-settings", "--timeout", "42"]
    )

    assert args.remote_settings is True
    assert args.timeout == 42


def test_conflicting_flags_fail_before_any_server_exists(monkeypatch, capsys):
    monkeypatch.setattr(
        feedback_cli, "ensure_foreground_blocking_invocation", lambda: None
    )
    monkeypatch.setattr(feedback_cli, "init_encoding", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["feedback-cli", "--remote-settings", "--summary", "x"]
    )

    with pytest.raises(SystemExit) as exit_info:
        feedback_cli.main()

    assert exit_info.value.code == 2
    assert _FakeManager.instances == []
    assert _FakeDesktop.instances == []


# --- Runtime ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_shows_only_the_settings_page_without_any_session():
    runtime = feedback_cli.RemoteSettingsRuntime(_args())
    manager = runtime.manager

    task = asyncio.create_task(runtime.run())
    await _wait_for(
        lambda: _FakeDesktop.instances and _FakeDesktop.instances[0].running
    )
    desktop = _FakeDesktop.instances[0]
    _FakeDesktop.instances[0].running = False
    result = await task
    await runtime.shutdown()

    assert result == 0
    assert manager.remote_settings_available is True
    assert desktop.launched_urls == [f"http://127.0.0.1:{manager.port}/remote-settings"]
    assert "create_session" not in manager.events
    assert manager.events[0] == "start_server"


@pytest.mark.asyncio
async def test_closing_the_desktop_window_ends_the_mode_with_exit_code_zero():
    task = asyncio.create_task(feedback_cli._run_remote_settings(_args()))
    await _wait_for(
        lambda: _FakeDesktop.instances and _FakeDesktop.instances[0].running
    )

    assert not task.done()
    _FakeDesktop.instances[0].running = False  # The user closes the window.
    result = await asyncio.wait_for(task, timeout=5)

    assert result == 0
    events = _FakeManager.instances[0].events
    assert events[-2:] == ["shutdown_remote_checks", "stop"]


@pytest.mark.asyncio
async def test_timeout_ends_the_mode_with_exit_code_zero():
    started = asyncio.get_running_loop().time()

    result = await asyncio.wait_for(
        feedback_cli._run_remote_settings(_args(timeout=1)), 10
    )

    assert result == 0
    assert asyncio.get_running_loop().time() - started >= 1
    assert _FakeDesktop.instances[0].stopped is True
    assert _FakeManager.instances[0].events[-1] == "stop"


@pytest.mark.asyncio
async def test_ctrl_c_ends_the_mode_with_exit_code_zero_and_still_cleans_up():
    task = asyncio.create_task(feedback_cli._run_remote_settings(_args()))
    await _wait_for(
        lambda: _FakeDesktop.instances and _FakeDesktop.instances[0].running
    )

    task.cancel()  # What asyncio.run does when SIGINT arrives.
    result = await asyncio.wait_for(task, timeout=5)

    assert result == 0
    assert task.cancelled() is False
    assert _FakeDesktop.instances[0].stopped is True
    assert _FakeManager.instances[0].events[-2:] == ["shutdown_remote_checks", "stop"]


@pytest.mark.asyncio
async def test_browser_fallback_waits_for_ctrl_c_and_tells_the_user(capsys):
    _FakeDesktop.launch_fails = True
    task = asyncio.create_task(feedback_cli._run_remote_settings(_args()))
    await _wait_for(
        lambda: (
            _FakeManager.instances
            and any(
                e.startswith("open_browser:") for e in _FakeManager.instances[0].events
            )
        )
    )
    manager = _FakeManager.instances[0]

    # Nothing observable can end a browser page: the mode must keep waiting.
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()
    result = await asyncio.wait_for(task, timeout=5)

    assert result == 0
    assert (
        f"open_browser:http://127.0.0.1:{manager.port}/remote-settings"
        in manager.events
    )
    assert "Ctrl+C" in capsys.readouterr().out
    assert manager.events[-2:] == ["shutdown_remote_checks", "stop"]


@pytest.mark.asyncio
async def test_browser_fallback_also_ends_on_timeout():
    _FakeDesktop.launch_fails = True

    result = await asyncio.wait_for(
        feedback_cli._run_remote_settings(_args(timeout=1)), 10
    )

    assert result == 0
    assert _FakeManager.instances[0].events[-1] == "stop"


@pytest.mark.asyncio
async def test_invalid_timeout_is_rejected_before_the_server_starts():
    with pytest.raises(ValueError):
        await feedback_cli._run_remote_settings(_args(timeout=0))

    events = _FakeManager.instances[0].events
    assert "start_server" not in events
    assert events[-1] == "stop"  # Shutdown stays safe on a server that never started.


@pytest.mark.asyncio
async def test_shutdown_survives_a_window_that_cannot_be_stopped():
    runtime = feedback_cli.RemoteSettingsRuntime(_args())
    task = asyncio.create_task(runtime.run())
    await _wait_for(
        lambda: _FakeDesktop.instances and _FakeDesktop.instances[0].running
    )

    def _broken_stop() -> None:
        raise OSError("cannot terminate")

    _FakeDesktop.instances[0].stop = _broken_stop  # type: ignore[method-assign]
    _FakeDesktop.instances[0].running = False
    await task
    await runtime.shutdown()

    assert runtime.manager.events[-2:] == ["shutdown_remote_checks", "stop"]


# --- Entry point --------------------------------------------------------------------


def _prepare_main(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(
        feedback_cli, "ensure_foreground_blocking_invocation", lambda: None
    )
    monkeypatch.setattr(feedback_cli, "init_encoding", lambda: None)
    monkeypatch.setattr(sys, "argv", ["feedback-cli", *argv])


def test_main_dispatches_the_flag_to_the_settings_mode(monkeypatch):
    seen: list[str] = []

    async def _settings(_args: argparse.Namespace) -> int:
        seen.append("settings")
        return 0

    async def _feedback(_args: argparse.Namespace) -> int:
        seen.append("feedback")
        return 0

    monkeypatch.setattr(feedback_cli, "_run_remote_settings", _settings)
    monkeypatch.setattr(feedback_cli, "_run_cli", _feedback)

    _prepare_main(monkeypatch, ["--remote-settings"])
    assert feedback_cli.main() == 0
    _prepare_main(monkeypatch, ["--summary", "hello"])
    assert feedback_cli.main() == 0

    assert seen == ["settings", "feedback"]


def test_keyboard_interrupt_is_a_normal_exit_only_in_the_settings_mode(
    monkeypatch, capsys
):
    def _interrupted(coro: Any) -> int:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(feedback_cli.asyncio, "run", _interrupted)

    _prepare_main(monkeypatch, ["--remote-settings"])
    assert feedback_cli.main() == 0
    assert capsys.readouterr().out == ""

    _prepare_main(monkeypatch, ["--summary", "hello"])
    assert feedback_cli.main() == 1
    assert "操作被中斷" in capsys.readouterr().out
