#!/usr/bin/env python3
"""Discord provider tests against a local fake of the Discord REST API and CDN."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
import pytest_asyncio

from mcp_feedback_enhanced.remote import (
    DiscordSettings,
    RemoteChannelError,
    RemoteOutcome,
    RemoteRequest,
    RemoteSessionCoordinator,
    RemoteState,
    SubmitResult,
)
from mcp_feedback_enhanced.remote.check import CheckStepState, ConnectionCheckRun
from mcp_feedback_enhanced.remote.discord_attachments import (
    AttachmentError,
    AttachmentReader,
    is_text_attachment,
)
from mcp_feedback_enhanced.remote.discord_channel import DiscordChannel
from mcp_feedback_enhanced.remote.discord_check import DiscordConnectionChecker
from mcp_feedback_enhanced.remote.discord_client import (
    DiscordApiClient,
    DiscordApiError,
)
from mcp_feedback_enhanced.remote.discord_format import (
    EMBED_SUMMARY_LIMIT,
    HINT_DOWNLOAD_FAILED,
    HINT_MESSAGE_CONTENT,
    HINT_NO_TEXT,
    HINT_NOT_UTF8,
    HINT_TOO_LARGE,
    RECEIPT,
    RECEIPT_PARTIAL,
    build_title,
)
from tests.helpers.fake_discord import (
    ALLOWED_USER,
    BOT_ID,
    BOT_TOKEN,
    FORUM_ID,
    OTHER_USER,
    TEXT_CHANNEL_ID,
    VOICE_CHANNEL_ID,
    FakeDiscord,
)


class Sleeps:
    """Records requested sleeps but waits at most a few milliseconds for real."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(min(seconds, 0.005))


@pytest_asyncio.fixture
async def fake():
    server = FakeDiscord()
    await server.start()
    yield server
    await server.stop()


def settings(**overrides: Any) -> DiscordSettings:
    values: dict[str, Any] = {
        "token": BOT_TOKEN,
        "channel_id": FORUM_ID,
        "allowed_user_ids": (ALLOWED_USER,),
    }
    values.update(overrides)
    return DiscordSettings(**values)


def local_reader() -> AttachmentReader:
    """Reader that trusts the fake CDN on 127.0.0.1 (production hosts stay fixed)."""
    return AttachmentReader(allowed_hosts=frozenset({"127.0.0.1"}), require_https=False)


def make_request(
    summary: str = "Please review the change", **overrides: Any
) -> RemoteRequest:
    values: dict[str, Any] = {
        "session_id": "abcdef1234567890",
        "project_directory": "D:\\work\\my-project",
        "summary": summary,
        "deadline_epoch": time.time() + 600,
        "timeout_seconds": 600,
    }
    values.update(overrides)
    return RemoteRequest(**values)


def make_channel(fake: FakeDiscord, sleeps: Sleeps | None = None, **kwargs: Any):
    sleeps = sleeps or Sleeps()
    created: list[DiscordApiClient] = []

    def factory(token: str) -> DiscordApiClient:
        client = DiscordApiClient(token, base_url=fake.base_url, sleep=sleeps)
        created.append(client)
        return client

    channel = DiscordChannel(
        kwargs.pop("settings", settings()),
        client_factory=factory,
        reader=local_reader(),
        sleep=sleeps,
        poll_interval=0.01,
        **kwargs,
    )
    channel.created_clients = created  # type: ignore[attr-defined]
    channel.sleeps = sleeps  # type: ignore[attr-defined]
    return channel


class StateLog:
    def __init__(self) -> None:
        self.events: list[tuple[RemoteState, str | None]] = []

    def __call__(self, state: RemoteState, reason: str | None) -> None:
        self.events.append((state, reason))


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


async def open_channel(fake: FakeDiscord, **kwargs: Any):
    channel = make_channel(fake, **kwargs)
    handle = await channel.open(make_request())
    return channel, handle


def bot_texts(fake: FakeDiscord, thread_id: str) -> list[str]:
    """Contents of the messages the provider posted after the first one."""
    return [m["content"] for m in fake.bot_messages(thread_id)[1:]]


# ---------------------------------------------------------------- post creation


@pytest.mark.asyncio
async def test_open_creates_one_forum_post_with_the_expected_content(fake):
    channel, handle = await open_channel(fake)

    assert len(fake.threads) == 1
    thread = fake.threads[handle.conversation_id]
    assert thread["name"] == "[my-project] Please review the change (#abcdef12)"
    assert thread["auto_archive_duration"] == 1440
    request = fake.calls("POST", r"/threads$")[0]
    assert request.headers["Authorization"] == f"Bot {BOT_TOKEN}"

    message = request.json["message"]
    assert f"<@{ALLOWED_USER}>" in message["content"]
    assert message["allowed_mentions"] == {"parse": [], "users": [ALLOWED_USER]}
    embed = message["embeds"][0]
    assert "Please review the change" in embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "D:\\work\\my-project" in fields["项目 / Project"]
    assert "abcdef12" in fields["会话 / Session"]
    assert "<t:" in fields["截止 / Deadline"]
    assert "first usable message" in fields["回复方式 / How to reply"]
    assert handle.state["first_message_id"] == handle.conversation_id
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


def test_title_is_shortened_to_100_characters_and_keeps_the_session_suffix():
    request = make_request(
        summary="# " + "very long heading " * 30,
        project_directory="/home/user/" + "p" * 200,
    )
    title = build_title(request)
    assert len(title) <= 100
    assert title.endswith(" (#abcdef12)")
    assert title.startswith("[p")


def test_title_uses_the_first_non_empty_summary_line():
    title = build_title(make_request(summary="\n\n## Heading line\nsecond"))
    assert title == "[my-project] Heading line (#abcdef12)"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("🔧 **Connection test**\n\nbody", "🔧 Connection test"),
        ("**Done** the `parser` change", "Done the parser change"),
        ("`code` first", "code first"),
        ("> - *note*", "note"),
    ],
)
def test_title_drops_markdown_that_thread_names_do_not_render(summary, expected):
    title = build_title(make_request(summary=summary))
    assert title == f"[my-project] {expected} (#abcdef12)"


@pytest.mark.asyncio
async def test_long_summary_is_truncated_in_the_embed_and_attached_in_full(fake):
    # A unique tail marker proves the cut without relying on a periodic filler pattern.
    summary = "x" * 5000 + "TAIL-MARKER"
    channel = make_channel(fake)
    handle = await channel.open(make_request(summary=summary))

    request = fake.calls("POST", r"/threads$")[0]
    embed = request.json["message"]["embeds"][0]
    assert embed["description"].startswith(summary[:EMBED_SUMMARY_LIMIT])
    assert "TAIL-MARKER" not in embed["description"]
    assert "summary.md" in embed["description"]
    assert len(embed["description"]) <= 4096
    assert request.json["message"]["attachments"] == [
        {"id": 0, "filename": "summary.md"}
    ]
    assert request.files == [("summary.md", summary.encode("utf-8"))]
    await channel.close(handle, RemoteOutcome.ERROR)


@pytest.mark.asyncio
async def test_mentions_inside_the_summary_do_not_notify_anybody(fake):
    channel = make_channel(fake)
    await channel.open(
        make_request(summary="@everyone <@&123456789012345678> please look")
    )

    message = fake.calls("POST", r"/threads$")[0].json["message"]
    assert message["allowed_mentions"]["parse"] == []
    assert message["allowed_mentions"]["users"] == [ALLOWED_USER]


@pytest.mark.asyncio
async def test_forum_that_requires_a_tag_is_retried_once_with_its_first_tag(fake):
    fake.require_tag = True
    fake.available_tags = [{"id": "600000000000000001", "name": "feedback"}]
    _channel, handle = await open_channel(fake)

    posts = fake.calls("POST", r"/threads$")
    assert len(posts) == 2
    assert posts[1].json["applied_tags"] == ["600000000000000001"]
    assert handle.conversation_id in fake.threads


@pytest.mark.asyncio
async def test_open_failure_releases_the_http_client(fake):
    fake.inject(
        "POST",
        r"/threads$",
        403,
        body={"message": "Missing Permissions", "code": 50013},
    )
    channel = make_channel(fake)

    with pytest.raises(RemoteChannelError) as caught:
        await channel.open(make_request())

    assert caught.value.permanent is True
    assert caught.value.reason == "missing_permission"
    assert all(client._session is None for client in channel.created_clients)


# ------------------------------------------------- text channel (one thread each)


def text_settings() -> DiscordSettings:
    return settings(channel_id=TEXT_CHANNEL_ID)


@pytest.mark.asyncio
async def test_open_in_a_text_channel_starts_a_public_thread_with_the_card_inside(fake):
    channel, handle = await open_channel(fake, settings=text_settings())

    thread_id = handle.conversation_id
    assert list(fake.threads) == [thread_id]
    thread = fake.threads[thread_id]
    assert thread["name"] == "[my-project] Please review the change (#abcdef12)"
    assert thread["auto_archive_duration"] == 1440

    # The thread is created without a message; the card is posted inside it afterwards.
    create = fake.calls("POST", r"/threads$")[0]
    assert create.path == f"/channels/{TEXT_CHANNEL_ID}/threads"
    assert create.json == {
        "name": thread["name"],
        "auto_archive_duration": 1440,
        "type": 11,
    }
    card = fake.calls("POST", r"/messages$")[0]
    assert card.path == f"/channels/{thread_id}/messages"
    assert f"<@{ALLOWED_USER}>" in card.json["content"]
    assert card.json["allowed_mentions"] == {"parse": [], "users": [ALLOWED_USER]}
    assert "Please review the change" in card.json["embeds"][0]["description"]

    # Replies are read after the card, which is not the thread itself.
    assert handle.state["first_message_id"] == thread["messages"][0]["id"]
    assert handle.state["first_message_id"] != thread_id
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_text_channel_thread_reads_replies_after_the_card_and_closes_like_a_post(
    fake,
):
    channel, handle = await open_channel(fake, settings=text_settings())
    thread_id = handle.conversation_id
    card_id = handle.state["first_message_id"]

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(thread_id, "from my phone")
    reply = await asyncio.wait_for(task, 5)

    assert reply.text == "from my phone"
    assert bot_texts(fake, thread_id) == [RECEIPT]
    assert fake.calls("GET", r"/messages$")[0].query["after"] == card_id

    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)
    card = fake.threads[thread_id]["messages"][0]
    assert "Answered here" in _status_of(card["embeds"][0])
    assert fake.threads[thread_id]["archived"] is True


@pytest.mark.asyncio
async def test_text_channel_long_summary_file_travels_with_the_card(fake):
    summary = "x" * 5000 + "TAIL-MARKER"
    channel = make_channel(fake, settings=text_settings())
    handle = await channel.open(make_request(summary=summary))

    # The file belongs to the card message, not to the thread creation request.
    assert fake.calls("POST", r"/threads$")[0].files == []
    card = fake.calls("POST", r"/messages$")[0]
    assert card.json["attachments"] == [{"id": 0, "filename": "summary.md"}]
    assert card.files == [("summary.md", summary.encode("utf-8"))]
    await channel.close(handle, RemoteOutcome.ERROR)


@pytest.mark.asyncio
async def test_unpostable_card_fails_the_open_and_archives_the_empty_thread(fake):
    fake.inject(
        "POST",
        r"/messages$",
        403,
        body={"message": "Missing Permissions", "code": 50013},
    )
    channel = make_channel(fake, settings=text_settings())

    with pytest.raises(RemoteChannelError) as caught:
        await channel.open(make_request())

    assert caught.value.reason == "missing_permission"
    (thread,) = fake.threads.values()
    assert thread["messages"] == []
    assert thread["archived"] is True  # Archived, so no active empty thread is left
    assert all(client._session is None for client in channel.created_clients)


@pytest.mark.asyncio
async def test_open_rejects_a_channel_that_can_not_host_conversations(fake):
    channel = make_channel(fake, settings=settings(channel_id=VOICE_CHANNEL_ID))

    with pytest.raises(RemoteChannelError) as caught:
        await channel.open(make_request())

    assert caught.value.permanent is True
    assert caught.value.reason == "unsupported_channel"
    assert fake.calls("POST", r"/threads$") == []
    assert all(client._session is None for client in channel.created_clients)


@pytest.mark.asyncio
async def test_open_reports_a_missing_channel_before_creating_anything(fake):
    channel = make_channel(fake, settings=settings(channel_id="123456789012345678"))

    with pytest.raises(RemoteChannelError) as caught:
        await channel.open(make_request())

    assert caught.value.reason == "not_found"
    assert fake.calls("POST", r"/threads$") == []


# ----------------------------------------------------------------- reply rules


@pytest.mark.asyncio
async def test_first_allowlisted_message_is_the_reply_and_gets_a_receipt(fake):
    channel, handle = await open_channel(fake)
    states = StateLog()

    task = asyncio.create_task(channel.wait_reply(handle, states))
    fake.add_user_message(handle.conversation_id, "  Looks good, ship it  ")
    reply = await asyncio.wait_for(task, 5)

    assert reply.text == "Looks good, ship it"
    assert reply.author_id == ALLOWED_USER
    assert bot_texts(fake, handle.conversation_id) == [RECEIPT]
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_other_authors_bots_webhooks_and_system_messages_are_ignored(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(thread_id, "stranger", author_id=OTHER_USER)
    fake.add_user_message(thread_id, "allowed bot", bot=True)
    fake.add_user_message(thread_id, "webhook", webhook_id="123456789012345678")
    fake.add_user_message(thread_id, "someone joined", message_type=7)
    fake.add_user_message(thread_id, "thread renamed", message_type=4)
    await asyncio.sleep(0.1)
    assert not task.done()
    assert bot_texts(fake, thread_id) == []  # Ignored messages get no hint either

    fake.add_user_message(thread_id, "the real reply")
    reply = await asyncio.wait_for(task, 5)
    assert reply.text == "the real reply"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_reply_type_messages_are_accepted(fake):
    channel, handle = await open_channel(fake)
    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(handle.conversation_id, "as a reply", message_type=19)
    assert (await asyncio.wait_for(task, 5)).text == "as a reply"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_polling_uses_a_cursor_and_never_reprocesses_messages(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    image_only = fake.add_user_message(
        thread_id,
        "",
        attachments=[fake.attachment_meta("pic.png", b"png", content_type="image/png")],
    )
    await wait_until(lambda: len(bot_texts(fake, thread_id)) == 1)
    await asyncio.sleep(0.15)  # Many more polls happen here
    assert bot_texts(fake, thread_id) == [HINT_NO_TEXT]  # One hint, not one per poll

    polls = fake.calls("GET", r"/messages$")
    assert polls[0].query["after"] == thread_id  # Starts from the first message
    assert any(p.query["after"] == image_only["id"] for p in polls)  # Cursor advanced
    assert all(p.query["limit"] == "100" for p in polls)
    assert not task.done()

    fake.add_user_message(thread_id, "finally text")
    assert (await asyncio.wait_for(task, 5)).text == "finally text"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_page_order_does_not_matter_the_oldest_usable_message_wins(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id
    # Both arrive between two polls; Discord lists newest first.
    fake.add_user_message(thread_id, "first answer")
    fake.add_user_message(thread_id, "second answer")

    reply = await asyncio.wait_for(channel.wait_reply(handle, StateLog()), 5)
    assert reply.text == "first answer"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_message_without_usable_text_gets_a_hint_and_the_wait_continues(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(
        thread_id,
        "   ",
        attachments=[fake.attachment_meta("pic.png", b"png", content_type="image/png")],
    )
    await wait_until(lambda: bot_texts(fake, thread_id) == [HINT_NO_TEXT])
    assert not task.done()

    fake.add_user_message(thread_id, "now text")
    assert (await asyncio.wait_for(task, 5)).text == "now text"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_blank_message_points_at_the_message_content_setting(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(thread_id, "")  # What Discord returns without Message Content
    await wait_until(lambda: bot_texts(fake, thread_id) == [HINT_MESSAGE_CONTENT])
    assert not task.done()

    task.cancel()
    await channel.close(handle, RemoteOutcome.INTERRUPTED)


@pytest.mark.asyncio
async def test_text_with_a_non_text_attachment_submits_text_and_notes_the_ignored_file(
    fake,
):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(
        thread_id,
        "see picture",
        attachments=[fake.attachment_meta("pic.png", b"png", content_type="image/png")],
    )
    reply = await asyncio.wait_for(task, 5)

    assert reply.text == "see picture"
    assert reply.ignored_attachments == 1
    assert bot_texts(fake, thread_id) == [RECEIPT_PARTIAL]
    assert fake.cdn_requests == []  # The image was never downloaded
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


# ------------------------------------------------------------- text attachments


async def reply_with_attachments(
    fake: FakeDiscord, content: str, attachments: list[dict]
):
    channel, handle = await open_channel(fake)
    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(handle.conversation_id, content, attachments=attachments)
    return channel, handle, task


@pytest.mark.asyncio
async def test_long_reply_sent_as_message_txt_is_read(fake):
    long_text = "line of a long reply\n" * 300
    meta = fake.attachment_meta("message.txt", long_text.encode("utf-8"))
    channel, handle, task = await reply_with_attachments(fake, "", [meta])

    reply = await asyncio.wait_for(task, 5)
    assert reply.text == long_text
    assert bot_texts(fake, handle.conversation_id) == [RECEIPT]
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_text_followed_by_attachment_text_is_joined_with_a_blank_line(fake):
    first = fake.attachment_meta("a.txt", b"first file")
    second = fake.attachment_meta("b.md", b"second file", content_type=None)
    channel, handle, task = await reply_with_attachments(fake, "intro", [first, second])

    reply = await asyncio.wait_for(task, 5)
    assert reply.text == "intro\n\nfirst file\n\nsecond file"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_utf8_bom_is_allowed_and_dropped(fake):
    meta = fake.attachment_meta(
        "message.txt", b"\xef\xbb\xbfhello \xe4\xbd\xa0\xe5\xa5\xbd"
    )
    channel, handle, task = await reply_with_attachments(fake, "", [meta])

    assert (await asyncio.wait_for(task, 5)).text == "hello 你好"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_bot_token_never_reaches_the_cdn(fake):
    meta = fake.attachment_meta("message.txt", b"text")
    channel, handle, task = await reply_with_attachments(fake, "", [meta])
    await asyncio.wait_for(task, 5)

    assert fake.cdn_requests
    for request in fake.cdn_requests:
        assert "Authorization" not in request.headers
        assert BOT_TOKEN not in str(request.headers)
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


UNREADABLE = [
    pytest.param(
        {"data": b"x" * 10, "declared_size": 300 * 1024},
        HINT_TOO_LARGE,
        False,
        id="declared-size-too-large-is-not-downloaded",
    ),
    pytest.param(
        {"data": b"x" * (300 * 1024), "declared_size": 10},
        HINT_TOO_LARGE,
        True,
        id="actual-bytes-too-large-despite-small-declared-size",
    ),
    pytest.param({"data": b"\xff\xfe\xfa invalid"}, HINT_NOT_UTF8, True, id="not-utf8"),
    pytest.param({"data": b"text\x00more"}, HINT_NOT_UTF8, True, id="nul-bytes"),
    pytest.param(
        {"data": b"x", "status": 500}, HINT_DOWNLOAD_FAILED, True, id="cdn-500"
    ),
    pytest.param(
        {"data": b"x", "status": 401}, HINT_DOWNLOAD_FAILED, True, id="cdn-401"
    ),
    pytest.param(
        {"data": b"x", "status": 403}, HINT_DOWNLOAD_FAILED, True, id="cdn-403"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("attachment", "hint", "downloads"), UNREADABLE)
async def test_unreadable_attachment_gets_a_hint_and_the_wait_continues(
    fake, attachment, hint, downloads
):
    data = attachment["data"]
    meta = fake.attachment_meta(
        "message.txt",
        data,
        declared_size=attachment.get("declared_size"),
        status=attachment.get("status", 200),
    )
    channel, handle, task = await reply_with_attachments(fake, "", [meta])
    thread_id = handle.conversation_id

    await wait_until(lambda: hint in bot_texts(fake, thread_id))
    assert bool(fake.cdn_requests) == downloads
    assert not task.done()  # Still waiting: a CDN failure is not a credential failure

    fake.add_user_message(thread_id, "resent as text")
    assert (await asyncio.wait_for(task, 5)).text == "resent as text"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_attachment_with_unexpected_host_is_not_downloaded(fake):
    meta = fake.attachment_meta(
        "message.txt", b"x", url="https://evil.example.com/message.txt"
    )
    channel, handle, task = await reply_with_attachments(fake, "", [meta])

    await wait_until(lambda: len(bot_texts(fake, handle.conversation_id)) == 1)
    assert fake.cdn_requests == []
    assert not task.done()
    task.cancel()
    await channel.close(handle, RemoteOutcome.INTERRUPTED)


@pytest.mark.asyncio
async def test_redirect_to_another_host_is_not_followed(fake):
    meta = fake.attachment_meta(
        "message.txt", b"x", redirect_to="https://evil.example.com/x.txt"
    )
    channel, handle, task = await reply_with_attachments(fake, "", [meta])

    await wait_until(lambda: len(bot_texts(fake, handle.conversation_id)) == 1)
    assert len(fake.cdn_requests) == 1  # Only the original request, nothing after it
    assert not task.done()
    task.cancel()
    await channel.close(handle, RemoteOutcome.INTERRUPTED)


@pytest.mark.asyncio
async def test_redirect_within_the_allowed_host_is_followed(fake):
    fake.attachment_meta("real.txt", b"redirected content")
    meta = fake.attachment_meta(
        "message.txt", b"", redirect_to=fake.cdn_url("real.txt")
    )
    channel, handle, task = await reply_with_attachments(fake, "", [meta])

    assert (await asyncio.wait_for(task, 5)).text == "redirected content"
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_total_attachment_size_is_limited_across_files(fake):
    chunk = b"y" * (200 * 1024)
    metas = [fake.attachment_meta("a.txt", chunk), fake.attachment_meta("b.txt", chunk)]
    channel, handle, task = await reply_with_attachments(fake, "text", metas)

    await wait_until(lambda: HINT_TOO_LARGE in bot_texts(fake, handle.conversation_id))
    assert fake.cdn_requests == []  # Declared sizes already exceed the limit
    task.cancel()
    await channel.close(handle, RemoteOutcome.INTERRUPTED)


@pytest.mark.parametrize(
    ("attachment", "expected"),
    [
        ({"content_type": "text/plain", "filename": "x.bin"}, True),
        ({"content_type": "text/markdown; charset=utf-8", "filename": "x"}, True),
        ({"filename": "notes.TXT"}, True),
        ({"filename": "notes.md"}, True),
        ({"content_type": "image/png", "filename": "a.png"}, False),
        ({"content_type": "application/json", "filename": "a.json"}, False),
        ({}, False),
    ],
)
def test_text_attachment_detection(attachment, expected):
    assert is_text_attachment(attachment) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/1/2/a.txt",
        "https://cdn.discordapp.com.evil.com/attachments/1/2/a.txt",
        "https://evil.com/cdn.discordapp.com/a.txt",
        "https://user:pass@cdn.discordapp.com/a.txt",
        "https://cdn.discordapp.com:8443/a.txt",
        "ftp://cdn.discordapp.com/a.txt",
        "",
    ],
)
async def test_production_reader_only_accepts_https_on_the_exact_cdn_hosts(url):
    reader = AttachmentReader()
    with pytest.raises(AttachmentError) as caught:
        await reader.read([{"filename": "a.txt", "size": 1, "url": url}])
    assert caught.value.reason == "bad_host"


# ------------------------------------------------------------ errors and limits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"), [(401, "auth_failed"), (403, "missing_permission")]
)
async def test_rejected_credentials_stop_polling_with_a_permanent_failure(
    fake, status, reason
):
    channel, handle = await open_channel(fake)
    fake.inject(
        "GET", r"/messages$", status, body={"message": "nope", "code": 0}, times=50
    )

    with pytest.raises(RemoteChannelError) as caught:
        await asyncio.wait_for(channel.wait_reply(handle, StateLog()), 5)

    assert caught.value.permanent is True
    assert caught.value.reason == reason
    polls = len(fake.calls("GET", r"/messages$"))
    await asyncio.sleep(0.1)
    assert len(fake.calls("GET", r"/messages$")) == polls  # No further requests
    await channel.close(handle, RemoteOutcome.ERROR)


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_after_and_continues(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id
    fake.inject(
        "GET",
        r"/messages$",
        429,
        headers={"Retry-After": "2.5"},
        body={"message": "rate limited", "retry_after": 2.5, "global": False},
    )
    fake.add_user_message(thread_id, "answer")

    reply = await asyncio.wait_for(channel.wait_reply(handle, StateLog()), 5)

    assert reply.text == "answer"
    assert 2.5 in channel.sleeps.calls
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_exhausted_bucket_header_delays_the_next_request(fake):
    sleeps = Sleeps()
    client = DiscordApiClient(BOT_TOKEN, base_url=fake.base_url, sleep=sleeps)
    fake.inject(
        "GET",
        r"/users/@me$",
        200,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"},
        body={"id": BOT_ID},
    )

    await client.request("GET", "/users/@me")
    assert sleeps.calls == []
    await client.request("GET", "/users/@me")

    assert sleeps.calls and 0 < sleeps.calls[0] <= 1.5
    await client.aclose()


@pytest.mark.asyncio
async def test_persistent_rate_limiting_becomes_a_transient_failure(fake):
    client = DiscordApiClient(BOT_TOKEN, base_url=fake.base_url, sleep=Sleeps())
    fake.inject(
        "GET", r"/users/@me$", 429, headers={"Retry-After": "0.01"}, body={}, times=10
    )

    with pytest.raises(DiscordApiError) as caught:
        await client.request("GET", "/users/@me")

    assert caught.value.permanent is False
    assert caught.value.reason == "rate_limited"
    await client.aclose()


@pytest.mark.asyncio
async def test_huge_retry_after_is_not_waited_out_silently(fake):
    sleeps = Sleeps()
    client = DiscordApiClient(BOT_TOKEN, base_url=fake.base_url, sleep=sleeps)
    fake.inject("GET", r"/users/@me$", 429, headers={"Retry-After": "3600"}, body={})

    with pytest.raises(DiscordApiError) as caught:
        await client.request("GET", "/users/@me")

    assert caught.value.reason == "rate_limited"
    assert sleeps.calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_server_errors_are_reported_then_recovered_with_exponential_backoff(fake):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id
    states = StateLog()
    fake.inject("GET", r"/messages$", 500, body={"message": "boom"}, times=3)
    fake.add_user_message(thread_id, "answer")

    reply = await asyncio.wait_for(channel.wait_reply(handle, states), 10)

    assert reply.text == "answer"
    assert states.events[0] == (RemoteState.UNAVAILABLE, "server_error")
    assert states.events[-1] == (RemoteState.WAITING, None)
    backoffs = [s for s in channel.sleeps.calls if s >= 1.0]
    assert backoffs[:3] == [1.0, 2.0, 4.0]
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_backoff_is_capped_at_thirty_seconds(fake):
    channel, handle = await open_channel(fake)
    fake.inject("GET", r"/messages$", 500, body={}, times=9)
    fake.add_user_message(handle.conversation_id, "answer")

    await asyncio.wait_for(channel.wait_reply(handle, StateLog()), 10)

    backoffs = [s for s in channel.sleeps.calls if s >= 1.0]
    assert max(backoffs) == 30.0
    assert backoffs[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
async def test_network_errors_are_transient_and_timeouts_follow_the_spec():
    client = DiscordApiClient(BOT_TOKEN, base_url="http://127.0.0.1:9", sleep=Sleeps())

    with pytest.raises(DiscordApiError) as caught:
        await client.request("GET", "/users/@me")

    assert caught.value.permanent is False
    assert caught.value.reason == "network_error"
    session = client._get_session()
    assert session.timeout.connect == 10
    assert session.timeout.total == 30
    assert session.trust_env is True
    await client.aclose()


@pytest.mark.asyncio
async def test_token_never_appears_in_errors_or_representations(fake):
    fake.inject(
        "GET",
        r"/users/@me$",
        403,
        body={"message": f"leaked {BOT_TOKEN} in message", "code": 50013},
    )
    client = DiscordApiClient(BOT_TOKEN, base_url=fake.base_url, sleep=Sleeps())

    with pytest.raises(DiscordApiError) as caught:
        await client.request("GET", "/users/@me")

    assert BOT_TOKEN not in str(caught.value)
    assert BOT_TOKEN not in repr(caught.value)
    assert BOT_TOKEN not in repr(client)
    assert BOT_TOKEN not in repr(settings())
    await client.aclose()


@pytest.mark.asyncio
async def test_only_rest_endpoints_are_used_no_gateway(fake):
    channel, handle = await open_channel(fake)
    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
    fake.add_user_message(handle.conversation_id, "answer")
    await asyncio.wait_for(task, 5)
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)

    for request in fake.requests:
        assert request.path.startswith("/channels/")
        assert "gateway" not in request.path


# ------------------------------------------------------------- liveness & close


@pytest.mark.asyncio
async def test_first_message_is_refreshed_about_once_per_minute(fake):
    clock = {"now": 1_000_000.0}
    channel, handle = await open_channel(fake, clock=lambda: clock["now"])
    thread_id = handle.conversation_id
    task = asyncio.create_task(channel.wait_reply(handle, StateLog()))

    await asyncio.sleep(0.1)
    assert fake.calls("PATCH", rf"/messages/{thread_id}$") == []  # Not yet a minute

    clock["now"] += 61
    await wait_until(lambda: len(fake.calls("PATCH", rf"/messages/{thread_id}$")) == 1)
    status = _status_of(
        fake.calls("PATCH", rf"/messages/{thread_id}$")[0].json["embeds"][0]
    )
    assert f"<t:{int(clock['now'])}:R>" in status

    await asyncio.sleep(0.1)
    assert len(fake.calls("PATCH", rf"/messages/{thread_id}$")) == 1  # At most once

    task.cancel()
    await channel.close(handle, RemoteOutcome.INTERRUPTED)


def _status_of(embed: dict[str, Any]) -> str:
    return next(f["value"] for f in embed["fields"] if f["name"].startswith("状态"))


@pytest.mark.asyncio
async def test_liveness_edit_failures_are_ignored(fake):
    clock = {"now": 1_000_000.0}
    channel, handle = await open_channel(fake, clock=lambda: clock["now"])
    thread_id = handle.conversation_id
    states = StateLog()
    fake.inject("PATCH", r"/messages/\d+$", 500, body={"message": "no"}, times=5)
    task = asyncio.create_task(channel.wait_reply(handle, states))

    clock["now"] += 61
    await wait_until(lambda: len(fake.calls("PATCH", rf"/messages/{thread_id}$")) >= 1)
    fake.add_user_message(thread_id, "still works")
    reply = await asyncio.wait_for(task, 5)

    assert reply.text == "still works"
    assert states.events == []  # An edit failure is not a status change
    await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status_text", "note"),
    [
        (RemoteOutcome.REMOTE_ANSWERED, "Answered here", False),
        (RemoteOutcome.LOCAL_ANSWERED, "Answered locally", True),
        (RemoteOutcome.TIMEOUT, "Timed out", True),
        (RemoteOutcome.INTERRUPTED, "Interrupted", True),
        (RemoteOutcome.ERROR, "error", True),
    ],
)
async def test_close_marks_the_outcome_explains_when_needed_and_archives(
    fake, outcome, status_text, note
):
    channel, handle = await open_channel(fake)
    thread_id = handle.conversation_id

    await channel.close(handle, outcome)

    first = fake.threads[thread_id]["messages"][0]
    assert status_text in _status_of(first["embeds"][0])
    assert len(bot_texts(fake, thread_id)) == (1 if note else 0)
    assert fake.threads[thread_id]["archived"] is True
    # Everything is written before the post is archived.
    order = [(r.method, r.path) for r in fake.requests if thread_id in r.path]
    assert order[-1] == ("PATCH", f"/channels/{thread_id}")
    assert all(r.json.get("archived") is None for r in fake.requests[:-1] if r.json)
    assert all(client._session is None for client in channel.created_clients)


@pytest.mark.asyncio
async def test_close_never_raises_even_when_everything_fails(fake):
    channel, handle = await open_channel(fake)
    fake.inject("PATCH", r".*", 500, body={}, times=10)
    fake.inject("POST", r"/messages$", 500, body={}, times=10)

    await channel.close(handle, RemoteOutcome.LOCAL_ANSWERED)  # Must not raise
    await channel.close(handle, RemoteOutcome.LOCAL_ANSWERED)  # Idempotent


# ------------------------------------------------------------ full session flow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_id", [FORUM_ID, TEXT_CHANNEL_ID], ids=["forum", "text-channel"]
)
async def test_full_session_through_the_coordinator(fake, channel_id):
    channel = make_channel(fake, settings=settings(channel_id=channel_id))
    submitted: list[tuple[str, str]] = []
    statuses: list[str] = []

    async def submit(reply, source):
        submitted.append((reply.text, source))
        return SubmitResult(True)

    coordinator = RemoteSessionCoordinator(
        channel,
        make_request(),
        submit=submit,
        report_status=lambda status: statuses.append(status.state.value),
    )
    coordinator.start()
    await wait_until(lambda: fake.last_thread_id() is not None)
    thread_id = fake.last_thread_id()
    await wait_until(lambda: "waiting" in statuses)
    fake.add_user_message(thread_id, "from my phone")
    await wait_until(lambda: submitted)
    await coordinator.finalize(RemoteOutcome.REMOTE_ANSWERED)

    assert submitted == [("from my phone", "remote:discord")]
    assert statuses[:2] == ["connecting", "waiting"]
    assert fake.threads[thread_id]["archived"] is True


@pytest.mark.asyncio
async def test_three_concurrent_sessions_use_their_own_posts(fake):
    results: dict[str, str] = {}

    async def run_one(label: str):
        channel = make_channel(fake)
        handle = await channel.open(
            make_request(summary=f"task {label}", session_id=f"{label}" * 8)
        )
        task = asyncio.create_task(channel.wait_reply(handle, StateLog()))
        return label, channel, handle, task

    sessions = [await run_one(label) for label in ("a", "b", "c")]
    assert len(fake.threads) == 3
    for label, _channel, handle, _task in sessions:
        fake.add_user_message(handle.conversation_id, f"reply for {label}")
    for label, channel, handle, task in sessions:
        results[label] = (await asyncio.wait_for(task, 5)).text
        await channel.close(handle, RemoteOutcome.REMOTE_ANSWERED)

    assert results == {"a": "reply for a", "b": "reply for b", "c": "reply for c"}


# ---------------------------------------------------------------------- checker


def make_checker(fake: FakeDiscord, **kwargs: Any) -> DiscordConnectionChecker:
    sleeps = Sleeps()
    return DiscordConnectionChecker(
        kwargs.pop("settings", settings()),
        client_factory=lambda token: DiscordApiClient(
            token, base_url=fake.base_url, sleep=sleeps
        ),
        reader=local_reader(),
        reply_timeout=kwargs.pop("reply_timeout", 5.0),
        poll_interval=0.01,
        sleep=sleeps,
        **kwargs,
    )


def new_run(checker: DiscordConnectionChecker) -> ConnectionCheckRun:
    return ConnectionCheckRun(
        checker.provider, checker.step_ids, checker.optional_steps
    )


def states_of(check: ConnectionCheckRun) -> dict[str, str]:
    return {step.id: step.state.value for step in check.steps}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_id", [FORUM_ID, TEXT_CHANNEL_ID], ids=["forum", "text-channel"]
)
async def test_check_passes_when_an_allowlisted_user_replies(fake, channel_id):
    checker = make_checker(fake, settings=settings(channel_id=channel_id))
    check = new_run(checker)

    task = asyncio.create_task(checker.run(check))
    await wait_until(lambda: fake.last_thread_id() is not None)
    await wait_until(lambda: check.reply_timeout_seconds is not None)
    thread_id = fake.last_thread_id()
    fake.add_user_message(thread_id, "hello from the test")
    await asyncio.wait_for(task, 5)

    assert states_of(check) == {
        "token": "ok",
        "channel": "ok",
        "post": "ok",
        "reply": "ok",
        "archive": "ok",
    }
    assert check.passed is True
    assert check.reply_timeout_seconds == 5
    assert fake.threads[thread_id]["archived"] is True
    assert BOT_TOKEN not in str(check.to_payload())


@pytest.mark.asyncio
async def test_check_stops_at_the_token_step_for_a_bad_token(fake):
    checker = make_checker(
        fake,
        settings=settings(token="not-the-right-token-value"),  # noqa: S106
    )
    check = new_run(checker)

    await checker.run(check)

    assert states_of(check) == {
        "token": "failed",
        "channel": "skipped",
        "post": "skipped",
        "reply": "skipped",
        "archive": "skipped",
    }
    assert check.steps[0].reason == "auth_failed"
    assert check.passed is False
    assert fake.threads == {}
    assert len(fake.calls("GET", r"/users/@me$")) == 1  # A permanent failure is final


@pytest.mark.asyncio
async def test_check_fails_when_the_channel_can_not_host_conversations(fake):
    checker = make_checker(fake, settings=settings(channel_id=VOICE_CHANNEL_ID))
    check = new_run(checker)

    await checker.run(check)

    assert check.steps[1].state == CheckStepState.FAILED
    assert check.steps[1].reason == "unsupported_channel"
    assert fake.threads == {}


@pytest.mark.asyncio
async def test_check_reports_a_missing_channel(fake):
    checker = make_checker(fake, settings=settings(channel_id="123456789012345678"))
    check = new_run(checker)

    await checker.run(check)

    assert check.steps[1].reason == "not_found"


@pytest.mark.asyncio
async def test_check_reports_missing_permission_to_create_posts(fake):
    fake.inject(
        "POST",
        r"/threads$",
        403,
        body={"message": "Missing Permissions", "code": 50013},
    )
    checker = make_checker(fake)
    check = new_run(checker)

    await checker.run(check)

    assert check.steps[2].state == CheckStepState.FAILED
    assert check.steps[2].reason == "missing_permission"


@pytest.mark.asyncio
async def test_check_reports_missing_permission_to_post_inside_a_text_thread(fake):
    fake.inject(
        "POST",
        r"/messages$",
        403,
        body={"message": "Missing Permissions", "code": 50013},
    )
    checker = make_checker(fake, settings=text_settings())
    check = new_run(checker)

    await checker.run(check)

    assert check.steps[2].state == CheckStepState.FAILED
    assert check.steps[2].reason == "missing_permission"
    assert len(fake.calls("POST", r"/threads$")) == 1  # A permanent failure is final
    (thread,) = fake.threads.values()
    assert thread["archived"] is True  # The empty thread was not left active


@pytest.mark.asyncio
async def test_check_repeats_a_step_after_transient_failures(fake):
    fake.inject("GET", r"/users/@me$", 500, body={}, times=2)
    checker = make_checker(fake, reply_timeout=0.2)
    check = new_run(checker)

    await asyncio.wait_for(checker.run(check), 5)

    assert check.steps[0].state == CheckStepState.OK
    assert len(fake.calls("GET", r"/users/@me$")) == 3
    assert check.steps[2].state == CheckStepState.OK  # The check went on afterwards


@pytest.mark.asyncio
async def test_check_gives_up_on_a_step_after_three_failed_attempts(fake):
    fake.inject("GET", r"/users/@me$", 500, body={}, times=5)
    checker = make_checker(fake)
    check = new_run(checker)

    await checker.run(check)

    assert check.steps[0].state == CheckStepState.FAILED
    assert check.steps[0].reason == "server_error"
    assert len(fake.calls("GET", r"/users/@me$")) == 3
    assert states_of(check)["channel"] == "skipped"


@pytest.mark.asyncio
async def test_check_retries_the_post_step_and_archives_the_abandoned_thread(fake):
    fake.inject("POST", r"/messages$", 500, body={}, times=1)
    checker = make_checker(fake, settings=text_settings(), reply_timeout=0.2)
    check = new_run(checker)

    await asyncio.wait_for(checker.run(check), 5)

    assert check.steps[2].state == CheckStepState.OK
    abandoned, retried = fake.threads.values()
    assert abandoned["messages"] == []
    assert abandoned["archived"] is True
    assert retried["messages"]  # The second attempt holds the card


@pytest.mark.asyncio
async def test_check_points_at_message_content_when_the_reply_text_is_blank(fake):
    checker = make_checker(fake)
    check = new_run(checker)

    task = asyncio.create_task(checker.run(check))
    await wait_until(lambda: fake.last_thread_id() is not None)
    thread_id = fake.last_thread_id()
    fake.add_user_message(thread_id, "")
    await asyncio.wait_for(task, 5)

    assert check.steps[3].state == CheckStepState.FAILED
    assert check.steps[3].reason == "message_content"
    assert check.passed is False
    assert fake.threads[thread_id]["archived"] is True  # Cleaned up after the failure
    assert HINT_MESSAGE_CONTENT in bot_texts(fake, thread_id)


@pytest.mark.asyncio
async def test_check_times_out_without_a_reply_and_archives_the_test_post(fake):
    checker = make_checker(fake, reply_timeout=0.3)
    check = new_run(checker)

    await asyncio.wait_for(checker.run(check), 5)

    assert check.steps[3].state == CheckStepState.FAILED
    assert check.steps[3].reason == "timeout"
    assert check.passed is False
    thread_id = fake.last_thread_id()
    assert fake.threads[thread_id]["archived"] is True


@pytest.mark.asyncio
async def test_check_ignores_unauthorized_replies(fake):
    checker = make_checker(fake, reply_timeout=0.4)
    check = new_run(checker)

    task = asyncio.create_task(checker.run(check))
    await wait_until(lambda: fake.last_thread_id() is not None)
    fake.add_user_message(
        fake.last_thread_id(), "I am not allowed", author_id=OTHER_USER
    )
    await asyncio.wait_for(task, 5)

    assert check.steps[3].reason == "timeout"


@pytest.mark.asyncio
async def test_check_archive_failure_does_not_fail_the_check(fake):
    fake.inject(
        "PATCH",
        r"/channels/\d+$",
        403,
        body={"message": "Missing Permissions", "code": 50013},
        times=5,
    )
    checker = make_checker(fake)
    check = new_run(checker)

    task = asyncio.create_task(checker.run(check))
    await wait_until(lambda: fake.last_thread_id() is not None)
    fake.add_user_message(fake.last_thread_id(), "reply")
    await asyncio.wait_for(task, 5)

    assert check.steps[4].state == CheckStepState.FAILED
    assert check.passed is True


@pytest.mark.asyncio
async def test_cancelled_check_still_archives_the_test_post(fake):
    checker = make_checker(fake)
    check = new_run(checker)

    task = asyncio.create_task(checker.run(check))
    await wait_until(lambda: check.reply_timeout_seconds is not None)
    thread_id = fake.last_thread_id()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.threads[thread_id]["archived"] is True
