---
name: feedback-cli
description: Runs blocking interactive feedback sessions through feedback-cli. Use when the user asks to route updates/replies through feedback-cli, requests interactive confirmation, or requires a unified feedback-cli-only workflow.
disable-model-invocation: true
---

# Feedback CLI Skill

## Purpose

Use `feedback-cli` as the only interactive feedback channel.

## Global Command Setup

`feedback-cli` is exposed by the package's `project.scripts` entry point. Prefer
calling the installed command directly so it works from any project directory:

```text
feedback-cli --project-directory "." --summary "Implemented X, ready for confirmation."
```

For a local checkout, install it as a user-level global tool once:

```powershell
cd D:\Sources\Github\mcp-feedback-enhanced
uv tool install --force .
uv tool update-shell
```

If `uv tool install` cannot replace an executable that is currently in use, use
the Python user installation instead:

```powershell
py -3.12 -m pip install --user .
```

For a published package, use `uv tool install mcp-feedback-enhanced` or
`py -3.12 -m pip install --user mcp-feedback-enhanced` instead.
After changing PATH, open a new `cmd` or PowerShell window. Verify the command
with `where feedback-cli` and `feedback-cli --help` before using it.

If the command is not found, check `uv tool dir --bin` (for a `uv` installation)
or the Python user `Scripts` directory (for `pip install --user`) and ensure the
directory is on PATH. Do not silently replace the global setup with a project
virtual-environment command.

## Required Invocation Rules

These are absolute invariants for every `feedback-cli` invocation. They apply
at all times and under every circumstance, including retries, timeouts,
failures, tests, scripts, and alternate shells:

1. Run `feedback-cli` in a normal foreground terminal and wait synchronously
   for it to finish or return feedback.
2. Never invoke it in the background or detached from the current terminal.
   This includes shell background operators, job APIs, `Start-Job`,
   `Start-Process`, `-AsJob`, daemon/service wrappers, `nohup`, async task
   wrappers, and any equivalent mechanism.
3. Never use a workaround that starts the command asynchronously and polls,
   tails, or reconnects to it later.
4. If a foreground terminal is unavailable, stop and ask the user for a
   usable foreground terminal; do not substitute a background invocation.
5. Wait for CLI output, read user feedback, then continue work.
6. If user feedback is non-empty, incorporate it before the next action.

## Command Parameters

- `--project-directory <path>`
  - Project context path for the feedback session.
  - Default: current directory.
- `--summary <text>`
  - Inline summary of what was done or what needs confirmation.
- `--summary-file <file>`
  - UTF-8 file containing summary text.
  - Mutually exclusive with `--summary`.
- `--timeout <seconds>`
  - Optional explicit timeout.
  - Current CLI default timeout is 3600 seconds.
- `--remote-settings`
  - Opens the remote communication settings window for a human. It creates no
    feedback session and forwards nothing.
  - Human-only: never use it in an Agent call.
  - Mutually exclusive with `--summary` and `--summary-file`.

## Parameter Rules

1. Never pass both `--summary` and `--summary-file`.
2. Prefer `--summary` for short updates.
3. Use `--summary-file` for long structured content.
4. Do not pass `--timeout` unless the user explicitly asks for a custom timeout.
5. Never pass `--remote-settings` from an Agent; it is a human-only entry point.

## Standard Usage

```text
feedback-cli --project-directory "." --summary "Implemented X, ready for confirmation."
```

## Long Summary Usage

```text
feedback-cli --project-directory "." --summary-file "tmp/feedback-summary.md"
```

Use `uv run feedback-cli ...` only when intentionally testing the checkout's
editable project environment; it is not the global invocation path.

## Failure Handling

- If `feedback-cli` times out, treat it as no feedback yet; continue per user instruction or retry when requested.
- If invocation fails due to non-foreground/non-interactive context, stop and request a normal foreground terminal session. Never rerun it through a background, detached, asynchronous, or polling workaround.
- Keep outputs concise and focus on actionable user feedback.

## Remote Communication

The user may mirror feedback requests to a private Discord forum post (setup:
`docs/en/remote-channel.md`). This does not change how you call `feedback-cli`:

- Use the same foreground, blocking invocation with the same parameters. There
  is no flag to turn the remote channel on or off for a call, and none is needed.
- A reply from the remote channel ends the command exactly like a local
  submission (same output format, same exit code). Treat it as ordinary user
  feedback.
- It does not extend any timeout. The outer tool timeout and the CLI timeout
  still apply, and the no-retry and no-background rules stay in force.
- `feedback-cli --remote-settings` is for humans only. If the user wants to
  change the remote settings, ask them to run it in their own terminal.

## Cautions

- `feedback-cli` is strictly blocking by design in this project. Foreground-only invocation is a hard requirement, never an optimization or preference.
- The command may open desktop UI first and fallback to browser.
- Always read and act on returned feedback text before sending the next major update.
- Never use delayed wakeups or background timers (for example `Start-Sleep`) to resume interaction.
