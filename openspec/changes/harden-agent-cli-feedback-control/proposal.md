## Why

The current `agent-cli-control` design is directionally correct but not safe enough to implement as-is. Several gaps around concurrent submission, instance routing, and stale cleanup can cause wrong feedback delivery or inconsistent session state, so we need a tighter and explicit contract before coding.

## What Changes

- Define a single first-writer-wins submission path shared by CLI submission, web submission, and timeout handling.
- Add an atomic submit-right claim so only the earliest successful commit becomes final and late arrivals return conflict.
- Centralize registry file lifecycle (create/read/delete) in one module and call it from all session cleanup paths.
- Persist final listening host/port only after server bind succeeds, and refresh registry data if bind target changes.
- Define stale entry cleanup using persisted process identity (`pid` + process start time), not in-memory retry counters.
- Clarify observable session semantics: `list` shows actionable sessions, `status` shows full lifecycle, and retry after client timeout is idempotent.
- Require API validation for required fields, type/length limits, and stable error contracts (`400/404/409`).
- Align browser behavior with competition rules: popup may remain open, but losing side sees a clear "already submitted by another agent" result.
- Restrict unauthenticated HTTP surface to loopback binding until an auth scheme is introduced.
- Add focused tests for concurrency, registry lifecycle, rebinding, stale cleanup, and multi-instance routing.

## Capabilities

### New Capabilities
- `feedback-submission-arbitration`: Deterministic first-success arbitration for CLI, web UI, and timeout on the same session.
- `feedback-instance-registry-routing`: Reliable cross-process session discovery and stale registry cleanup based on persisted process identity.
- `feedback-cli-http-contract`: CLI-oriented HTTP endpoints with validation, idempotent retry behavior, and explicit status semantics.

### Modified Capabilities
- None.

## Impact

- Affected backend components: `web/models/feedback_session.py`, `web/routes/main_routes.py`, `web/main.py`, and new registry helper module(s).
- Affected frontend behavior: `web/static/js/app.js` submission conflict display for non-winning clients.
- Affected CLI contract: target resolution, request validation, conflict handling, and retry semantics.
- Affected tests: session race tests, stale registry tests, final-port registration tests, and endpoint contract tests.
- Affected docs: `docs/design/agent-cli-control.md` must be updated to match the finalized rules.
