#!/usr/bin/env python3
"""Unit tests for feedback CLI helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from mcp_feedback_enhanced.cli import (
    DEFAULT_TIMEOUT_SECONDS,
    build_parser,
    cleanup_expired_image_artifacts,
    ensure_foreground_blocking_invocation,
    persist_feedback_images,
    render_cli_output,
    resolve_project_directory,
    resolve_summary,
)


def test_resolve_summary_from_file(tmp_path: Path):
    summary_file = tmp_path / "summary.txt"
    summary_file.write_text("summary from file", encoding="utf-8")
    assert resolve_summary(None, str(summary_file)) == "summary from file"


def test_resolve_summary_conflict_raises():
    with pytest.raises(ValueError):
        resolve_summary("inline", "summary.txt")


def test_resolve_project_directory_fallbacks_to_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = resolve_project_directory("missing-directory")
    assert result == os.path.abspath(tmp_path)


def test_persist_feedback_images_returns_paths(tmp_path: Path):
    images = [{"name": "demo.png", "data": b"abc123", "size": 6}]
    paths = persist_feedback_images(images, "session-a", root_dir=tmp_path)
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.exists()
    assert saved.read_bytes() == b"abc123"


def test_cleanup_expired_image_artifacts(tmp_path: Path):
    stale_file = tmp_path / "session-a" / "old.png"
    stale_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_bytes(b"stale")
    old_time = time.time() - 7200
    os.utime(stale_file, (old_time, old_time))

    removed_count = cleanup_expired_image_artifacts(tmp_path, ttl_seconds=3600)
    assert removed_count == 1
    assert not stale_file.exists()


def test_render_cli_output_includes_image_paths():
    output = render_cli_output(
        {"interactive_feedback": "ok", "command_logs": "", "images": [], "settings": {}},
        ["C:/tmp/a.png"],
    )
    assert "=== 圖片檔案路徑 ===" in output
    assert "C:/tmp/a.png" in output


def test_foreground_guard_rejects_non_tty_stdin(monkeypatch):
    class _FakeStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("mcp_feedback_enhanced.cli.sys.stdin", _FakeStdin())
    with pytest.raises(RuntimeError):
        ensure_foreground_blocking_invocation()


def test_cli_default_timeout_is_one_hour():
    parser = build_parser()
    args = parser.parse_args([])
    assert DEFAULT_TIMEOUT_SECONDS == 3600
    assert args.timeout == 3600
