## 1. Prerequisites (shared with other changes)

- [x] 1.1 Land the first-success submission arbitration from `harden-agent-cli-feedback-control` tasks 1.1–1.4 (atomic claim-and-submit shared by web, remote and timeout paths); do not implement its registry or HTTP contract parts here.
- [x] 1.2 Make a session close the desktop window through its owning manager (or an injected close callback) instead of the global `get_web_ui_manager()`; add a regression test proving no default manager is created in CLI mode.
- [x] 1.3 Provide a thread-safe way to submit from the CLI main loop into the web server event loop (`run_coroutine_threadsafe`) and verify the local window still receives the submitted notification.

## 2. Remote channel core

- [x] 2.1 Create the `remote/` package with the `RemoteChannel` protocol (open / wait_reply / close), the outcome enum, and request / handle / reply data types.
- [x] 2.2 Implement `RemoteSessionCoordinator`: background task, exception isolation, status reporting, submission through the arbitration with source `remote:<provider>`, finalization with outcome.
- [x] 2.3 Implement the effectiveness check (enabled + fingerprint match + readable config) so every other case is a strict no-op with zero network requests.
- [x] 2.4 Integrate into `CliSessionRuntime`: read config per invocation, start the coordinator after UI launch without awaiting the network, record the end reason, run bounded finalization in `shutdown()`.
- [x] 2.5 Push remote status over WebSocket and show a status badge in the local window (off / connecting / waiting / unavailable with reason / answered remotely / answered locally).

## 3. Configuration and settings UI

- [x] 3.1 Implement the config store for `remote_channel.json`: atomic write, POSIX 0600 best effort, redacted read, keep stored token when a save omits it.
- [x] 3.2 Implement fingerprint computation (one-way digest of provider, token, channel id, sorted allowlist) and the verification gating helper.
- [x] 3.3 Add endpoints: read (redacted), save, test (per-step results).
- [x] 3.4 Enforce server-side: allowlist required, enable rejected without a matching verification, changes invalidate verification.
- [x] 3.5 Add a "remote settings available" flag to `WebUIManager` (default false, set by the CLI runtimes, read at request time); when false, do not render the card and answer the remote configuration endpoints with 404; the card script must not initialize when the card element is absent.
- [x] 3.6 Add the same-origin guard for the remote configuration endpoints: loopback `Host` hostname, JSON content type for write requests, matching `Origin`; 403 otherwise and no configuration change.
- [x] 3.7 Add the settings card as one shared component (toggle, provider, write-only token, channel id, allowlist, test button, verification status, privacy notice), included in the settings tab of `feedback-cli` windows and in the standalone page.
- [x] 3.8 Add zh-TW / zh-CN / en strings and message codes; run `scripts/validate_message_codes.py`.

## 4. Standalone settings mode

- [x] 4.1 Add `--remote-settings` to the parser, mutually exclusive with `--summary` and `--summary-file`; treat Ctrl+C and `--timeout` expiry as a normal exit (code 0) in this mode.
- [x] 4.2 Extract the desktop-first launch (desktop app with browser fallback) into a helper shared by `CliSessionRuntime` and the new runtime.
- [x] 4.3 Implement `RemoteSettingsRuntime`: manager flagged as CLI-hosted, `start_server()` without any session, open `/remote-settings`, wait until the desktop process exits, Ctrl+C or `--timeout`, then bounded shutdown; never start the coordinator.
- [x] 4.4 Add the `/remote-settings` route and a minimal template that loads only the shared styles, `i18n.js` (language read from `ui_settings.json`, never written back) and the card module, with no session or WebSocket dependency.

## 5. Discord provider

- [x] 5.1 Build the HTTP client on `aiohttp`: 10 s connect / 30 s total timeouts (see 5.10), proxy from environment, rate-limit headers and `Retry-After`, error classification for Discord API requests, token redaction in logs and errors.
- [x] 5.2 Create the conversation thread: title format, embed, mention allowlisted users with restricted `allowed_mentions`, `auto_archive_duration=1440`, `summary.md` attachment for summaries over 4000 characters.
- [x] 5.3 Implement reply polling with cursor and jitter, authorized reply rules, receipt and hint messages, and usable-text assembly (message text followed by text attachments).
- [x] 5.4 Implement text attachment reading: media type / extension filter, https-only CDN host allowlist without cross-host redirects, no token on the download, 256 KiB cap (declared size and actual bytes), strict UTF-8 without NUL bytes; map every failure to a hint and keep waiting, and never treat a CDN 401 / 403 as a credential failure.
- [x] 5.5 Implement the deadline text and the last-alive refresh (edit the first message about once per minute, ignore failures).
- [x] 5.6 Implement close: outcome edit, explanation message for non-remote outcomes, archive, all within a bounded time.
- [x] 5.7 Implement the test flow: token, channel type, create test thread, allowlisted reply within 120 s, archive the test thread.
- [x] 5.8 Support ordinary text channels next to forum channels (the setting is `channel_id`): look the channel up first; a forum gets one post as before, a text channel gets one public thread created without a message plus the first message posted inside it (archive the empty thread when that message fails); any other channel type fails permanently with `unsupported_channel`.
- [x] 5.9 Repeat a connection-test step up to three times, one second apart, when its request failed transiently; never repeat permanent failures.
- [x] 5.10 Tolerate slow proxies: on the owner's machine about 4 of 10 requests to Discord stall for about 10 s and then succeed, so the per-request timeouts are 10 s connect / 30 s total (the earlier 5 s / 10 s turned those into failures; attachment downloads follow), and closing a session that ended with a remote answer may take up to 20 s (a local answer keeps the 5 s bound because the user is waiting for the Agent).

## 6. Tests and validation

- [x] 6.1 Coordinator tests with a fake provider: remote first, local first, simultaneous, timeout, interruption, unavailable at start, transient and permanent failure.
- [x] 6.2 Concurrency test: three concurrent runtimes with a fake provider keep replies isolated.
- [x] 6.3 Discord adapter tests with mocked HTTP: cursor handling, allowlist, bot and system messages, empty text, image-only messages, text attachments (`message.txt` only, text plus attachment, oversize, non-UTF-8 or NUL bytes, wrong host, download failure, CDN 401 / 403), 401 / 403 / 429 / 5xx on API requests, mention restriction, long summary attachment.
- [x] 6.4 Config tests: atomic write, redaction, token retention, fingerprint invalidation, a stale window saving general settings leaves remote config unchanged, and a change during an in-flight invocation does not affect it.
- [x] 6.5 Visibility and guard tests: an MCP-mode manager renders no card and answers 404; the same-origin guard rejects cross-site `text/plain` posts, non-loopback hosts and mismatching origins, and accepts `localhost` through a forwarded port.
- [x] 6.6 Standalone mode tests with a fake desktop and manager: flag exclusivity, no session created, no coordinator started, exit when the desktop process exits, exit on timeout, Ctrl+C exits with code 0.
- [x] 6.7 Verify no behavior change when disabled: existing CLI tests pass and no network request is issued.
- [x] 6.9 Text-channel and retry tests: thread creation without a message and the card posted inside it, replies read after the card, summary file attached to the card, empty thread archived when the card fails, `unsupported_channel` for a voice channel, the full session and the connection test in both channel kinds, step repeats after transient failures, no repeat after permanent failures.
- [x] 6.10 Timeout tests: the client uses 10 s connect / 30 s total; a remote answer gets the longer close bound, a local answer the short one, and both stay bounded when the provider close hangs.
- [x] 6.8 Manual acceptance on a real private Discord server: phone push, remote reply ends the CLI, long reply sent as `message.txt`, local-first path, archive, killed process leaves a recognizably stale thread, and a full test flow started from `feedback-cli --remote-settings`. **Status:** covered by automated tests against a local fake Discord (REST and CDN), by real desktop-window runs and by browser checks. On the owner's real private text channel these ran and passed: the full connection test (with a reply from the owner), a session answered from Discord (card with mention, receipt, status "Answered here", archive, the CLI ended with that reply) and a session answered in the local window (status "Answered locally", closing note, archive). Not run live, automated tests only: a long reply sent as `message.txt`, a killed process leaving a stale thread, and the standalone `--remote-settings` window. The owner accepted it on this basis.

## 7. Documentation

- [x] 7.1 Add a setup guide (create app and bot, Message Content switch, permissions, private text channel or forum channel with its Community-server prerequisite, how to get ids, `feedback-cli --remote-settings` usage) the network requirement (outbound HTTPS to `discord.com` and `cdn.discordapp.com` only; no public IP, no inbound port, proxy or Tailscale exit node handled at the system level) and the limits (outer tool timeout, machine sleep or shutdown, text replies with the 256 KiB attachment cap, loopback-only access for configuration).
- [x] 7.2 State in the `feedback-cli` skill notes that remote communication does not change the invocation rules (foreground, blocking, no retry) and that `--remote-settings` is for humans, not for Agent calls.
