## Context

`agent-cli-control` introduces CLI-side control over web feedback sessions, but the current design leaves race conditions and lifecycle gaps unresolved. The same session can be completed by web submit, CLI submit, or timeout, and these paths currently do not share one atomic commit boundary. In addition, instance registry data can become stale or inconsistent when port binding changes, cleanup runs through different paths, or process crashes leave residual files.

The change must preserve current session retention behavior, avoid broad architecture rewrites, and remain compatible with no-auth local operation while reducing accidental misuse.

## Goals / Non-Goals

**Goals:**
- Guarantee deterministic first-success submission across web, CLI, and timeout paths.
- Prevent late submissions from overwriting already committed feedback.
- Make instance registry lifecycle consistent with existing session lifecycle and all cleanup paths.
- Ensure CLI routing points to the actual bound host/port and supports idempotent retry.
- Define explicit API contract and validation for predictable CLI behavior.
- Keep browser UX consistent with arbitration outcome without forcing non-desktop window close.

**Non-Goals:**
- Introducing a new authentication system in this iteration.
- Redesigning the entire feedback session state machine beyond required submission-state alignment.
- Changing current retention duration or global cleanup policy semantics.

## Decisions

### 1) Unified atomic submission gate
- Decision: Add a session-level atomic "claim-and-submit" path used by web submit, CLI submit, and timeout completion.
- Rationale: State checks alone are insufficient under concurrent arrivals; shared atomic ownership prevents overwrite.
- Alternative considered: Separate checks per endpoint + optimistic writes. Rejected due to unavoidable race windows.

### 2) Explicit winner semantics
- Decision: "First success" means first request that passes validation and commits feedback/result state.
- Rationale: Arrival order without successful commit should not reserve ownership.
- Alternative considered: First request arrival timestamp. Rejected because failed early attempts could block valid later submissions.

### 3) Idempotent CLI retry contract
- Decision: CLI submit accepts a request identifier and stores outcome for replay on retry of the same request id.
- Rationale: Network timeout may hide a successful commit from caller; retry must return original outcome, not duplicate or conflict incorrectly.
- Alternative considered: Stateless retry with conflict-only handling. Rejected due to poor operator experience and ambiguity.

### 4) Centralized instance registry manager
- Decision: Implement a dedicated registry module for create/update/read/delete and require all cleanup paths to call it.
- Rationale: Cleanup currently runs through synchronous and asynchronous flows; centralizing prevents stale drift.
- Alternative considered: Inline file operations in multiple call sites. Rejected for high maintenance and missed-path risk.

### 5) Register final bind target only
- Decision: Write or refresh registry entry only after server binds successfully to final host/port.
- Rationale: Pre-bind values can diverge if port rebinding occurs.
- Alternative considered: Pre-write in session creation and best-effort later fixup. Rejected due to route-to-dead-port risk.

### 6) Stale cleanup based on process identity
- Decision: Persist `pid` and process start time in registry; delete stale entry only when process absence or identity mismatch is confirmed.
- Rationale: CLI invocation memory is not shared across runs; retry counters cannot be trusted for stale judgment.
- Alternative considered: "N consecutive failures" in CLI memory. Rejected because it is non-persistent and unreliable.

### 7) Session visibility contract
- Decision: `list` returns actionable (still-submittable) sessions only; `status` returns full lifecycle.
- Rationale: Operators need fast action list by default while keeping deep inspection available.
- Alternative considered: Show all sessions in `list`. Rejected due to noise and ambiguous actionability.

### 8) Host exposure constraint
- Decision: Without auth, default and enforced route target remains loopback.
- Rationale: Prevent external process spoofing through open bind addresses.
- Alternative considered: Allow arbitrary host binding immediately. Rejected until auth boundary exists.

## Risks / Trade-offs

- [Increased state handling complexity] -> Keep submission gate API minimal and unit-test concurrency boundaries directly.
- [Extra storage for request outcome replay] -> Use bounded retention tied to existing session lifecycle cleanup.
- [Potential mismatch between desktop and browser UX expectations] -> Explicitly document: browser shows submitted/conflict state, desktop may auto-close.
- [Process start time retrieval differences across platforms] -> Wrap platform-specific retrieval in utility with fallback behavior and tests.
- [Backward compatibility risk for existing CLI assumptions] -> Preserve existing fields where possible and version/extend response schema conservatively.

## Migration Plan

1. Introduce atomic submission gate in session model and route all three submit/timeout paths through it.
2. Add registry manager module and replace direct file operations in startup and cleanup paths.
3. Move registry write timing to post-bind final host/port and refresh on rebind.
4. Add request-id replay handling and response schema updates for CLI submit.
5. Enforce validation and consistent error contracts across new CLI-facing routes.
6. Update front-end conflict display and docs (`agent-cli-control.md`) to match finalized semantics.
7. Add and run concurrency, lifecycle, and routing tests; verify no retention policy regression.

Rollback strategy: feature-flag or guarded code path can revert to current submit and routing behavior if severe regressions appear.

## Open Questions

- None blocking after current decisions; implementation should proceed with the agreed defaults.
