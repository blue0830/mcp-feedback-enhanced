#!/usr/bin/env python3
"""Message content for the Discord conversation of one feedback session.

Responsibilities:
- build the forum post title and the first message (embed, mentions, summary file);
- provide the status lines, receipts, hints and closing notes the bot posts.

Limitations:
- pure functions with no I/O, so the exact payloads can be unit tested;
- bot texts are short bilingual lines (Simplified Chinese / English) because the post is
  read on a phone and the user's UI language is not known to the provider;
- Discord limits are respected here (title 100, embed description 4096, content 2000,
  100 mentioned users), nothing is validated again by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import RemoteOutcome, RemoteRequest


TITLE_LIMIT = 100
EMBED_SUMMARY_LIMIT = 4000
AUTO_ARCHIVE_MINUTES = 1440
MAX_MENTIONED_USERS = 100
SUMMARY_FILE_NAME = "summary.md"
EMBED_COLOR = 0x007ACC
_FIELD_VALUE_LIMIT = 1000

TRUNCATION_NOTE = "\n\n…（已截断，完整摘要见附件 / truncated, full text in summary.md）"

RECEIPT = "✅ 已收到，正在交给 Agent。 / Received, handing it to the agent."
RECEIPT_PARTIAL = (
    "✅ 已收到，部分附件已忽略（只读取文字）。 / "
    "Received. Some attachments were ignored (only text is used)."
)

HINT_NO_TEXT = (
    "⚠️ 没有读到可用文字。请发送文字消息，或 .txt / .md 文字附件。 / "
    "No usable text. Send a text message or a .txt / .md attachment."
)
HINT_MESSAGE_CONTENT = (
    "⚠️ 消息正文是空的。如果你确实发了文字，请在 Discord 开发者后台为机器人开启 "
    "Message Content。 / The message text is empty. If you did send text, enable "
    "Message Content for the bot in the Discord Developer Portal."
)
HINT_TOO_LARGE = (
    "⚠️ 附件超过 256 KiB，未读取，请缩短后重发。 / "
    "The attachment is larger than 256 KiB and was not read. Please shorten it and resend."
)
HINT_NOT_UTF8 = (
    "⚠️ 附件不是有效的 UTF-8 文字，请以 UTF-8 文本重发。 / "
    "The attachment is not valid UTF-8 text. Please resend it as UTF-8 text."
)
HINT_DOWNLOAD_FAILED = (
    "⚠️ 附件读取失败，请重新发送。 / Could not read the attachment. Please resend."
)
HINT_BAD_HOST = "⚠️ 附件来源不受信任，未读取。 / The attachment source is not trusted and was not read."

# Reason code (AttachmentError.reason / inspection reason) -> hint text.
HINTS_BY_REASON = {
    "no_text": HINT_NO_TEXT,
    "message_content": HINT_MESSAGE_CONTENT,
    "too_large": HINT_TOO_LARGE,
    "not_utf8": HINT_NOT_UTF8,
    "download_failed": HINT_DOWNLOAD_FAILED,
    "bad_host": HINT_BAD_HOST,
}

HOW_TO_REPLY = (
    "在此帖内发送一条消息即为最终回复（白名单用户的第一条有效消息生效）。长文字可作为 .txt 文件发送。\n"
    "Send one message in this post: the first usable message from an allowed user is the "
    "final reply. Long text may be sent as a .txt file."
)

STATUS_FIELD_NAME = "状态 / Status"

_OUTCOME_STATUS = {
    RemoteOutcome.REMOTE_ANSWERED: "✅ 已通过此帖回复 / Answered here",
    RemoteOutcome.LOCAL_ANSWERED: "💻 已在本机窗口回复 / Answered locally",
    RemoteOutcome.TIMEOUT: "⏱️ 已超时 / Timed out",
    RemoteOutcome.INTERRUPTED: "⚠️ 已中断 / Interrupted",
    RemoteOutcome.ERROR: "❌ 出错结束 / Ended with an error",
}

# Explanation posted when the session did not end with a reply from this post.
_OUTCOME_NOTE = {
    RemoteOutcome.LOCAL_ANSWERED: (
        "💻 这个请求已在本机窗口中回复，此帖不再监听。 / "
        "This request was answered in the local window; this post is no longer monitored."
    ),
    RemoteOutcome.TIMEOUT: (
        "⏱️ 这个请求已超时，此帖不再监听。 / "
        "This request timed out; this post is no longer monitored."
    ),
    RemoteOutcome.INTERRUPTED: (
        "⚠️ 这个请求已被中断，此帖不再监听。 / "
        "This request was interrupted; this post is no longer monitored."
    ),
    RemoteOutcome.ERROR: (
        "❌ 这个请求因错误结束，此帖不再监听。 / "
        "This request ended with an error; this post is no longer monitored."
    ),
}


# Texts of the settings "test connection" post.
TEST_SUMMARY = (
    "🔧 **连接测试 / Connection test**\n\n"
    "请用白名单账号在此帖内回复任意一句话，以完成验证。\n"
    "Reply with any sentence in this post from an allowed account to finish the check."
)
TEST_RECEIPT = "✅ 测试通过，已收到你的回复。 / Test passed, your reply was received."
TEST_RECEIPT_PARTIAL = (
    "✅ 测试通过，已收到你的回复（部分附件已忽略）。 / "
    "Test passed, your reply was received (some attachments were ignored)."
)
TEST_FINISHED_STATUS = "🔧 测试已结束 / Test finished"
TEST_FINISHED_NOTE = "🔧 连接测试已结束，此帖将被归档。 / The connection test is over; this post will be archived."


def outcome_note(outcome: RemoteOutcome) -> str | None:
    """Closing explanation for outcomes other than a remote reply (None for that one)."""
    return _OUTCOME_NOTE.get(outcome)


def outcome_status(outcome: RemoteOutcome) -> str:
    """Status line shown in the first message once the session is over."""
    return _OUTCOME_STATUS[outcome]


def waiting_status(last_alive_epoch: float) -> str:
    """Status line while waiting, carrying the liveness timestamp."""
    return (
        f"⏳ 等待回复 / Waiting for reply · 最后存活 / last alive "
        f"<t:{int(last_alive_epoch)}:R>"
    )


def _project_name(project_directory: str) -> str:
    """Last path component of a Windows or POSIX path."""
    parts = [part for part in re.split(r"[\\/]", project_directory.strip()) if part]
    return parts[-1] if parts else "project"


def _first_line(summary: str) -> str:
    """First non-empty line without Markdown decoration, whitespace collapsed."""
    for line in summary.splitlines():
        cleaned = re.sub(r"\s+", " ", line.strip().lstrip("#>*-` ").rstrip("`* "))
        if cleaned:
            return cleaned
    return ""


def build_title(request: RemoteRequest) -> str:
    """``[<project>] <first summary line> (#<short id>)`` within 100 characters.

    The short id suffix is kept intact when the middle part has to be shortened, so the
    post can always be matched to its session.
    """
    suffix = f" (#{request.short_id})"
    head = f"[{_project_name(request.project_directory)}] {_first_line(request.summary) or '(no summary)'}"
    available = TITLE_LIMIT - len(suffix)
    if len(head) > available:
        head = head[: available - 1].rstrip() + "…"
    return head + suffix


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_embed(request: RemoteRequest, status: str) -> dict[str, Any]:
    """The embed of the first message: summary, project, session, deadline, status."""
    summary = request.summary
    description = summary[:EMBED_SUMMARY_LIMIT]
    if len(summary) > EMBED_SUMMARY_LIMIT:
        description += TRUNCATION_NOTE
    deadline = int(request.deadline_epoch)
    return {
        "description": description or "(no summary)",
        "color": EMBED_COLOR,
        "fields": [
            {
                "name": "项目 / Project",
                "value": _clip(f"`{request.project_directory}`", _FIELD_VALUE_LIMIT),
                "inline": False,
            },
            {
                "name": "会话 / Session",
                "value": f"`{request.short_id}`",
                "inline": True,
            },
            {
                "name": "截止 / Deadline",
                "value": f"<t:{deadline}:F> (<t:{deadline}:R>)",
                "inline": True,
            },
            {"name": STATUS_FIELD_NAME, "value": status, "inline": False},
            {"name": "回复方式 / How to reply", "value": HOW_TO_REPLY, "inline": False},
        ],
    }


def with_status(embed: dict[str, Any], status: str) -> dict[str, Any]:
    """Copy of ``embed`` whose status field shows ``status``."""
    updated = dict(embed)
    updated["fields"] = [
        {**field, "value": status} if field.get("name") == STATUS_FIELD_NAME else field
        for field in embed.get("fields", [])
    ]
    return updated


@dataclass(frozen=True)
class PostPayload:
    """Everything needed to create the forum post."""

    body: dict[str, Any]
    files: tuple[tuple[str, bytes, str], ...]
    embed: dict[str, Any]


def build_post(
    request: RemoteRequest, allowed_user_ids: tuple[str, ...], status: str
) -> PostPayload:
    """Forum post creation body: title, first message, mentions and optional summary file.

    Mentions are restricted to the allowlisted users, so ``@everyone`` or role mentions
    inside the summary notify nobody.
    """
    users = list(allowed_user_ids)[:MAX_MENTIONED_USERS]
    mentions = " ".join(f"<@{user_id}>" for user_id in users)
    content = (
        f"{mentions} 新的反馈请求 / New feedback request #{request.short_id}".strip()
    )

    embed = build_embed(request, status)
    message: dict[str, Any] = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"parse": [], "users": users},
    }
    files: tuple[tuple[str, bytes, str], ...] = ()
    if len(request.summary) > EMBED_SUMMARY_LIMIT:
        message["attachments"] = [{"id": 0, "filename": SUMMARY_FILE_NAME}]
        files = ((SUMMARY_FILE_NAME, request.summary.encode("utf-8"), "text/markdown"),)

    body = {
        "name": build_title(request),
        "auto_archive_duration": AUTO_ARCHIVE_MINUTES,
        "message": message,
    }
    return PostPayload(body=body, files=files, embed=embed)
