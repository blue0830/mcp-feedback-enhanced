---
name: feedback-cli
description: Runs blocking interactive feedback sessions through feedback-cli. Use when the user asks to route updates/replies through feedback-cli, requests interactive confirmation, or requires a unified feedback-cli-only workflow.
disable-model-invocation: true
---

# Feedback CLI Skill

## Purpose

Use `feedback-cli` as the only interactive feedback channel.

## Required Invocation Rules

1. Run `feedback-cli` in foreground blocking mode only.
2. Do not background it, do not use `&`, and do not use detached execution.
3. Wait for CLI output, read user feedback, then continue work.
4. If user feedback is non-empty, incorporate it before next action.

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

## Parameter Rules

1. Never pass both `--summary` and `--summary-file`.
2. Prefer `--summary` for short updates.
3. Use `--summary-file` for long structured content.
4. Do not pass `--timeout` unless the user explicitly asks for a custom timeout.

## Standard Usage

```bash
uv run feedback-cli --project-directory "." --summary "Implemented X, ready for confirmation."
```

## Long Summary Usage

```bash
uv run feedback-cli --project-directory "." --summary-file "tmp/feedback-summary.md"
```

## Failure Handling

- If `feedback-cli` times out, treat it as no feedback yet; continue per user instruction or retry when requested.
- If invocation fails due to non-foreground/non-interactive context, rerun in a normal foreground terminal session.
- Keep outputs concise and focus on actionable user feedback.

## Cautions

- `feedback-cli` is strictly blocking by design in this project.
- The command may open desktop UI first and fallback to browser.
- Always read and act on returned feedback text before sending the next major update.
