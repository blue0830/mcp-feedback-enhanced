#!/usr/bin/env python3
"""Reading the text attachments of a Discord reply.

Responsibilities:
- decide which attachments count as text: media type ``text/*`` or a ``.txt`` / ``.md``
  file name (mainly Discord's automatic ``message.txt`` for overlong messages);
- download them defensively: https only, exactly the Discord CDN hosts, never with the bot
  token, never following a redirect to another host, at most 256 KiB in total, strictly
  valid UTF-8 without NUL bytes;
- map every failure to a short reason code (``AttachmentError.reason``) so the provider
  can tell the user what to fix.

Limitations:
- a failed attachment makes its whole message unusable; nothing is retried here;
- downloads are not Discord API requests: their failures (even a CDN 401/403 for an
  expired link) say nothing about the bot credentials and must never be treated as such;
- ``require_https=False`` exists for local tests only; it is not reachable from the user
  configuration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp


# Hosts that serve attachment links returned by the Discord API.
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
MAX_TOTAL_BYTES = 256 * 1024
# Same tolerance as the Discord API client (see discord_client.py): the CDN is reached
# through the same, possibly slow, proxy, and a failed download makes the reply unusable.
CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK_BYTES = 16 * 1024
_TEXT_SUFFIXES = (".txt", ".md")

REASON_TOO_LARGE = "too_large"
REASON_NOT_UTF8 = "not_utf8"
REASON_BAD_HOST = "bad_host"
REASON_DOWNLOAD_FAILED = "download_failed"


class AttachmentError(Exception):
    """An attachment could not be used; ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AttachmentTexts:
    """Text read from a message's attachments plus the count of ignored ones."""

    texts: tuple[str, ...]
    ignored: int


def is_text_attachment(attachment: Mapping[str, Any]) -> bool:
    """True for ``text/*`` media types and ``.txt`` / ``.md`` file names."""
    content_type = str(attachment.get("content_type") or "").lower()
    filename = str(attachment.get("filename") or "").lower()
    return content_type.startswith("text/") or filename.endswith(_TEXT_SUFFIXES)


def _declared_size(attachment: Mapping[str, Any]) -> int:
    size = attachment.get("size")
    return size if isinstance(size, int) and size > 0 else 0


class AttachmentReader:
    """Download and decode the text attachments of one message."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] = CDN_HOSTS,
        require_https: bool = True,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._require_https = require_https
        self._max_total_bytes = max_total_bytes

    async def read(self, attachments: Sequence[Mapping[str, Any]]) -> AttachmentTexts:
        """Read every text attachment; other attachments are only counted.

        Raises:
            AttachmentError: any text attachment is unusable (size, encoding, host,
            download), which makes the whole message unusable.
        """
        text_attachments = [a for a in attachments if is_text_attachment(a)]
        ignored = len(attachments) - len(text_attachments)
        if not text_attachments:
            return AttachmentTexts((), ignored)

        # Declared sizes are checked before any byte is downloaded.
        if sum(_declared_size(a) for a in text_attachments) > self._max_total_bytes:
            raise AttachmentError(REASON_TOO_LARGE)

        texts: list[str] = []
        budget = self._max_total_bytes
        # Fresh session without credentials: the bot token must never reach the CDN.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            ),
            trust_env=True,
        ) as session:
            for attachment in text_attachments:
                data = await self._download(
                    session, str(attachment.get("url") or ""), budget
                )
                budget -= len(data)
                text = self._decode(data)
                if text.strip():
                    texts.append(text)
        return AttachmentTexts(tuple(texts), ignored)

    def _check_url(self, url: str) -> None:
        """Only https links on the exact CDN hosts, without credentials or odd ports."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise AttachmentError(REASON_BAD_HOST) from None
        hostname = (parts.hostname or "").lower()
        if hostname not in self._allowed_hosts or parts.username or parts.password:
            raise AttachmentError(REASON_BAD_HOST)
        if self._require_https and (parts.scheme != "https" or port not in (None, 443)):
            raise AttachmentError(REASON_BAD_HOST)
        if parts.scheme not in ("http", "https"):
            raise AttachmentError(REASON_BAD_HOST)

    async def _download(
        self, session: aiohttp.ClientSession, url: str, budget: int
    ) -> bytes:
        """Fetch one attachment with manual redirects, bounded by ``budget`` bytes."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            self._check_url(current)
            try:
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise AttachmentError(REASON_DOWNLOAD_FAILED)
                        # Each hop is validated again, so a redirect can never leave
                        # the allowed hosts.
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise AttachmentError(REASON_DOWNLOAD_FAILED)
                    if (
                        response.content_length is not None
                        and response.content_length > budget
                    ):
                        raise AttachmentError(REASON_TOO_LARGE)
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                        data.extend(chunk)
                        if len(data) > budget:  # Actual bytes, whatever was declared
                            raise AttachmentError(REASON_TOO_LARGE)
                    return bytes(data)
            except AttachmentError:
                raise
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError):
                raise AttachmentError(REASON_DOWNLOAD_FAILED) from None
        raise AttachmentError(REASON_DOWNLOAD_FAILED)  # Too many redirects

    @staticmethod
    def _decode(data: bytes) -> str:
        """Strict UTF-8 (an optional BOM is dropped) without NUL bytes."""
        if b"\x00" in data:
            raise AttachmentError(REASON_NOT_UTF8)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AttachmentError(REASON_NOT_UTF8) from None
