#!/usr/bin/env python3
"""Connection check ("test" button) for remote channel settings.

Responsibilities:
- model one check run as ordered steps with a per-step result for the settings card;
- run at most one check at a time in the background and keep recent runs pollable (a
  provider check waits for a human reply, so it can take up to two minutes);
- store the verification fingerprint only after a fully successful run whose settings
  are still the stored ones.

Limitations:
- runs live in the memory of the window process that started them; a window that goes
  away abandons its run (providers clean their test conversation up on a best-effort
  basis);
- ``ConnectionCheckRun`` is mutated from one event loop only (the web server loop), so
  it carries no locking;
- step details are English diagnostics and must never contain secrets.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..debug import server_debug_log as debug_log
from .config import ConfigError, RemoteChannelConfig, RemoteConfigStore
from .models import RemoteChannelError


# Finished runs kept for polling; older ones are dropped.
MAX_KEPT_RUNS = 5
# Upper bound for cancelling a running check while the window shuts down.
ABORT_TIMEOUT_SECONDS = 3.0


class CheckStepState(StrEnum):
    """Progress of one check step."""

    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CheckStep:
    """One step of a check; ``reason`` is a short code the page maps to localized text."""

    id: str
    state: CheckStepState = CheckStepState.PENDING
    reason: str | None = None
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "reason": self.reason,
            "detail": self.detail,
        }


class ConnectionCheckRun:
    """Progress record of one connection check.

    Steps are declared up front so the page can show all of them from the start.
    ``optional_steps`` (for example cleanup) may fail without failing the check.
    """

    def __init__(
        self,
        provider: str,
        step_ids: Sequence[str],
        optional_steps: Sequence[str] = (),
    ) -> None:
        self.run_id = uuid.uuid4().hex
        self.provider = provider
        self.steps = [CheckStep(step_id) for step_id in step_ids]
        self.optional_steps = frozenset(optional_steps)
        self.started_at = time.time()
        self.done = False
        # Set when the stored settings changed while the run was in progress.
        self.verify_error: str | None = None
        self.verified = False
        # Seconds the provider waits for the human reply (shown by the page).
        self.reply_timeout_seconds: int | None = None

    def _step(self, step_id: str) -> CheckStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def begin(self, step_id: str) -> None:
        self._step(step_id).state = CheckStepState.RUNNING

    def succeed(self, step_id: str, detail: str | None = None) -> None:
        step = self._step(step_id)
        step.state = CheckStepState.OK
        step.reason = None
        step.detail = detail

    def fail(self, step_id: str, reason: str, detail: str | None = None) -> None:
        """Fail one step; every step after it that never started is skipped."""
        step = self._step(step_id)
        step.state = CheckStepState.FAILED
        step.reason = reason
        step.detail = detail
        for later in self.steps:
            if later.state == CheckStepState.PENDING:
                later.state = CheckStepState.SKIPPED

    def fail_running(self, reason: str, detail: str | None = None) -> None:
        """Fail whichever step is running (or the first pending one) after a crash."""
        for step in self.steps:
            if step.state == CheckStepState.RUNNING:
                self.fail(step.id, reason, detail)
                return
        for step in self.steps:
            if step.state == CheckStepState.PENDING:
                self.fail(step.id, reason, detail)
                return

    @property
    def passed(self) -> bool:
        """Every required step succeeded (optional steps may have failed)."""
        return all(
            step.state == CheckStepState.OK
            for step in self.steps
            if step.id not in self.optional_steps
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "done": self.done,
            "passed": self.done and self.passed,
            "verified": self.verified,
            "verify_error": self.verify_error,
            "reply_timeout_seconds": self.reply_timeout_seconds,
            "steps": [step.to_payload() for step in self.steps],
        }


class ConnectionChecker(Protocol):
    """Provider side of a check: drives the steps of one ``ConnectionCheckRun``."""

    # Read-only on purpose: providers declare them as plain class attributes.
    @property
    def provider(self) -> str: ...

    @property
    def step_ids(self) -> Sequence[str]: ...

    @property
    def optional_steps(self) -> Sequence[str]: ...

    async def run(self, check: ConnectionCheckRun) -> None:
        """Execute the steps, reporting progress on ``check``; must clean up after itself."""
        ...


class CheckBusyError(Exception):
    """A check is already running; ``run_id`` identifies it."""

    def __init__(self, run_id: str) -> None:
        super().__init__("A connection check is already running")
        self.run_id = run_id


class ConnectionCheckService:
    """Start, track and finish connection checks for one window.

    Notes:
    - ``start`` must run on the event loop that will own the background task;
    - only one check runs at a time, so a double click cannot open two test posts;
    - the verification fingerprint is written through ``RemoteConfigStore.mark_verified``
      which refuses it when the settings changed during the run.
    """

    def __init__(
        self,
        store: RemoteConfigStore,
        checker_factory: Callable[[RemoteChannelConfig], ConnectionChecker],
    ) -> None:
        self._store = store
        self._checker_factory = checker_factory
        self._runs: OrderedDict[str, ConnectionCheckRun] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._active: ConnectionCheckRun | None = None

    def start(self, config: RemoteChannelConfig) -> ConnectionCheckRun:
        """Begin a check of ``config`` in the background.

        Raises:
            ConfigError: the configuration is incomplete.
            CheckBusyError: another check is still running.
            RemoteChannelError: the provider is not supported.
        """
        if self._active is not None and not self._active.done:
            raise CheckBusyError(self._active.run_id)
        missing = config.missing_field()
        if missing:
            raise ConfigError(missing)

        checker = self._checker_factory(config)
        check = ConnectionCheckRun(
            checker.provider, checker.step_ids, checker.optional_steps
        )
        self._active = check
        self._runs[check.run_id] = check
        while len(self._runs) > MAX_KEPT_RUNS:
            self._runs.popitem(last=False)
        self._task = asyncio.get_running_loop().create_task(
            self._execute(check, checker, config)
        )
        return check

    def get(self, run_id: str) -> ConnectionCheckRun | None:
        return self._runs.get(run_id)

    async def _execute(
        self,
        check: ConnectionCheckRun,
        checker: ConnectionChecker,
        config: RemoteChannelConfig,
    ) -> None:
        """Run the provider check and, on success, store the verification."""
        try:
            try:
                await checker.run(check)
            except asyncio.CancelledError:
                check.fail_running("cancelled")
                raise
            except RemoteChannelError as error:
                check.fail_running(error.reason, str(error))
            except Exception as error:
                # A provider bug must not kill the window; report it on the running step.
                debug_log(f"遠端連線檢查發生未預期錯誤: {type(error).__name__}")
                check.fail_running("internal_error", type(error).__name__)

            if check.passed:
                try:
                    self._store.mark_verified(config)
                    check.verified = True
                except ConfigError as error:
                    check.verify_error = error.code
                except Exception as error:
                    debug_log(f"寫入驗證結果失敗: {type(error).__name__}")
                    check.verify_error = "save_failed"
        finally:
            # A run that never finishes would block every later check of this window.
            check.done = True

    async def aclose(self, timeout: float = ABORT_TIMEOUT_SECONDS) -> None:
        """Cancel a running check so the provider can clean up (bounded, never raises)."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        # asyncio.wait neither raises for the task's own cancellation nor after the
        # timeout, which is exactly the bounded best-effort wait needed here.
        await asyncio.wait({task}, timeout=timeout)
