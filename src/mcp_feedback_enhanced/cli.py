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
from .remote import (
    REMOTE_SOURCE_PREFIX,
    RemoteChannelConfig,
    RemoteConfigStore,
    RemoteOutcome,
    RemoteReply,
    RemoteRequest,
    RemoteSessionCoordinator,
    RemoteState,
    RemoteStatus,
    SubmitResult,
)
from .remote.factory import create_channel
from .server import create_feedback_text, init_encoding, normalize_feedback_result
from .utils.error_handler import ErrorHandler
from .web.main import WebUIManager
from .web.models import WebFeedbackSession


DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_SUMMARY = "我已完成了您請求的任務。"
DEFAULT_IMAGE_TTL_SECONDS = 3600
# Upper bound for telling the local window about a remote submission: the outcome is
# already decided by then, so the notification is best effort.
REMOTE_NOTIFY_TIMEOUT_SECONDS = 3.0
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

    # Exclusive on purpose: a feedback session needs a summary, the settings mode has none.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--summary",
        default=None,
        help="Inline summary text for the feedback session.",
    )
    mode_group.add_argument(
        "--summary-file",
        default=None,
        help="Read summary text from a UTF-8 file.",
    )
    mode_group.add_argument(
        "--remote-settings",
        action="store_true",
        help=(
            "Open only the remote communication settings (for humans, not for Agents): "
            "no feedback session is created and nothing is forwarded. "
            "--timeout bounds how long the window stays open."
        ),
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


async def launch_desktop_first(manager: WebUIManager, url: str) -> DesktopApp | None:
    """Open ``url`` in the desktop window, falling back to the browser on failure.

    Shared by every CLI runtime that shows a window. Returns the running desktop app,
    or None when the browser fallback was used (the caller then cannot observe when the
    user closes the page).
    """
    desktop_app = DesktopApp()
    desktop_app.set_desktop_mode(True)
    try:
        await desktop_app.launch_tauri_app(url)
        if not desktop_app.is_running():
            raise RuntimeError("桌面應用程式在啟動後立即退出")
        return desktop_app
    except Exception as desktop_error:
        debug_log(f"桌面模式啟動失敗，回退瀏覽器: {desktop_error}")
        try:
            desktop_app.stop()
        except Exception:
            pass
        os.environ.pop("MCP_DESKTOP_MODE", None)
        manager.open_browser(url)
        return None


class CliSessionRuntime:
    """Run one isolated feedback session for a single CLI invocation.

    Responsibilities:
    - create isolated manager/session/backend for this process only;
    - launch desktop UI first and fallback to browser on startup failure;
    - return normalized text output and persist image artifacts;
    - when remote communication is effective, mirror the session to the remote provider
      through a background coordinator and finalize it on every end path.

    Limitations:
    - one runtime handles exactly one feedback session;
    - caller is responsible for external timeout wrapping;
    - the remote configuration is read once at startup, so later edits only affect the
      next invocation;
    - remote problems never change the local result, they only show up as a status
      badge in the window.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.manager = WebUIManager(host="127.0.0.1", port=0)
        # Exposes the remote settings card and endpoints in this CLI-hosted window.
        self.manager.remote_settings_available = True
        self.desktop_app: DesktopApp | None = None
        self._session: Any = None
        self._coordinator: RemoteSessionCoordinator | None = None
        # How the wait ended; consumed by shutdown() to finalize the remote conversation.
        self._end_outcome: RemoteOutcome | None = None

    async def run(self) -> int:
        project_directory = resolve_project_directory(self.args.project_directory)
        summary = resolve_summary(self.args.summary, self.args.summary_file)
        if self.args.timeout <= 0:
            raise ValueError("--timeout 必須大於 0 秒。")

        session_id = self.manager.create_session(project_directory, summary)
        session = self.manager.get_current_session()
        if session is None:
            raise RuntimeError("無法創建回饋會話")
        self._session = session

        # Read the remote configuration exactly once; edits made while this invocation
        # waits only take effect from the next one.
        remote_config = self._load_remote_config()
        session.remote_status = self._initial_remote_status(remote_config).to_payload()

        self.manager.start_server()
        feedback_url = self.manager.get_server_url()

        await self._launch_desktop_first(feedback_url)
        self._start_remote_channel(remote_config, project_directory, summary)

        try:
            raw_result = await session.wait_for_feedback(self.args.timeout)
        except TimeoutError:
            self._end_outcome = RemoteOutcome.TIMEOUT
            raise
        except asyncio.CancelledError:
            self._end_outcome = RemoteOutcome.INTERRUPTED
            raise
        except Exception:
            self._end_outcome = RemoteOutcome.ERROR
            raise
        self._end_outcome = self._resolve_answer_outcome(session)

        normalized_result = normalize_feedback_result(raw_result)
        image_paths = persist_feedback_images(normalized_result["images"], session_id)
        print(render_cli_output(normalized_result, image_paths))
        return 0

    async def _launch_desktop_first(self, feedback_url: str) -> None:
        """Try desktop first and fallback to browser if startup fails."""
        self.desktop_app = await launch_desktop_first(self.manager, feedback_url)

    def _load_remote_config(self) -> RemoteChannelConfig:
        """Read the latest remote configuration; any problem means local-only."""
        try:
            return RemoteConfigStore().load()
        except Exception as config_error:
            debug_log(
                f"讀取遠端設定失敗，改為僅本地模式: {type(config_error).__name__}"
            )
            return RemoteChannelConfig()

    @staticmethod
    def _initial_remote_status(config: RemoteChannelConfig) -> RemoteStatus:
        """Status shown before (or instead of) any remote activity."""
        if config.is_effective():
            return RemoteStatus(RemoteState.CONNECTING, config.provider)
        reason = None
        if config.enabled:
            # Switched on but unusable: tell the user why instead of failing silently.
            reason = config.missing_field() or "not_verified"
        return RemoteStatus(RemoteState.OFF, config.provider, reason)

    def _start_remote_channel(
        self, config: RemoteChannelConfig, project_directory: str, summary: str
    ) -> None:
        """Start the background coordinator when remote communication is effective.

        Never raises and never waits for the network; every failure leaves the session
        local-only.
        """
        if not config.is_effective():
            return  # Strict no-op: no remote object, no network request
        try:
            channel = create_channel(config)
            request = RemoteRequest(
                session_id=self._session.session_id,
                project_directory=project_directory,
                summary=summary,
                deadline_epoch=time.time()
                + WebFeedbackSession.effective_wait_timeout(self.args.timeout),
                timeout_seconds=self.args.timeout,
            )
            coordinator = RemoteSessionCoordinator(
                channel,
                request,
                submit=self._submit_remote_reply,
                report_status=self._report_remote_status,
            )
            coordinator.start()
            self._coordinator = coordinator
        except Exception as start_error:
            debug_log(f"啟動遠端通道失敗，改為僅本地模式: {type(start_error).__name__}")
            self._report_remote_status(
                RemoteStatus(
                    RemoteState.UNAVAILABLE, config.provider, "internal_error", None
                )
            )

    def _report_remote_status(self, status: RemoteStatus) -> None:
        """Store the status on the session and push it to the window (thread-safe)."""
        if self._session is not None:
            self.manager.push_remote_status(self._session, status.to_payload())

    async def _submit_remote_reply(
        self, reply: RemoteReply, source: str
    ) -> SubmitResult:
        """Hand a remote reply to the session through the first-success arbitration."""
        session = self._session
        if session is None:
            return SubmitResult(False)

        # The commit is thread-safe and decides the outcome; it runs right here so a
        # slow or closed web loop can never lose or delay a reply.
        result = session.commit_feedback(reply.text, [], {}, source)
        if result.accepted:
            session.add_user_message(
                {
                    "content": reply.text,
                    "submission_method": source.removeprefix(REMOTE_SOURCE_PREFIX),
                }
            )

        # The window's websocket belongs to the web server loop: notify it there.
        try:
            await self.manager.await_on_server_loop(
                session.notify_commit_result(result, source),
                timeout=REMOTE_NOTIFY_TIMEOUT_SECONDS,
            )
        except Exception as notify_error:
            debug_log(f"通知本地視窗失敗（忽略）: {type(notify_error).__name__}")

        return SubmitResult(result.accepted, None if result.accepted else result.source)

    @staticmethod
    def _resolve_answer_outcome(session: Any) -> RemoteOutcome:
        """Tell a remote answer from a local one using the arbitration winner."""
        source = getattr(session, "feedback_source", None)
        if isinstance(source, str) and source.startswith(REMOTE_SOURCE_PREFIX):
            return RemoteOutcome.REMOTE_ANSWERED
        return RemoteOutcome.LOCAL_ANSWERED

    async def shutdown(self) -> None:
        """Perform bounded shutdown for desktop child, remote conversation and backend."""
        # Close the local window first: it must never linger while the remote
        # conversation is being finalized.
        if self.desktop_app:
            try:
                self.desktop_app.stop()
            except Exception as desktop_shutdown_error:
                debug_log(f"關閉桌面應用程式失敗: {desktop_shutdown_error}")
            finally:
                self.desktop_app = None

        # Both steps are bounded and exception-free by contract, cannot change the CLI
        # result and touch different remote posts, so they run side by side to keep the
        # worst-case exit delay at the longer of the two.
        coordinator, self._coordinator = self._coordinator, None
        cleanup_steps = [self.manager.shutdown_remote_checks()]
        if coordinator is not None:
            cleanup_steps.append(
                coordinator.finalize(self._end_outcome or RemoteOutcome.INTERRUPTED)
            )
        await asyncio.gather(*cleanup_steps, return_exceptions=True)

        try:
            self.manager.stop()
        except Exception as manager_shutdown_error:
            debug_log(f"關閉 Web 服務失敗: {manager_shutdown_error}")
        finally:
            os.environ.pop("MCP_DESKTOP_MODE", None)


class RemoteSettingsRuntime:
    """Host the standalone remote settings window (``feedback-cli --remote-settings``).

    Responsibilities:
    - serve the settings card alone: no feedback session, no WebSocket session state, no
      remote forwarding (the remote coordinator is never created);
    - open the window desktop first (browser as fallback) and stay alive until the user
      closes it, presses Ctrl+C or ``--timeout`` elapses;
    - on the way out, stop the window, cancel a running connection check (so its test
      post gets archived) and stop the web server, all within bounded time.

    Limitations:
    - this mode is for humans: it consumes no Agent request and produces no result;
    - a page opened through the browser fallback cannot be observed, so only Ctrl+C or
      the timeout ends it;
    - every normal way out is exit code 0 (there is no feedback result to report).
    """

    # How often the desktop child process is checked while the window is open.
    DESKTOP_POLL_SECONDS = 0.5

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.manager = WebUIManager(host="127.0.0.1", port=0)
        # Serves the card and the remote configuration endpoints.
        self.manager.remote_settings_available = True
        self.desktop_app: DesktopApp | None = None

    async def run(self) -> int:
        if self.args.timeout <= 0:
            raise ValueError("--timeout 必須大於 0 秒。")

        self.manager.start_server()
        settings_url = f"{self.manager.get_server_url()}/remote-settings"
        self.desktop_app = await launch_desktop_first(self.manager, settings_url)
        if self.desktop_app is None:
            print("远程设置页已在浏览器中打开；设置完成后按 Ctrl+C 退出。")

        await self._wait_until_closed()
        return 0

    async def _wait_until_closed(self) -> None:
        """Return when the desktop window closed or the timeout elapsed."""
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            if self.desktop_app is not None and not self.desktop_app.is_running():
                return
            await asyncio.sleep(self.DESKTOP_POLL_SECONDS)
        debug_log("遠端設定模式已達 --timeout，結束")

    async def shutdown(self) -> None:
        """Stop the window, any running connection check and the web server (bounded)."""
        if self.desktop_app:
            try:
                self.desktop_app.stop()
            except Exception as desktop_shutdown_error:
                debug_log(f"關閉桌面應用程式失敗: {desktop_shutdown_error}")
            finally:
                self.desktop_app = None

        # Exception-free by contract: a test post must be archived before the server goes.
        await self.manager.shutdown_remote_checks()

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


async def _run_remote_settings(args: argparse.Namespace) -> int:
    runtime = RemoteSettingsRuntime(args)
    try:
        return await runtime.run()
    except asyncio.CancelledError:
        # Ctrl+C reaches the main task as a cancellation. In this mode it is the normal
        # way out of the browser fallback, so absorb it and report success.
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        return 0
    finally:
        await runtime.shutdown()


def main() -> int:
    """CLI main entrypoint."""
    ensure_foreground_blocking_invocation()
    init_encoding()
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.remote_settings:
            return asyncio.run(_run_remote_settings(args))
        return asyncio.run(_run_cli(args))
    except KeyboardInterrupt:
        if args.remote_settings:
            return 0  # Ctrl+C is how this mode is normally left
        print("❌ 操作被中斷。")
        return 1
    except Exception as error:
        user_error = ErrorHandler.format_user_error(error, include_technical=False)
        print(user_error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
