## ADDED Requirements

### Requirement: Remote settings card
The remote communication card MUST contain an enable toggle, a provider selector (Discord), a write-only token field, a channel id field (a text channel or a forum channel), an allowlisted user id list, a test button, a verification status, and a notice that summaries and replies pass through the chosen third-party service. The same card component MUST be used in the settings tab of `feedback-cli` windows and on the standalone settings page. All texts MUST be localized for zh-TW, zh-CN and en.

#### Scenario: Card displayed
- **WHEN** the user opens the settings tab of a `feedback-cli` window or the standalone settings page
- **THEN** the card shows the current enabled state, the verification status, and whether a token is set without revealing it
- **THEN** the privacy notice is visible

### Requirement: Card availability limited to CLI-hosted windows
The remote communication card and all remote configuration endpoints MUST be available only when the web server is hosted by `feedback-cli`. Windows served by the MCP server path MUST NOT render the card, and the endpoints MUST answer 404 there.

#### Scenario: MCP-mode window
- **WHEN** a feedback window is served by the MCP server path
- **THEN** its settings tab does not contain the remote communication card
- **THEN** requests to the remote configuration endpoints receive 404

#### Scenario: CLI-hosted window
- **WHEN** a feedback window is served by `feedback-cli`
- **THEN** its settings tab contains the remote communication card

### Requirement: Standalone remote settings mode
`feedback-cli --remote-settings` MUST open a settings page that contains only the remote communication card, without creating a feedback session, without starting remote forwarding and without consuming an Agent request. The flag MUST be mutually exclusive with `--summary` and `--summary-file`, and `--timeout` MUST bound how long the mode stays alive.

#### Scenario: Open standalone settings
- **WHEN** the user runs `feedback-cli --remote-settings` in a foreground terminal
- **THEN** a window (desktop first, browser as fallback) shows the remote communication card
- **THEN** no feedback session exists and nothing is forwarded to the remote provider

#### Scenario: Desktop window closed
- **WHEN** the desktop window is closed
- **THEN** the process stops its server and exits with code 0

#### Scenario: Browser fallback
- **WHEN** the desktop window cannot be started and the browser is used
- **THEN** the process keeps running until Ctrl+C or until `--timeout` elapses, then stops its server and exits with code 0

#### Scenario: Conflicting flags
- **WHEN** `--remote-settings` is combined with `--summary` or `--summary-file`
- **THEN** the command fails with a usage error and does not start a server

#### Scenario: Language
- **WHEN** the standalone page is displayed
- **THEN** it uses the language stored in the general settings without writing anything back to them

### Requirement: Local same-origin access only
The remote configuration read, save and test endpoints MUST reject requests whose `Host` header hostname is not a loopback name (`127.0.0.1`, `localhost`, `[::1]`), write requests whose `Content-Type` is not `application/json`, and requests whose `Origin` header does not match the `Host`. A rejected request MUST receive 403 and MUST NOT change any configuration.

#### Scenario: Cross-site form post
- **WHEN** a web page from another origin submits a `text/plain` POST to the save endpoint
- **THEN** the request is rejected with 403 and the configuration is unchanged

#### Scenario: DNS rebinding
- **WHEN** a request arrives with a `Host` header naming a non-loopback host
- **THEN** the request is rejected with 403

#### Scenario: Forwarded loopback port
- **WHEN** the window is opened through an SSH-forwarded `localhost` port
- **THEN** the requests are accepted

### Requirement: Independent configuration storage
Remote configuration MUST be stored in a dedicated file `remote_channel.json` under the application config directory, written atomically, and MUST NOT be stored in or round-tripped through `ui_settings.json` or the general frontend settings object.

#### Scenario: Stale window saves general settings
- **WHEN** a window that was opened before remote configuration was saved later saves any general setting
- **THEN** the remote configuration is unchanged

#### Scenario: Atomic write
- **WHEN** the configuration is saved
- **THEN** the file is replaced atomically so a concurrent reader sees either the old or the new content

### Requirement: Token protection
The bot token MUST be write-only: no API response, UI element, log line or error message SHALL contain it. When a save request omits the token, the stored token MUST be kept.

#### Scenario: Read configuration
- **WHEN** the frontend requests the remote configuration
- **THEN** the response reports `token_set` as a boolean and never the token value

#### Scenario: Save without token
- **WHEN** the user saves other fields without entering a token
- **THEN** the previously stored token is kept

### Requirement: Mandatory allowlist
The configuration MUST contain at least one allowlisted user id to be verified or enabled.

#### Scenario: Empty allowlist
- **WHEN** the allowlist is empty
- **THEN** the test and the enable toggle are rejected with a clear reason

### Requirement: Verification gating by fingerprint
The enable toggle MUST take effect only when the stored verification fingerprint equals the fingerprint computed from the current configuration (provider, token, channel id, sorted allowlist). Changing any of these fields MUST invalidate verification and disable the feature until re-verified. The fingerprint MUST be a one-way digest and MUST NOT expose the token.

#### Scenario: Enable before verification
- **WHEN** the user tries to enable remote communication without a matching verification
- **THEN** the toggle is rejected and the UI asks the user to run the test first

#### Scenario: Configuration edited after verification
- **WHEN** the user changes the token, channel id or allowlist after a successful test
- **THEN** verification is invalidated and the effective state becomes disabled until a new test passes

### Requirement: End-to-end test flow
The test action MUST verify, in order and with per-step results: token validity, channel existence and type (text or forum), ability to start a thread or post in it, and that an allowlisted user can reply with readable text within 120 seconds. Only when all steps pass MUST the verification fingerprint be stored. The test thread MUST be archived afterwards. A step whose request failed transiently (network error, server error, rate limit) MUST be attempted up to three times before it is reported as failed; permanent failures MUST NOT be retried.

#### Scenario: All steps pass
- **WHEN** the user runs the test and replies in the test thread within 120 seconds
- **THEN** every step is reported as passed and the verification fingerprint is stored
- **THEN** the test thread is archived

#### Scenario: Unsupported channel type
- **WHEN** the configured channel is neither a text channel nor a forum channel
- **THEN** the channel step fails with the reason `unsupported_channel`, later steps are skipped and no verification fingerprint is stored

#### Scenario: Transient failure on the first attempt
- **WHEN** the first request of a step fails with a network error and the next attempt succeeds
- **THEN** the step is reported as passed and the check continues

#### Scenario: Permanent failure
- **WHEN** a request is rejected because the token is invalid or the bot lacks a permission
- **THEN** the step fails right away without further attempts

#### Scenario: Reply text unreadable
- **WHEN** the allowlisted user replies but the message text is empty because message content access is disabled
- **THEN** the step fails with a reason pointing to the Message Content setting
- **THEN** no verification fingerprint is stored

#### Scenario: Test timeout
- **WHEN** no allowlisted reply arrives within 120 seconds
- **THEN** the test fails with a timeout reason and no verification fingerprint is stored

### Requirement: Configuration read at each invocation
Each `feedback-cli` invocation MUST read the latest remote configuration at startup and MUST NOT cache it across invocations.

#### Scenario: Toggle changed between invocations
- **WHEN** the user disables remote communication in one window
- **THEN** the next `feedback-cli` invocation runs local-only

#### Scenario: Change during an in-flight invocation
- **WHEN** the user changes or disables remote communication while an invocation is already waiting
- **THEN** that invocation keeps the configuration it read at startup and is not affected
