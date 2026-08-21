## 1. Submission Arbitration Core

- [ ] 1.1 Add a session-level atomic claim-and-submit API in `feedback_session.py` for web, CLI, and timeout callers.
- [ ] 1.2 Route existing web submit and timeout completion paths to the new shared arbitration API.
- [ ] 1.3 Enforce winner semantics (first successful commit wins) and return conflict for non-winning commits.
- [ ] 1.4 Align session state transitions so successful submit paths enter the committed/submitted final state consistently.

## 2. Registry Lifecycle and Routing

- [ ] 2.1 Create a dedicated instance registry manager module for create/read/update/delete operations.
- [ ] 2.2 Move registry write timing to post-bind success and persist final host/port values only.
- [ ] 2.3 Update all cleanup paths (sync, async, session replacement, memory-pressure, shutdown) to use registry manager deletion.
- [ ] 2.4 Add persisted process identity fields (`pid`, process start time) and stale detection logic based on verified mismatch/absence.

## 3. CLI HTTP Contract and Validation

- [ ] 3.1 Implement `list` contract to return only currently submittable sessions.
- [ ] 3.2 Implement `status` contract to return full lifecycle details for a specified session.
- [ ] 3.3 Add submit payload validation (required fields, type checks, max feedback length) with structured `400/404/409` responses.
- [ ] 3.4 Add request-id based idempotent retry handling so repeated submit calls can replay original outcome.
- [ ] 3.5 Enforce loopback-only exposure for unauthenticated CLI-facing submission routes.

## 4. Frontend Behavior Alignment

- [ ] 4.1 Update web submit result handling to show explicit conflict/already-submitted feedback when losing arbitration.
- [ ] 4.2 Preserve desktop-mode auto-close behavior while keeping standard browser flow non-forced-close.

## 5. Testing and Documentation

- [ ] 5.1 Add concurrency tests for CLI-vs-web and submit-vs-timeout race outcomes.
- [ ] 5.2 Add tests for port rebinding and registry final-port correctness.
- [ ] 5.3 Add tests for registry cleanup coverage across all cleanup paths and stale record handling.
- [ ] 5.4 Add endpoint contract tests for validation errors and idempotent retry behavior.
- [ ] 5.5 Update `docs/design/agent-cli-control.md` to match finalized arbitration, lifecycle, and contract rules.
