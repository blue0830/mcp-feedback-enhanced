#!/usr/bin/env python3
"""Runtime behavior tests for feedback CLI session orchestration."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from itertools import count
from typing import Any

import pytest

import mcp_feedback_enhanced.cli as feedback_cli


@dataclass
class _ScenarioConfig:
    desktop_launch_fails: bool = False
    timeout: bool = False
    manual_desktop_close: bool = False


class _FakeSession:
    def __init__(self, session_id: str, summary: str, scenario: _ScenarioConfig):
        self.session_id = session_id
        self.summary = summary
        self._scenario = scenario

    async def wait_for_feedback(self, _timeout: int) -> dict[str, Any]:
        await asyncio.sleep(0)
        if self._scenario.manual_desktop_close:
            _FakeDesktop.running = False
        if self._scenario.timeout:
            raise TimeoutError("simulated timeout")
        return {
            "logs": f"log-{self.session_id}",
            "interactive_feedback": f"feedback-{self.summary}",
            "images": [],
            "settings": {},
        }


class _FakeManager:
    counter = count(start=9200)
    created_ports: list[int] = []
    stopped_ports: list[int] = []
    opened_urls: list[str] = []
    scenario = _ScenarioConfig()

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = next(self.counter) if port == 0 else port
        self.current_session: _FakeSession | None = None
        self._session_count = 0
        self.created_ports.append(self.port)

    def create_session(self, _project_directory: str, summary: str) -> str:
        self._session_count += 1
        session_id = f"{self.port}-{self._session_count}"
        self.current_session = _FakeSession(session_id, summary, self.scenario)
        return session_id

    def get_current_session(self) -> _FakeSession | None:
        return self.current_session

    def start_server(self) -> None:
        return None

    def get_server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def open_browser(self, url: str) -> None:
        self.opened_urls.append(url)

    async def shutdown_remote_checks(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped_ports.append(self.port)


class _FakeDesktop:
    running = True

    def set_desktop_mode(self, _enabled: bool = True):
        return None

    async def launch_tauri_app(self, _url: str):
        if _FakeManager.scenario.desktop_launch_fails:
            raise RuntimeError("desktop startup failed")
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self):
        self.running = False


@pytest.fixture(autouse=True)
def _patch_runtime_dependencies(monkeypatch):
    _FakeManager.created_ports = []
    _FakeManager.stopped_ports = []
    _FakeManager.opened_urls = []
    _FakeManager.scenario = _ScenarioConfig()
    _FakeDesktop.running = True

    monkeypatch.setattr(feedback_cli, "WebUIManager", _FakeManager)
    monkeypatch.setattr(feedback_cli, "DesktopApp", _FakeDesktop)
    monkeypatch.setattr(
        feedback_cli, "cleanup_expired_image_artifacts", lambda *args, **kwargs: 0
    )
    monkeypatch.setattr(
        feedback_cli, "persist_feedback_images", lambda *args, **kwargs: []
    )


def _build_args(summary: str, timeout: int = 30) -> argparse.Namespace:
    return argparse.Namespace(
        project_directory=".",
        summary=summary,
        summary_file=None,
        timeout=timeout,
    )


@pytest.mark.asyncio
async def test_concurrent_instances_keep_isolated_ports_and_results():
    runtimes = [
        feedback_cli.CliSessionRuntime(_build_args("A")),
        feedback_cli.CliSessionRuntime(_build_args("B")),
        feedback_cli.CliSessionRuntime(_build_args("C")),
    ]
    results = await asyncio.gather(*(runtime.run() for runtime in runtimes))
    await asyncio.gather(*(runtime.shutdown() for runtime in runtimes))

    assert results == [0, 0, 0]
    assert len(set(_FakeManager.created_ports)) == 3
    assert _FakeManager.opened_urls == []


@pytest.mark.asyncio
async def test_desktop_fallback_uses_browser():
    _FakeManager.scenario = _ScenarioConfig(desktop_launch_fails=True)
    runtime = feedback_cli.CliSessionRuntime(_build_args("fallback"))
    result = await runtime.run()
    await runtime.shutdown()

    assert result == 0
    assert len(_FakeManager.opened_urls) == 1


@pytest.mark.asyncio
async def test_manual_close_and_timeout_paths_still_cleanup():
    _FakeManager.scenario = _ScenarioConfig(manual_desktop_close=True)
    runtime = feedback_cli.CliSessionRuntime(_build_args("manual-close"))
    result = await runtime.run()
    await runtime.shutdown()
    assert result == 0

    _FakeManager.scenario = _ScenarioConfig(timeout=True)
    with pytest.raises(TimeoutError):
        await feedback_cli._run_cli(_build_args("timeout", timeout=1))
    assert _FakeManager.stopped_ports
