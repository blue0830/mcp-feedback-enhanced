#!/usr/bin/env python3
"""Minimal Discord REST client used by the Discord remote provider.

Responsibilities:
- perform authenticated JSON/multipart requests against the Discord HTTP API with a 5 s
  connect and 10 s total timeout, reading proxy settings from the environment;
- honor rate limits (``Retry-After``, ``X-RateLimit-*``) with a bounded wait;
- classify failures for the provider: 401/403/404/other 4xx are permanent, 5xx and network
  errors are transient;
- keep the bot token out of logs, errors and exception messages.

Limitations:
- no Gateway connection: replies are found by polling, which is why this client is all the
  provider needs;
- one client serves one conversation; it is not meant to be shared across sessions;
- retry and backoff policy for transient failures belongs to the caller (the polling
  loop, the coordinator); the client only waits out rate limits itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import aiohttp

from .. import __version__
from ..debug import server_debug_log as debug_log
from .config import redact_secret
from .models import RemoteChannelError


API_BASE = "https://discord.com/api/v10"
CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 10.0

# A single rate-limit wait never exceeds this; a longer one is reported as a transient
# failure so the caller's backoff takes over instead of a silent long stall.
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0
MAX_CONSECUTIVE_RATE_LIMITS = 3

# Discord error code: a forum that requires tags refused a post without one.
ERROR_CODE_TAG_REQUIRED = 40067

# (filename, content, content type) of one uploaded file.
UploadFile = tuple[str, bytes, str]


class DiscordApiError(RemoteChannelError):
    """A failed Discord API request with the HTTP status and Discord error code."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: int | None = None,
        permanent: bool,
        reason: str,
    ) -> None:
        super().__init__(message, permanent=permanent, reason=reason)
        self.status = status
        self.code = code


class DiscordApiClient:
    """Authenticated access to the Discord REST API for one conversation.

    ``base_url`` and ``sleep`` are injectable so tests can run against a local fake
    server without waiting in real time.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._session: aiohttp.ClientSession | None = None
        # Monotonic time before which the next request should wait (exhausted bucket).
        self._blocked_until = 0.0

    def __repr__(self) -> str:
        return f"DiscordApiClient(base_url={self._base_url!r}, token=<hidden>)"

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
                ),
                # Proxy settings (HTTP(S)_PROXY, NO_PROXY, system proxy) come from the
                # environment, matching how the rest of the machine reaches the internet.
                trust_env=True,
                headers={
                    "Authorization": f"Bot {self._token}",
                    "User-Agent": f"DiscordBot (mcp-feedback-enhanced, {__version__})",
                },
            )
        return self._session

    async def aclose(self) -> None:
        """Close the underlying HTTP session (safe to call repeatedly)."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    def _redact(self, text: str) -> str:
        return redact_secret(text, self._token)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: Sequence[UploadFile] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send one API request and return the decoded JSON (None for empty bodies).

        With ``files`` the body is multipart: ``json_body`` travels as ``payload_json``.

        Raises:
            DiscordApiError: permanent rejection (401/403/404/other 4xx) or transient
            failure (429 beyond the retry budget, 5xx, network error, timeout).
        """
        url = f"{self._base_url}{path}"
        rate_limited = 0

        while True:
            await self._wait_for_bucket()
            try:
                session = self._get_session()
                kwargs: dict[str, Any] = {"params": params}
                if files:
                    form = aiohttp.FormData()
                    form.add_field(
                        "payload_json",
                        json.dumps(json_body or {}),
                        content_type="application/json",
                    )
                    for index, (name, content, content_type) in enumerate(files):
                        form.add_field(
                            f"files[{index}]",
                            content,
                            filename=name,
                            content_type=content_type,
                        )
                    kwargs["data"] = form
                elif json_body is not None:
                    kwargs["json"] = json_body
                async with session.request(method, url, **kwargs) as response:
                    status = response.status
                    headers = response.headers
                    raw = await response.read()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError) as network_error:
                # Only the exception class is reported: messages can echo request details.
                raise DiscordApiError(
                    f"Network error while calling Discord: {type(network_error).__name__}",
                    status=0,
                    permanent=False,
                    reason="network_error",
                ) from None

            self._note_bucket(headers)
            payload = self._decode(raw)

            if status == 429:
                rate_limited += 1
                wait = self._retry_after(headers, payload)
                if rate_limited > MAX_CONSECUTIVE_RATE_LIMITS or (
                    wait > MAX_RATE_LIMIT_WAIT_SECONDS
                ):
                    raise DiscordApiError(
                        "Discord is rate limiting this bot",
                        status=429,
                        permanent=False,
                        reason="rate_limited",
                    )
                debug_log(f"Discord 限流，等待 {wait:.1f} 秒後重試")
                await self._sleep(wait)
                continue

            if 200 <= status < 300:
                return payload
            raise self._error_for(status, payload)

    async def _wait_for_bucket(self) -> None:
        """Wait out an exhausted rate-limit bucket announced by the previous response."""
        delay = self._blocked_until - time.monotonic()
        if delay > 0:
            await self._sleep(min(delay, MAX_RATE_LIMIT_WAIT_SECONDS))

    def _note_bucket(self, headers: Any) -> None:
        """Remember when an exhausted bucket resets (``X-RateLimit-Remaining: 0``)."""
        if headers.get("X-RateLimit-Remaining") != "0":
            return
        try:
            reset_after = float(headers.get("X-RateLimit-Reset-After", "0"))
        except ValueError:
            return
        if reset_after > 0:
            self._blocked_until = time.monotonic() + reset_after

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return (
                None  # Non-JSON bodies (proxies, Cloudflare pages) carry nothing useful
            )

    @staticmethod
    def _retry_after(headers: Any, payload: Any) -> float:
        """Seconds to wait after a 429 (header first, then the JSON body)."""
        candidates: list[Any] = [headers.get("Retry-After")]
        if isinstance(payload, dict):
            candidates.append(payload.get("retry_after"))
        for candidate in candidates:
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return 1.0

    def _error_for(self, status: int, payload: Any) -> DiscordApiError:
        """Classify a non-success response."""
        code: int | None = None
        detail = ""
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = raw_code if isinstance(raw_code, int) else None
            message = payload.get("message")
            detail = self._redact(str(message)) if message else ""
        suffix = f": {detail}" if detail else ""

        if status == 401:
            return DiscordApiError(
                "Discord rejected the bot token",
                status=status,
                code=code,
                permanent=True,
                reason="auth_failed",
            )
        if status == 403:
            return DiscordApiError(
                f"The bot lacks access or permission{suffix}",
                status=status,
                code=code,
                permanent=True,
                reason="missing_permission",
            )
        if status == 404:
            return DiscordApiError(
                f"Discord resource not found{suffix}",
                status=status,
                code=code,
                permanent=True,
                reason="not_found",
            )
        if status >= 500:
            return DiscordApiError(
                f"Discord server error ({status})",
                status=status,
                code=code,
                permanent=False,
                reason="server_error",
            )
        if code == ERROR_CODE_TAG_REQUIRED:
            return DiscordApiError(
                f"The forum requires a tag{suffix}",
                status=status,
                code=code,
                permanent=True,
                reason="forum_tag_required",
            )
        return DiscordApiError(
            f"Discord rejected the request ({status}){suffix}",
            status=status,
            code=code,
            permanent=True,
            reason="bad_request",
        )
