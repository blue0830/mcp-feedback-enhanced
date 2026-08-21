#!/usr/bin/env python3
"""Blocking CLI entrypoint for interactive feedback collection."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .debug import server_debug_log as debug_log
from .desktop_app import DesktopApp
from .server import create_feedback_text, init_encoding, normalize_feedback_result
from .utils.error_handler import ErrorHandler
from .web.main import WebUIManager


DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_SUMMARY = "我已完成了您請求的任務。"
DEFAULT_IMAGE_TTL_SECONDS = 3600
IMAGE_ARTIFACT_ROOT = Path.home() / ".cache" / "mcp-feedback-enhanced" / "cli-images"


def build_parser() -> argparse.ArgumentParser:
    """Create CLI parser for feedback collection mode."""
    parser = argparse.ArgumentParser(
        prog="feedback-cli",
        description="Launch one interactive feedback session and block for the result.",
    )
    parser.add_argument(
        "--project-directory",
        default=".",
        help="Project directory path used in feedback context.",
    )

    summary_group = parser.add_mutually_exclusive_group()
    summary_group.add_argument(
        "--summary",
        default=None,
        help="Inline summary text for the feedback session.",
    )
    summary_group.add_argument(
        "--summary-file",
        default=None,
        help="Read summary text from a UTF-8 file.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Wait timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    return parser


def ensure_foreground_blocking_invocation() -> None:
    """Reject detached/background invocation styles for this CLI."""
    if not hasattr(sys.stdin, "isatty"):
        raise RuntimeError(
            "feedback-cli 只支持前台阻塞调用（需要可交互的前台终端输入）。"
        )

    try:
        stdin_is_tty = sys.stdin.isatty()
    except (ValueError, OSError):
        stdin_is_tty = False

    if not stdin_is_tty:
        raise RuntimeError(
            "feedback-cli 只支持前台阻塞调用（需要可交互的前台终端输入）。"
        )


def resolve_project_directory(raw_path: str) -> str:
    """Resolve project directory with MCP-compatible fallback semantics."""
    if os.path.exists(raw_path):
        return os.path.abspath(raw_path)
    return os.path.abspath(os.getcwd())


def resolve_summary(summary: str | None, summary_file: str | None) -> str:
    """Resolve summary text from CLI flags."""
    if summary and summary_file:
        raise ValueError("不能同時指定 --summary 與 --summary-file。")
    if summary_file:
        file_path = Path(summary_file)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(f"摘要檔案不存在: {summary_file}")
        return file_path.read_text(encoding="utf-8")
    if summary:
        return summary
    return DEFAULT_SUMMARY


def cleanup_expired_image_artifacts(
    root_dir: Path = IMAGE_ARTIFACT_ROOT, ttl_seconds: int = DEFAULT_IMAGE_TTL_SECONDS
) -> int:
    """Delete expired image artifacts and empty directories."""
    if ttl_seconds <= 0 or not root_dir.exists():
        return 0

    expire_before = time.time() - ttl_seconds
    removed_count = 0

    for file_path in root_dir.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            if file_path.stat().st_mtime < expire_before:
                file_path.unlink(missing_ok=True)
                removed_count += 1
        except Exception as cleanup_error:
            debug_log(f"清理過期圖片失敗: {file_path} ({cleanup_error})")

    for directory in sorted(root_dir.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass

    return removed_count


def _sanitize_filename(name: str, fallback_index: int) -> str:
    """Generate a filesystem-safe filename for persisted images."""
    safe_name = name.strip() or f"image_{fallback_index:02d}.bin"
    safe_name = re.sub(r"[^\w.\-]+", "_", safe_name)
    return safe_name


def persist_feedback_images(
    images: list[dict[str, Any]],
    session_id: str,
    root_dir: Path = IMAGE_ARTIFACT_ROOT,
) -> list[str]:
    """Persist feedback images to session-scoped files and return file paths."""
    if not images:
        return []

    session_dir = root_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    persisted_paths: list[str] = []

    for index, image in enumerate(images, 1):
        if not isinstance(image, dict):
            continue
        data = image.get("data")
        if data is None:
            continue

        try:
            if isinstance(data, bytes):
                image_bytes = data
            elif isinstance(data, str):
                data_value = data.split(",", 1)[1] if data.startswith("data:") else data
                image_bytes = base64.b64decode(data_value)
            else:
                continue

            file_name = _sanitize_filename(str(image.get("name", "")), index)
            file_path = session_dir / file_name
            file_path.write_bytes(image_bytes)
            persisted_paths.append(str(file_path))
        except Exception as image_error:
            debug_log(f"圖片落盤失敗 (index={index}): {image_error}")

    return persisted_paths


def render_cli_output(result: dict[str, Any], image_paths: list[str]) -> str:
    """Render CLI output text consistent with MCP-visible semantics."""
    output_text = create_feedback_text(result)
    if image_paths:
        path_lines = "\n".join(f"- {path}" for path in image_paths)
        output_text = f"{output_text}\n\n=== 圖片檔案路徑 ===\n{path_lines}"
    return output_text


class CliSessionRuntime:
    """Run one isolated feedback session for a single CLI invocation.

    Responsibilities:
    - create isolated manager/session/backend for this process only;
    - launch desktop UI first and fallback to browser on startup failure;
    - return normalized text output and persist image artifacts.

    Limitations:
    - one runtime handles exactly one feedback session;
    - caller is responsible for external timeout wrapping.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.manager = WebUIManager(host="127.0.0.1", port=0)
        self.desktop_app: DesktopApp | None = None

    async def run(self) -> int:
        project_directory = resolve_project_directory(self.args.project_directory)
        summary = resolve_summary(self.args.summary, self.args.summary_file)
        if self.args.timeout <= 0:
            raise ValueError("--timeout 必須大於 0 秒。")

        session_id = self.manager.create_session(project_directory, summary)
        session = self.manager.get_current_session()
        if session is None:
            raise RuntimeError("無法創建回饋會話")

        self.manager.start_server()
        feedback_url = self.manager.get_server_url()

        await self._launch_desktop_first(feedback_url)
        raw_result = await session.wait_for_feedback(self.args.timeout)
        normalized_result = normalize_feedback_result(raw_result)
        image_paths = persist_feedback_images(normalized_result["images"], session_id)
        print(render_cli_output(normalized_result, image_paths))
        return 0

    async def _launch_desktop_first(self, feedback_url: str) -> None:
        """Try desktop first and fallback to browser if startup fails."""
        desktop_app = DesktopApp()
        desktop_app.set_desktop_mode(True)
        try:
            await desktop_app.launch_tauri_app(feedback_url)
            if not desktop_app.is_running():
                raise RuntimeError("桌面應用程式在啟動後立即退出")
            self.desktop_app = desktop_app
        except Exception as desktop_error:
            debug_log(f"桌面模式啟動失敗，回退瀏覽器: {desktop_error}")
            try:
                desktop_app.stop()
            except Exception:
                pass
            os.environ.pop("MCP_DESKTOP_MODE", None)
            self.manager.open_browser(feedback_url)

    async def shutdown(self) -> None:
        """Perform bounded shutdown for desktop child and backend manager."""
        if self.desktop_app:
            try:
                self.desktop_app.stop()
            except Exception as desktop_shutdown_error:
                debug_log(f"關閉桌面應用程式失敗: {desktop_shutdown_error}")
            finally:
                self.desktop_app = None

        try:
            self.manager.stop()
        except Exception as manager_shutdown_error:
            debug_log(f"關閉 Web 服務失敗: {manager_shutdown_error}")
        finally:
            os.environ.pop("MCP_DESKTOP_MODE", None)


async def _run_cli(args: argparse.Namespace) -> int:
    cleanup_expired_image_artifacts()
    runtime = CliSessionRuntime(args)
    try:
        return await runtime.run()
    finally:
        await runtime.shutdown()
        cleanup_expired_image_artifacts()


def main() -> int:
    """CLI main entrypoint."""
    ensure_foreground_blocking_invocation()
    init_encoding()
    parser = build_parser()
    args = parser.parse_args()

    try:
        return asyncio.run(_run_cli(args))
    except KeyboardInterrupt:
        print("❌ 操作被中斷。")
        return 1
    except Exception as error:
        user_error = ErrorHandler.format_user_error(error, include_technical=False)
        print(user_error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
