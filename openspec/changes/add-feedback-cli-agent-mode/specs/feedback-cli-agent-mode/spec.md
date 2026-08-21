## ADDED Requirements

### Requirement: Agent-blocking CLI feedback entrypoint
The system MUST provide a `feedback-cli` command that starts one interactive feedback session and blocks until feedback result or timeout, then returns MCP-compatible text output.

#### Scenario: CLI invocation with explicit arguments
- **WHEN** Agent runs `feedback-cli --project-directory <path> --summary <text> --timeout <seconds>`
- **THEN** system starts one session and blocks current CLI process until session ends
- **THEN** system returns text result with the same user-visible semantics as current MCP feedback output

#### Scenario: CLI invocation with summary file
- **WHEN** Agent runs `feedback-cli --summary-file <file>`
- **THEN** system loads summary content from file and uses it as session context
- **THEN** system rejects simultaneous `--summary` and `--summary-file` as parameter error

### Requirement: Multi-instance session isolation by process
Each CLI invocation MUST create an isolated runtime context and MUST NOT share active session state with other CLI invocations.

#### Scenario: Three concurrent CLI calls
- **WHEN** Agent starts three `feedback-cli` processes concurrently
- **THEN** each process owns a unique session identity
- **THEN** each process receives only its own feedback result

#### Scenario: One instance closes while others continue
- **WHEN** one concurrent session ends or times out
- **THEN** remaining sessions continue waiting independently
- **THEN** no cross-session cancellation or result overwrite occurs

### Requirement: Desktop-first UI with browser fallback
The system MUST try desktop window first, MUST fallback to browser when desktop startup fails, and MUST keep waiting when user manually closes an opened window.

#### Scenario: Desktop startup failure
- **WHEN** desktop app is unavailable, crashes during startup, or exits immediately after launch
- **THEN** system opens browser for the same session URL
- **THEN** system continues waiting under the same session until feedback or timeout

#### Scenario: Manual window close
- **WHEN** user manually closes the desktop window after it was successfully opened
- **THEN** system does not mark session as canceled
- **THEN** CLI process keeps waiting until feedback submission or timeout

### Requirement: Runtime URL and port correctness for concurrent sessions
CLI session backend MUST bind to an OS-assigned ephemeral local port and frontend navigation MUST use the injected runtime URL instead of fixed port literals.

#### Scenario: Multiple sessions with dynamic ports
- **WHEN** multiple CLI sessions start in parallel
- **THEN** each session binds an independent local port allocated by OS
- **THEN** frontend connects to the session-specific runtime URL and does not hardcode `127.0.0.1:8765`

### Requirement: Timeout and error behavior compatibility
CLI timeout, default values, and user-facing error semantics MUST remain aligned with current MCP feedback behavior.

#### Scenario: Default timeout usage
- **WHEN** Agent omits `--timeout`
- **THEN** system applies current MCP default timeout value
- **THEN** timeout completion returns MCP-compatible timeout message semantics

#### Scenario: Invalid project directory
- **WHEN** Agent provides a non-existent project directory
- **THEN** system preserves current MCP-compatible handling behavior for project directory resolution
- **THEN** session summary view reflects the resolved project path used by runtime

### Requirement: Image path output with deferred cleanup
When session includes images, CLI MUST persist image files to session-scoped temp directories, return file paths in text output, and cleanup by TTL policy instead of immediate deletion.

#### Scenario: Image returned to Agent
- **WHEN** user submits feedback containing images
- **THEN** system writes image files to a session-specific temp directory
- **THEN** CLI output includes readable local file paths for those images

#### Scenario: Cleanup policy execution
- **WHEN** CLI starts or exits
- **THEN** system removes expired temp files based on configured TTL
- **THEN** system does not delete newly returned image files before Agent has chance to read paths

### Requirement: Robust child-process and resource cleanup
CLI runtime MUST avoid unconsumed subprocess pipes and MUST perform bounded cleanup for backend and desktop child processes across normal exit, timeout, and interruption paths.

#### Scenario: Subprocess output handling
- **WHEN** desktop child process produces continuous stdout/stderr output
- **THEN** runtime does not rely on unconsumed PIPE buffers that can deadlock process
- **THEN** feedback output channel for Agent remains unpolluted by child-process logs in normal mode

#### Scenario: Forced interruption handling
- **WHEN** CLI process is interrupted or terminated by outer runtime
- **THEN** runtime attempts to stop session backend and child UI process through process-group-aware cleanup strategy
- **THEN** orphan window and port residue risk is minimized
