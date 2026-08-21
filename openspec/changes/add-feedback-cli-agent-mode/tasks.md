## 1. CLI Entry and Parameter Contract

- [x] 1.1 Add `feedback-cli` command entrypoint while preserving existing MCP command entrypoints.
- [x] 1.2 Implement CLI argument parsing for `--project-directory`, `--summary`, `--summary-file`, and `--timeout`.
- [x] 1.3 Enforce parameter validation: reject simultaneous `--summary` and `--summary-file`.
- [x] 1.4 Align default values and timeout/error display semantics with current MCP behavior.

## 2. Session Isolation and Runtime Startup

- [x] 2.1 Ensure one CLI invocation creates one isolated session runtime (manager, session, backend, UI context).
- [x] 2.2 Update CLI backend startup to bind local port `0` and propagate resolved runtime URL.
- [x] 2.3 Remove fixed `127.0.0.1:8765` dependency from desktop/web frontend retry and redirect path.
- [x] 2.4 Verify concurrent CLI instances use independent ports and do not cross-route feedback.

## 3. UI Launch Strategy and Waiting Behavior

- [x] 3.1 Implement desktop-first launch path for CLI mode.
- [x] 3.2 Add browser fallback when desktop app cannot start or exits immediately during startup.
- [x] 3.3 Keep CLI waiting when user manually closes desktop window (no cancel, no auto-reopen).
- [x] 3.4 Keep session result routing and output semantics consistent across desktop and browser paths.

## 4. Result Formatting, Image Persistence, and Cleanup

- [x] 4.1 Normalize feedback result fields so command logs are consistently rendered in output text.
- [x] 4.2 Persist returned images to session-scoped temp directories and include file paths in CLI output.
- [x] 4.3 Implement TTL cleanup for expired image artifacts (default 1 hour) on CLI start/exit.
- [x] 4.4 Ensure normal-mode child-process logs do not pollute feedback stdout.

## 5. Lifecycle Hardening and Validation

- [x] 5.1 Replace unconsumed child-process PIPE behavior with safe output handling.
- [x] 5.2 Implement bounded shutdown flow for backend and UI child process on success, timeout, and interruption.
- [x] 5.3 Add process-group-aware cleanup strategy to reduce orphan windows after forced termination.
- [x] 5.4 Execute acceptance verification: 3 concurrent instances, timeout path, manual window close path, desktop-fallback path, and image TTL cleanup path.
