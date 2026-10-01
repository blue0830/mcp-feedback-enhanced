#!/usr/bin/env python3
"""
Web UI 資料模型模組
==================

定義 Web UI 相關的資料結構和型別。
"""

from .feedback_result import FeedbackResult
from .feedback_session import (
    SOURCE_CLEANUP,
    SOURCE_USER_TIMEOUT,
    SOURCE_WAIT_TIMEOUT,
    SOURCE_WEB,
    CleanupReason,
    CommitResult,
    SessionStatus,
    WebFeedbackSession,
)


__all__ = [
    "SOURCE_CLEANUP",
    "SOURCE_USER_TIMEOUT",
    "SOURCE_WAIT_TIMEOUT",
    "SOURCE_WEB",
    "CleanupReason",
    "CommitResult",
    "FeedbackResult",
    "SessionStatus",
    "WebFeedbackSession",
]
