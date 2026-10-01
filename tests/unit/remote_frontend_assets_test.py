#!/usr/bin/env python3
"""Consistency tests for the remote communication frontend assets.

Covered rules:
- the three locales carry the same ``remoteChannel`` keys with the same placeholders;
- every translation key used by the card templates and by the remote scripts (including
  keys the scripts compose at runtime) exists in every locale;
- every reason code the backend can report has a localized text;
- the new stylesheet and scripts are served by the web app.

Limitations:
- the scripts themselves are not executed here (no JavaScript engine in the test
  environment); their behavior is checked in a real browser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_feedback_enhanced.remote.discord_check import DiscordConnectionChecker
from mcp_feedback_enhanced.remote.models import RemoteState
from mcp_feedback_enhanced.web.main import WebUIManager


PACKAGE_DIR = Path(__file__).resolve().parents[2] / "src" / "mcp_feedback_enhanced"
WEB_DIR = PACKAGE_DIR / "web"
REMOTE_PY_DIR = PACKAGE_DIR / "remote"
REMOTE_JS_DIR = WEB_DIR / "static" / "js" / "modules" / "remote"
TEMPLATES = (
    WEB_DIR / "templates" / "components" / "remote-settings-card.html",
    WEB_DIR / "templates" / "remote_settings.html",
)
LANGUAGES = ("zh-TW", "zh-CN", "en")

I18N_ATTRIBUTE = re.compile(r'data-i18n(?:-placeholder|-title|-aria-label)?="([^"]+)"')
SCRIPT_KEY = re.compile(r"""['"](remoteChannel\.[A-Za-z0-9_.]+)['"]""")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Code patterns through which the backend reports a machine-readable reason code.
REASON_PATTERNS = (
    re.compile(r'reason="([a-z_]+)"'),
    re.compile(r'REASON_[A-Z_]+ = "([a-z_]+)"'),
    re.compile(r'\.fail\(\s*\w+,\s*"([a-z_]+)"'),
    re.compile(r'fail_running\(\s*"([a-z_]+)"'),
    re.compile(r'RemoteState\.UNAVAILABLE,[^"\n]*"([a-z_]+)"'),
    re.compile(r'ConfigError\("([a-z_]+)"\)'),
    re.compile(r'return "([a-z]+_required)"'),
    re.compile(r'_json_error\(\s*\d+,\s*"([a-z_]+)"'),
)

# Reason codes documented in the change's specs (some are only produced by the scripts
# themselves or computed at runtime, so the source scan cannot see all of them).
DOCUMENTED_REASONS = {
    "network_error",
    "rate_limited",
    "auth_failed",
    "missing_permission",
    "not_found",
    "server_error",
    "forum_tag_required",
    "bad_request",
    "bad_response",
    "not_open",
    "not_forum",
    "timeout",
    "message_content",
    "no_text",
    "too_large",
    "bad_host",
    "download_failed",
    "submit_failed",
    "internal_error",
    "cancelled",
    "config_changed",
    "save_failed",
    "forbidden",
    "payload_too_large",
    "invalid_payload",
    "unsupported_provider",
    "invalid_token",
    "invalid_channel_id",
    "invalid_user_id",
    "token_required",
    "channel_required",
    "allowlist_required",
    "not_verified",
    "check_in_progress",
    "unknown_run",
    "unavailable",
    "request_failed",
}

# One code per scan pattern: proves every pattern still matches something, so a refactor
# of the reporting style cannot silently shrink the check.
SCAN_SENTINELS = {
    "auth_failed",  # reason="..."
    "too_large",  # REASON_... constants
    "not_forum",  # check.fail(step, "...")
    "cancelled",  # check.fail_running("...")
    "submit_failed",  # RemoteState.UNAVAILABLE, "..."
    "invalid_token",  # ConfigError("...")
    "token_required",  # return "..._required"
    "unknown_run",  # _json_error(status, "...")
}


def _flatten(tree: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


@pytest.fixture(scope="module")
def locales() -> dict[str, dict[str, str]]:
    result = {}
    for language in LANGUAGES:
        path = WEB_DIR / "locales" / language / "translation.json"
        result[language] = _flatten(json.loads(path.read_text(encoding="utf-8")))
    return result


def _remote_keys(flat: dict[str, str]) -> set[str]:
    return {key for key in flat if key.startswith("remoteChannel.")}


def test_locales_carry_the_same_remote_keys(locales):
    reference = _remote_keys(locales["zh-TW"])

    assert reference
    for language in LANGUAGES:
        assert _remote_keys(locales[language]) == reference, language


def test_remote_texts_are_filled_and_share_their_placeholders(locales):
    for key in _remote_keys(locales["zh-TW"]):
        placeholder_sets = set()
        for language in LANGUAGES:
            text = locales[language][key]
            assert isinstance(text, str)
            assert text.strip(), (language, key)
            placeholder_sets.add(frozenset(PLACEHOLDER.findall(text)))
        assert len(placeholder_sets) == 1, (key, placeholder_sets)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_template_keys_exist_in_every_locale(template, locales):
    keys = I18N_ATTRIBUTE.findall(template.read_text(encoding="utf-8"))

    assert keys
    for language in LANGUAGES:
        missing = [key for key in keys if key not in locales[language]]
        assert not missing, (language, missing)


def _runtime_key_prefixes() -> dict[str, tuple[str, ...]]:
    """Key prefixes the scripts extend at runtime, with the values they append."""
    return {
        "remoteChannel.status.": tuple(state.value for state in RemoteState),
        "remoteChannel.check.steps.": DiscordConnectionChecker.step_ids,
    }


def test_script_keys_exist_in_every_locale(locales):
    static_keys: set[str] = set()
    prefixes: set[str] = set()
    for script in REMOTE_JS_DIR.glob("*.js"):
        for key in SCRIPT_KEY.findall(script.read_text(encoding="utf-8")):
            (prefixes if key.endswith(".") else static_keys).add(key)

    # A new runtime-composed prefix must be registered here so its values get checked.
    runtime = _runtime_key_prefixes()
    assert prefixes <= set(runtime) | {"remoteChannel.reasons."}
    expected = set(static_keys)
    for prefix, suffixes in runtime.items():
        expected |= {prefix + suffix for suffix in suffixes}

    assert static_keys
    for language in LANGUAGES:
        missing = sorted(key for key in expected if key not in locales[language])
        assert not missing, (language, missing)


def _reason_codes_found_in_sources() -> set[str]:
    sources = [
        *REMOTE_PY_DIR.glob("*.py"),
        WEB_DIR / "routes" / "remote_routes.py",
        PACKAGE_DIR / "cli.py",
    ]
    found: set[str] = set()
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for pattern in REASON_PATTERNS:
            found.update(pattern.findall(text))
    return found


def test_every_reason_code_has_a_text_in_every_locale(locales):
    codes = _reason_codes_found_in_sources()

    assert codes >= SCAN_SENTINELS
    prefix = "remoteChannel.reasons."
    for language in LANGUAGES:
        texts = {
            key[len(prefix) :] for key in locales[language] if key.startswith(prefix)
        }
        missing = sorted((codes | DOCUMENTED_REASONS) - texts)
        assert not missing, (language, missing)


@pytest.mark.parametrize(
    "path",
    [
        "/static/css/remote-settings.css",
        "/static/js/modules/remote/remote-common.js",
        "/static/js/modules/remote/remote-status-badge.js",
        "/static/js/modules/remote/remote-settings-card.js",
        "/static/js/modules/remote/remote-settings-page.js",
    ],
)
def test_remote_assets_are_served(path):
    manager = WebUIManager(host="127.0.0.1", port=0)

    with TestClient(manager.app, base_url="http://127.0.0.1") as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.content
