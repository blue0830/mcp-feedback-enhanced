#!/usr/bin/env python3
"""Coordinator tests driven by a fake provider that implements only the three operations."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from mcp_feedback_enhanced.remote import (
    REMOTE_SOURCE_PREFIX,
    RemoteChannel,
    RemoteChannelError,
    RemoteHandle,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    RemoteSessionCoordinator,
    RemoteState,
    RemoteStatus,
    StateReporter,
    SubmitResult,
)


def _request(session_id: str = "abcdef12-0000-0000-0000-000000000000") -> RemoteRequest:
    return RemoteRequest(
        session_id=session_id,
        project_directory="D:/proj",
        summary="summary",
        deadline_epoch=time.time() + 600,
        timeout_seconds=600,
    )


class FakeChannel:
    """Minimal provider: open / wait_reply / close, all driven by the test."""

    provider = "fake"

    def __init__(
        self,
        *,
        open_errors: list[Exception] | None = None,
        wait_error: Exception | None = None,
        close_delay: float = 0.0,
        open_gate: asyncio.Event | None = None,
    ) -> None:
        self.open_errors = list(open_errors or [])
        self.wait_error = wait_error
        self.close_delay = close_delay
        self.open_gate = open_gate
        self.open_calls = 0
        self.closed: list[tuple[str, RemoteOutcome]] = []
        self.reply_event = asyncio.Event()
        self.reply = RemoteReply(text="remote says hi", author_id="1")
        self.report_script: list[tuple[RemoteState, str | None]] = []
        self.wait_cancelled = False

    async def open(self, request: RemoteRequest) -> RemoteHandle:
        self.open_calls += 1
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_errors:
            raise self.open_errors.pop(0)
        return RemoteHandle(self.provider, f"conv-{request.short_id}")

    async def wait_reply(
        self, handle: RemoteHandle, report: StateReporter
    ) -> RemoteReply:
        if self.wait_error is not None:
            raise self.wait_error
        for state, reason in self.report_script:
            report(state, reason)
        try:
            await self.reply_event.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        return self.reply

    async def close(self, handle: RemoteHandle, outcome: RemoteOutcome) -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed.append((handle.conversation_id, outcome))


class Recorder:
    """Collects statuses and submissions made by a coordinator."""

    def __init__(self, accept: bool = True) -> None:
        self.statuses: list[RemoteStatus] = []
        self.submissions: list[tuple[str, str]] = []
        self.accept = accept
        self.submit_error: Exception | None = None

    def report(self, status: RemoteStatus) -> None:
        self.statuses.append(status)

    async def submit(self, reply: RemoteReply, source: str) -> SubmitResult:
        if self.submit_error is not None:
            raise self.submit_error
        self.submissions.append((reply.text, source))
        return SubmitResult(self.accept, None if self.accept else "web")

    @property
    def states(self) -> list[RemoteState]:
        return [status.state for status in self.statuses]


def _coordinator(
    channel: FakeChannel,
    recorder: Recorder,
    request: RemoteRequest | None = None,
    **kwargs: Any,
) -> RemoteSessionCoordinator:
    return RemoteSessionCoordinator(
        channel,
        request or _request(),
        submit=recorder.submit,
        report_status=recorder.report,
        **kwargs,
    )


async def _settle(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


def test_fake_channel_satisfies_the_three_operation_protocol():
    assert isinstance(FakeChannel(), RemoteChannel)


@pytest.mark.asyncio
async def test_remote_reply_first_is_submitted_with_the_provider_source():
    channel, recorder = FakeChannel(), Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    channel.reply_event.set()
    await _settle(lambda: RemoteState.ANSWERED_REMOTELY in recorder.states)
    await coordinator.finalize(RemoteOutcome.REMOTE_ANSWERED)

    assert recorder.submissions == [("remote says hi", f"{REMOTE_SOURCE_PREFIX}fake")]
    assert recorder.states[:3] == [
        RemoteState.CONNECTING,
        RemoteState.WAITING,
        RemoteState.ANSWERED_REMOTELY,
    ]
    assert channel.closed == [("conv-abcdef12", RemoteOutcome.REMOTE_ANSWERED)]


@pytest.mark.asyncio
async def test_local_answer_first_cancels_the_wait_and_closes_as_local():
    channel, recorder = FakeChannel(), Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)

    assert channel.wait_cancelled
    assert recorder.submissions == []
    assert channel.closed == [("conv-abcdef12", RemoteOutcome.LOCAL_ANSWERED)]
    assert recorder.states[-1] == RemoteState.ANSWERED_LOCALLY


@pytest.mark.asyncio
async def test_lost_arbitration_is_not_reported_as_answered_remotely():
    channel, recorder = FakeChannel(), Recorder(accept=False)
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    channel.reply_event.set()
    await _settle(lambda: bool(recorder.submissions))
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)

    assert RemoteState.ANSWERED_REMOTELY not in recorder.states
    assert recorder.states[-1] == RemoteState.ANSWERED_LOCALLY
    assert channel.closed == [("conv-abcdef12", RemoteOutcome.LOCAL_ANSWERED)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [RemoteOutcome.TIMEOUT, RemoteOutcome.INTERRUPTED, RemoteOutcome.ERROR],
)
async def test_every_end_path_closes_the_conversation_with_its_outcome(outcome):
    channel, recorder = FakeChannel(), Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    await coordinator.finalize(outcome)

    assert channel.closed == [("conv-abcdef12", outcome)]


@pytest.mark.asyncio
async def test_start_does_not_wait_for_the_network():
    gate = asyncio.Event()  # open() blocks until the test releases it
    channel, recorder = FakeChannel(open_gate=gate), Recorder()
    coordinator = _coordinator(channel, recorder)

    started = time.monotonic()
    coordinator.start()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert recorder.states == [RemoteState.CONNECTING]
    gate.set()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    await coordinator.finalize(RemoteOutcome.INTERRUPTED)


@pytest.mark.asyncio
async def test_transient_open_failure_reports_unavailable_then_recovers():
    channel = FakeChannel(
        open_errors=[
            RemoteChannelError("net down", reason="network_error"),
            RemoteChannelError("net down", reason="network_error"),
        ]
    )
    recorder = Recorder()
    coordinator = _coordinator(channel, recorder, open_retry_delays=(0.01,))

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    await coordinator.finalize(RemoteOutcome.INTERRUPTED)

    assert channel.open_calls == 3
    unavailable = [s for s in recorder.statuses if s.state == RemoteState.UNAVAILABLE]
    assert unavailable
    assert unavailable[0].reason == "network_error"
    assert recorder.states[-1] == RemoteState.WAITING


@pytest.mark.asyncio
async def test_permanent_open_failure_stops_without_retry_and_without_close():
    channel = FakeChannel(
        open_errors=[
            RemoteChannelError("bad token", permanent=True, reason="auth_failed")
        ]
    )
    recorder = Recorder()
    coordinator = _coordinator(channel, recorder, open_retry_delays=(0.01,))

    coordinator.start()
    await _settle(lambda: RemoteState.UNAVAILABLE in recorder.states)
    await asyncio.sleep(0.1)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)

    assert channel.open_calls == 1
    assert recorder.statuses[-2].reason == "auth_failed"
    assert channel.closed == []


@pytest.mark.asyncio
async def test_open_retry_stops_when_the_deadline_would_pass():
    channel = FakeChannel(
        open_errors=[RemoteChannelError("net", reason="network_error")] * 50
    )
    recorder = Recorder()
    request = _request()
    request = RemoteRequest(
        session_id=request.session_id,
        project_directory=request.project_directory,
        summary=request.summary,
        deadline_epoch=time.time() + 0.05,
        timeout_seconds=1,
    )
    coordinator = _coordinator(channel, recorder, request, open_retry_delays=(1.0,))

    coordinator.start()
    await _settle(lambda: RemoteState.UNAVAILABLE in recorder.states)
    await asyncio.sleep(0.1)
    await coordinator.finalize(RemoteOutcome.TIMEOUT)

    assert channel.open_calls == 1


@pytest.mark.asyncio
async def test_permanent_wait_failure_is_reported_and_conversation_still_closed():
    channel = FakeChannel(
        wait_error=RemoteChannelError("forbidden", permanent=True, reason="auth_failed")
    )
    recorder = Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.UNAVAILABLE in recorder.states)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)

    assert recorder.statuses[-2].state == RemoteState.UNAVAILABLE
    assert recorder.statuses[-2].reason == "auth_failed"
    assert channel.closed == [("conv-abcdef12", RemoteOutcome.LOCAL_ANSWERED)]


@pytest.mark.asyncio
async def test_unexpected_provider_error_is_isolated():
    channel = FakeChannel(wait_error=ZeroDivisionError("bug"))
    recorder = Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.UNAVAILABLE in recorder.states)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)

    assert recorder.statuses[-2].reason == "internal_error"


@pytest.mark.asyncio
async def test_provider_reported_transient_failure_and_recovery_reach_the_window():
    channel, recorder = FakeChannel(), Recorder()
    channel.report_script = [
        (RemoteState.UNAVAILABLE, "network_error"),
        (RemoteState.WAITING, None),
    ]
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(
        lambda: (
            recorder.states.count(RemoteState.WAITING) >= 1
            and RemoteState.UNAVAILABLE in recorder.states
        )
    )
    await coordinator.finalize(RemoteOutcome.INTERRUPTED)

    assert recorder.states[:4] == [
        RemoteState.CONNECTING,
        RemoteState.WAITING,
        RemoteState.UNAVAILABLE,
        RemoteState.WAITING,
    ]


@pytest.mark.asyncio
async def test_submit_failure_is_reported_and_does_not_crash():
    channel, recorder = FakeChannel(), Recorder()
    recorder.submit_error = RuntimeError("loop closed")
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    channel.reply_event.set()
    await _settle(lambda: RemoteState.UNAVAILABLE in recorder.states)
    await coordinator.finalize(RemoteOutcome.ERROR)

    assert recorder.statuses[-1].reason == "submit_failed"


@pytest.mark.asyncio
async def test_finalize_is_bounded_when_the_provider_close_hangs():
    channel, recorder = FakeChannel(close_delay=30), Recorder()
    coordinator = _coordinator(channel, recorder, finalize_timeout=0.1)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    started = time.monotonic()
    await coordinator.finalize(RemoteOutcome.TIMEOUT)

    assert time.monotonic() - started < 1.0
    assert channel.closed == []


@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_start_after_finalize_is_a_no_op():
    channel, recorder = FakeChannel(), Recorder()
    coordinator = _coordinator(channel, recorder)

    coordinator.start()
    await _settle(lambda: RemoteState.WAITING in recorder.states)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)
    await coordinator.finalize(RemoteOutcome.LOCAL_ANSWERED)
    coordinator.start()

    assert len(channel.closed) == 1
    assert channel.open_calls == 1


@pytest.mark.asyncio
async def test_a_failing_status_callback_never_breaks_the_flow():
    channel = FakeChannel()

    def broken_report(_status: RemoteStatus) -> None:
        raise RuntimeError("ui gone")

    submissions: list[str] = []

    async def submit(reply: RemoteReply, _source: str) -> SubmitResult:
        submissions.append(reply.text)
        return SubmitResult(True)

    coordinator = RemoteSessionCoordinator(
        channel, _request(), submit=submit, report_status=broken_report
    )
    coordinator.start()
    await _settle(
        lambda: (
            coordinator.status is not None
            and coordinator.status.state == RemoteState.WAITING
        )
    )
    channel.reply_event.set()
    await _settle(lambda: bool(submissions))
    await coordinator.finalize(RemoteOutcome.REMOTE_ANSWERED)

    assert submissions == ["remote says hi"]


@pytest.mark.asyncio
async def test_three_concurrent_sessions_keep_their_replies_isolated():
    sessions = [f"{index}bcdef12-0000-0000-0000-000000000000" for index in range(3)]
    channels = [FakeChannel() for _ in sessions]
    for index, channel in enumerate(channels):
        channel.reply = RemoteReply(text=f"reply-for-{index}")
    recorders = [Recorder() for _ in sessions]
    coordinators = [
        _coordinator(channel, recorder, _request(session_id))
        for channel, recorder, session_id in zip(
            channels, recorders, sessions, strict=True
        )
    ]

    for coordinator in coordinators:
        coordinator.start()
    for recorder in recorders:
        await _settle(lambda recorder=recorder: RemoteState.WAITING in recorder.states)

    # Answer out of order: only the owning coordinator may receive each reply.
    for index in (2, 0, 1):
        channels[index].reply_event.set()
        await _settle(lambda index=index: bool(recorders[index].submissions))

    for coordinator in coordinators:
        await coordinator.finalize(RemoteOutcome.REMOTE_ANSWERED)

    for index, recorder in enumerate(recorders):
        assert recorder.submissions == [(f"reply-for-{index}", "remote:fake")]
    assert [channel.closed[0][0] for channel in channels] == [
        f"conv-{index}bcdef12" for index in range(3)
    ]
