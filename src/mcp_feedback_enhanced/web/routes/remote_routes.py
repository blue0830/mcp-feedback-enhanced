#!/usr/bin/env python3
"""Routes for remote communication settings (``feedback-cli`` windows only).

Responsibilities:
- serve the standalone ``/remote-settings`` page and the JSON endpoints behind the shared
  settings card: read (redacted), save, start a connection check, poll a check;
- answer 404 for all of them unless the window is hosted by ``feedback-cli``
  (``WebUIManager.remote_settings_available`` is read on every request);
- reject non-local or cross-site requests before any configuration is read or changed.

Limitations:
- the guard defends against browsers (cross-site posts, DNS rebinding). It does not
  authenticate other local processes, which can already do anything the user can;
- the bot token is write-only: no response here ever contains it;
- configuration writes are last-write-wins across processes (see ``RemoteConfigStore``).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from ... import __version__
from ...debug import web_debug_log as debug_log
from ...remote.check import CheckBusyError, ConnectionCheckService
from ...remote.config import ConfigError, RemoteConfigStore
from ...remote.factory import create_checker
from ...remote.models import RemoteChannelError


if TYPE_CHECKING:
    from ..main import WebUIManager


# Hostnames a browser may use to reach the local server (Host header, port stripped).
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
# A settings payload is a handful of short fields; anything bigger is refused.
MAX_BODY_BYTES = 64 * 1024
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


def _hostname(host_header: str) -> str:
    """Return the lower-cased hostname of a Host header value (port removed)."""
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else ""
    return value.split(":", 1)[0]


def local_request_violation(request: Request, *, write: bool) -> str | None:
    """Return the rule a request violates, or None when it may proceed.

    Rules (all must hold):
    - the ``Host`` hostname is a loopback name, which stops DNS rebinding;
    - write requests carry ``Content-Type: application/json``, which a cross-site form
      post cannot forge and a cross-site fetch cannot send without a preflight;
    - an ``Origin`` header, when present, names exactly the ``Host`` the request used.
      Without it, browsers that label the request cross-site are still refused; clients
      that send neither header (command line tools) are local processes by nature.
    """
    host_header = request.headers.get("host", "")
    if _hostname(host_header) not in LOOPBACK_HOSTNAMES:
        return "host"

    if write:
        media_type = request.headers.get("content-type", "")
        if media_type.split(";", 1)[0].strip().lower() != "application/json":
            return "content_type"

    origin = request.headers.get("origin")
    if origin is not None:
        try:
            parts = urlsplit(origin)
        except ValueError:
            return "origin"
        if (
            parts.scheme not in ("http", "https")
            or parts.netloc.lower() != host_header.strip().lower()
        ):
            return "origin"
    elif write:
        fetch_site = request.headers.get("sec-fetch-site")
        if (
            fetch_site is not None
            and fetch_site.lower() not in _SAME_ORIGIN_FETCH_SITES
        ):
            return "origin"
    return None


def _json_error(status: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, **extra})


class _Rejected(Exception):
    """Carries the error response of a refused request out of a helper."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__("request rejected")
        self.response = response


async def _read_json_object(request: Request) -> dict[str, Any]:
    """Read a bounded JSON object body (an empty body counts as ``{}``).

    Raises:
        _Rejected: the body is too large or not a JSON object.
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise _Rejected(_json_error(413, "payload_too_large"))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise _Rejected(_json_error(400, "invalid_payload")) from None
    if not isinstance(data, dict):
        raise _Rejected(_json_error(400, "invalid_payload"))
    return data


def setup_remote_routes(manager: WebUIManager) -> None:
    """Register the remote settings page and endpoints on ``manager.app``."""
    store = RemoteConfigStore()
    service = ConnectionCheckService(store, create_checker)
    manager.remote_check_service = service

    def reject(request: Request, *, write: bool) -> JSONResponse | None:
        """Availability first (404 for MCP-hosted windows), then the local-origin guard."""
        if not manager.remote_settings_available:
            return _json_error(404, "not_found")
        violation = local_request_violation(request, write=write)
        if violation is not None:
            debug_log(f"拒絕遠端設定請求: {violation}")
            return _json_error(403, "forbidden", reason=violation)
        return None

    @manager.app.get("/remote-settings", response_class=HTMLResponse)
    async def remote_settings_page(request: Request):
        """Standalone page that only hosts the remote settings card."""
        rejected = reject(request, write=False)
        if rejected is not None:
            return rejected
        return manager.templates.TemplateResponse(
            request=request,
            name="remote_settings.html",
            context={"title": "Remote Communication", "version": __version__},
        )

    @manager.app.get("/api/remote-config")
    async def read_remote_config(request: Request):
        """Redacted configuration: the token is reported as ``token_set`` only."""
        rejected = reject(request, write=False)
        if rejected is not None:
            return rejected
        return JSONResponse(content={"config": store.view()})

    @manager.app.post("/api/remote-config")
    async def save_remote_config(request: Request):
        """Apply a user edit; the store enforces the verification and allowlist rules."""
        rejected = reject(request, write=True)
        if rejected is not None:
            return rejected
        try:
            payload = await _read_json_object(request)
            # File I/O with Windows retries: keep it off the event loop.
            config = await asyncio.to_thread(store.update, payload)
        except _Rejected as rejection:
            return rejection.response
        except ConfigError as config_error:
            return _json_error(400, config_error.code)
        return JSONResponse(content={"config": store.view(config)})

    @manager.app.post("/api/remote-config/test")
    async def start_remote_check(request: Request):
        """Start the connection check for the stored settings (poll for progress)."""
        rejected = reject(request, write=True)
        if rejected is not None:
            return rejected
        try:
            await _read_json_object(request)  # Body is unused but must stay well formed
            check = service.start(store.load())
        except _Rejected as rejection:
            return rejection.response
        except ConfigError as config_error:
            return _json_error(400, config_error.code)
        except CheckBusyError as busy:
            return _json_error(409, "check_in_progress", run_id=busy.run_id)
        except RemoteChannelError as channel_error:
            return _json_error(400, channel_error.reason)
        return JSONResponse(status_code=202, content=check.to_payload())

    @manager.app.get("/api/remote-config/test/{run_id}")
    async def read_remote_check(request: Request, run_id: str):
        """Progress of a connection check started by this window."""
        rejected = reject(request, write=False)
        if rejected is not None:
            return rejected
        check = service.get(run_id)
        if check is None:
            return _json_error(404, "unknown_run")
        return JSONResponse(content=check.to_payload())
