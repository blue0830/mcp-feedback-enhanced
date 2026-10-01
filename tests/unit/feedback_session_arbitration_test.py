#!/usr/bin/env python3
"""Tests for the first-success arbitration of WebFeedbackSession.

Covers the shared claim-and-submit path used by the web page, the remote channel and
the timeout paths, plus the desktop close callback and the manager loop bridge.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

import mcp_feedback_enhanced.web.main as web_main
from mcp_feedback_enhanced.web.models import (
    SOURCE_CLEANUP,
    SOURCE_USER_TIMEOUT,
    SOURCE_WAIT_TIMEOUT,
    SOURCE_WEB,
    CleanupReason,
    SessionStatus,
    WebFeedbackSession,
)
from mcp_feedback_enhanced.web.models.feedback_session import (
    REASON_ALREADY_DECIDED,
    REASON_CLOSED,
    REASON_INVALID,
)


REMOTE_SOURCE = "remote:fake"


class _FakeWebSocket:
    """Records the payloads sent to the page and the event loop that sent them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.loops.append(asyncio.get_running_loop())
        self.sent.append(payload)


@pytest.fixture
def session(test_project_dir):
    created = WebFeedbackSession("arb-session", str(test_project_dir), "summary")
    yield created
    created.cleanup()  # Cancels the auto-cleanup and user-timeout timers


def test_first_commit_wins_and_status_is_aligned(session):
    first = session.commit_feedback("one", [], {}, SOURCE_WEB)
    second = session.commit_feedback("two", [], {}, REMOTE_SOURCE)

    assert first.accepted
    assert first.source == SOURCE_WEB
    assert not second.accepted
    assert second.source == SOURCE_WEB
    assert second.reason == REASON_ALREADY_DECIDED
    assert session.feedback_result == "one"
    assert session.feedback_source == SOURCE_WEB
    assert session.status == SessionStatus.FEEDBACK_SUBMITTED
    assert session.feedback_completed.is_set()


def test_invalid_payload_does_not_reserve_the_outcome(session):
    rejected = session.commit_feedback(None, [], {}, SOURCE_WEB)
    assert not rejected.accepted
    assert rejected.reason == REASON_INVALID
    assert session.committed_source is None
    assert not session.feedback_completed.is_set()

    accepted = session.commit_feedback("ok", [], None, REMOTE_SOURCE)
    assert accepted.accepted
    assert session.feedback_source == REMOTE_SOURCE


def test_terminal_session_rejects_commit(session):
    session.set_error("broken")
    result = session.commit_feedback("late", [], {}, SOURCE_WEB)
    assert not result.accepted
    assert result.reason == REASON_CLOSED
    assert session.committed_source is None


def test_timeout_claim_blocks_late_reply(session):
    claim = session.claim_timeout(SOURCE_WAIT_TIMEOUT, "deadline")
    late = session.commit_feedback("late", [], {}, REMOTE_SOURCE)

    assert claim.accepted
    assert session.status == SessionStatus.TIMEOUT
    assert not late.accepted
    assert late.source == SOURCE_WAIT_TIMEOUT
    assert session.feedback_result is None


def test_reply_before_timeout_claim_keeps_the_feedback(session):
    session.commit_feedback("reply", [], {}, REMOTE_SOURCE)
    claim = session.claim_user_timeout()

    assert not claim.accepted
    assert claim.source == REMOTE_SOURCE
    assert session.status == SessionStatus.FEEDBACK_SUBMITTED


def test_concurrent_commits_have_exactly_one_winner(session):
    thread_count = 16
    barrier = threading.Barrier(thread_count)
    results: list[Any] = []
    results_lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        result = session.commit_feedback(
            f"reply-{index}", [], {}, f"remote:fake{index}"
        )
        with results_lock:
            results.append(result)

    threads = [
        threading.Thread(target=attempt, args=(index,)) for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    winners = [result for result in results if result.accepted]
    assert len(results) == thread_count
    assert len(winners) == 1
    assert session.feedback_source == winners[0].source
    assert session.feedback_result == "reply-" + winners[0].source.removeprefix(
        "remote:fake"
    )


def test_commits_racing_with_timeouts_keep_a_coherent_state(session):
    barrier = threading.Barrier(12)
    results: list[Any] = []
    results_lock = threading.Lock()

    def commit(index: int) -> None:
        barrier.wait()
        outcome = session.commit_feedback(
            f"reply-{index}", [], {}, f"remote:fake{index}"
        )
        with results_lock:
            results.append(outcome)

    def timeout() -> None:
        barrier.wait()
        outcome = session.claim_user_timeout()
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=commit, args=(i,)) for i in range(8)]
    threads += [threading.Thread(target=timeout) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sum(1 for result in results if result.accepted) == 1
    if session.feedback_source is not None:
        assert session.status == SessionStatus.FEEDBACK_SUBMITTED
        assert session.feedback_result is not None
    else:
        assert session.status == SessionStatus.TIMEOUT
        assert session.feedback_result is None


@pytest.mark.asyncio
async def test_wait_returns_a_commit_from_another_thread(session):
    waiter = asyncio.create_task(session.wait_for_feedback(timeout=30))
    await asyncio.sleep(0.05)

    thread = threading.Thread(
        target=lambda: session.commit_feedback("from remote", [], {}, REMOTE_SOURCE)
    )
    thread.start()
    thread.join(timeout=5)

    result = await asyncio.wait_for(waiter, timeout=5)
    assert result["interactive_feedback"] == "from remote"


@pytest.mark.asyncio
async def test_reply_at_the_deadline_is_honored(session):
    class _RacyEvent:
        """Commits a reply right before reporting that the wait timed out."""

        def __init__(self, inner: threading.Event) -> None:
            self._inner = inner

        def wait(self, timeout: float | None = None) -> bool:
            session.commit_feedback("late but first", [], {}, REMOTE_SOURCE)
            return False

        def set(self) -> None:
            self._inner.set()

        def is_set(self) -> bool:
            return self._inner.is_set()

    session.feedback_completed = _RacyEvent(session.feedback_completed)  # type: ignore[assignment]

    result = await session.wait_for_feedback(timeout=30)

    assert result["interactive_feedback"] == "late but first"
    assert session.status == SessionStatus.FEEDBACK_SUBMITTED


@pytest.mark.asyncio
async def test_deadline_claims_the_timeout_and_rejects_later_replies(
    session, monkeypatch
):
    monkeypatch.setattr(
        WebFeedbackSession, "effective_wait_timeout", staticmethod(lambda _t: 0.1)
    )

    with pytest.raises(TimeoutError):
        await session.wait_for_feedback(timeout=30)

    assert session.committed_source == SOURCE_WAIT_TIMEOUT
    late = session.commit_feedback("too late", [], {}, REMOTE_SOURCE)
    assert not late.accepted
    assert session.feedback_result is None


@pytest.mark.asyncio
async def test_user_timeout_claim_ends_the_wait_with_timeout(session):
    waiter = asyncio.create_task(session.wait_for_feedback(timeout=30))
    await asyncio.sleep(0.05)

    claim = session.claim_user_timeout()

    assert claim.accepted
    assert claim.source == SOURCE_USER_TIMEOUT
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(waiter, timeout=5)


def test_user_timeout_timer_loses_against_an_earlier_reply(session):
    session.update_timeout_settings(enabled=True, timeout_seconds=3600)
    timer = session.user_timeout_timer
    assert timer is not None
    assert timer.daemon  # A pending timer must never keep the process alive

    session.commit_feedback("reply", [], {}, REMOTE_SOURCE)
    timer.function()  # Fire the timer handler late, as if the timeout had just elapsed

    assert session.status == SessionStatus.FEEDBACK_SUBMITTED
    assert session.feedback_source == REMOTE_SOURCE


def test_user_timeout_timer_wins_when_nothing_was_decided(session):
    session.update_timeout_settings(enabled=True, timeout_seconds=3600)
    timer = session.user_timeout_timer
    assert timer is not None

    timer.function()

    assert session.status == SessionStatus.TIMEOUT
    assert session.committed_source == SOURCE_USER_TIMEOUT
    assert session.feedback_completed.is_set()


def test_cleanup_seals_the_outcome(session):
    session._cleanup_sync_enhanced(CleanupReason.SHUTDOWN)

    late = session.commit_feedback("late", [], {}, SOURCE_WEB)

    assert not late.accepted
    assert late.source == SOURCE_CLEANUP


def test_websocket_preserving_cleanup_keeps_the_outcome_open(session):
    session._cleanup_sync()  # Used when a newer session replaces this one

    result = session.commit_feedback("still open", [], {}, SOURCE_WEB)

    assert result.accepted


@pytest.mark.asyncio
async def test_desktop_close_uses_injected_callback_not_the_global_manager(
    test_project_dir, monkeypatch
):
    def _forbidden():
        raise AssertionError("the global manager must not be used")

    monkeypatch.setattr(web_main, "_web_ui_manager", None)
    monkeypatch.setattr(web_main, "get_web_ui_manager", _forbidden)
    monkeypatch.setenv("MCP_DESKTOP_MODE", "true")

    closed: list[int] = []
    owned = WebFeedbackSession(
        "desktop-session",
        str(test_project_dir),
        "summary",
        close_desktop_callback=lambda: closed.append(1),
    )
    owned.websocket = _FakeWebSocket()  # type: ignore[assignment]
    try:
        result = await owned.submit_feedback("done", [], {})
    finally:
        owned.cleanup()

    assert result.accepted
    assert closed == [1]
    assert web_main._web_ui_manager is None  # No stray default manager was created


@pytest.mark.asyncio
async def test_submit_without_close_callback_is_safe(test_project_dir, monkeypatch):
    monkeypatch.setenv("MCP_DESKTOP_MODE", "true")
    bare = WebFeedbackSession("bare-session", str(test_project_dir), "summary")
    bare.websocket = _FakeWebSocket()  # type: ignore[assignment]
    try:
        result = await bare.submit_feedback("done", [], {})
    finally:
        bare.cleanup()

    assert result.accepted


@pytest.mark.asyncio
async def test_successful_submit_notifies_the_page_with_its_source(session):
    page = _FakeWebSocket()
    session.websocket = page

    await session.submit_feedback("hello", [], {}, source=REMOTE_SOURCE)

    assert page.sent[0]["code"] == "session.feedbackSubmitted"
    assert page.sent[0]["source"] == REMOTE_SOURCE


@pytest.mark.asyncio
async def test_losing_submit_notifies_the_page_with_a_conflict(session):
    page = _FakeWebSocket()
    session.websocket = page
    session.commit_feedback("remote first", [], {}, REMOTE_SOURCE)

    result = await session.submit_feedback("web second", [], {})

    assert not result.accepted
    assert page.sent[-1]["code"] == "session.feedbackConflict"
    assert page.sent[-1]["winner"] == REMOTE_SOURCE
    assert session.feedback_result == "remote first"


@pytest.mark.asyncio
async def test_invalid_submit_sends_no_conflict_notice(session):
    page = _FakeWebSocket()
    session.websocket = page

    result = await session.submit_feedback(None, [], {})

    assert result.reason == REASON_INVALID
    assert page.sent == []


@pytest.mark.asyncio
async def test_manager_bridges_a_commit_onto_the_server_loop(
    web_ui_manager, test_project_dir
):
    manager = web_ui_manager
    manager.create_session(str(test_project_dir), "summary")
    session = manager.get_current_session()
    page = _FakeWebSocket()
    session.websocket = page
    manager.start_server()
    try:
        server_loop = manager._server_loop
        result = session.commit_feedback("remote text", [], {}, REMOTE_SOURCE)
        await manager.await_on_server_loop(
            session.notify_commit_result(result, REMOTE_SOURCE), timeout=5
        )

        assert server_loop is not None
        assert page.sent[0]["code"] == "session.feedbackSubmitted"
        assert page.loops[0] is server_loop
        assert page.loops[0] is not asyncio.get_running_loop()
    finally:
        manager.stop()


@pytest.mark.asyncio
async def test_manager_close_callback_reaches_its_own_desktop_instance(
    web_ui_manager, test_project_dir, monkeypatch
):
    monkeypatch.setenv("MCP_DESKTOP_MODE", "true")

    class _Desktop:
        stopped = 0

        def stop(self) -> None:
            type(self).stopped += 1

    manager = web_ui_manager
    manager.desktop_app_instance = _Desktop()
    manager.create_session(str(test_project_dir), "summary")
    session = manager.get_current_session()
    session.websocket = _FakeWebSocket()
    try:
        await session.submit_feedback("done", [], {})
    finally:
        session.cleanup()

    assert _Desktop.stopped == 1
    assert manager.desktop_app_instance is None


@pytest.mark.asyncio
async def test_await_on_server_loop_without_a_running_server_raises(web_ui_manager):
    async def _never_runs() -> None:
        return None

    with pytest.raises(RuntimeError):
        await web_ui_manager.await_on_server_loop(_never_runs())


@pytest.mark.asyncio
async def test_remote_status_is_stored_and_pushed_to_the_page(
    web_ui_manager, test_project_dir
):
    manager = web_ui_manager
    manager.create_session(str(test_project_dir), "summary")
    session = manager.get_current_session()
    page = _FakeWebSocket()
    session.websocket = page
    manager.start_server()
    try:
        manager.push_remote_status(session, {"state": "waiting", "provider": "fake"})

        deadline = time.time() + 5
        while time.time() < deadline and not page.sent:
            await asyncio.sleep(0.02)

        assert session.remote_status == {"state": "waiting", "provider": "fake"}
        assert page.sent == [
            {
                "type": "remote_status",
                "status": {"state": "waiting", "provider": "fake"},
            }
        ]
    finally:
        manager.stop()
