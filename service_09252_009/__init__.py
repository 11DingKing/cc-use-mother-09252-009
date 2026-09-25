"""联合教研议题决议的服务端包入口。"""
from __future__ import annotations

PROJECT_CODE = "service_09252_009"

from .clock import FixedClock, SystemClock
from .errors import (
    AuthorizationError,
    ConflictError,
    DeadlinePassedError,
    NotFoundError,
    ResolutionError,
    ValidationError,
)
from .service import ResolutionService
from .storage import SQLiteRepository


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "联合教研议题决议"}


__all__ = [
    "PROJECT_CODE",
    "project_info",
    "ResolutionService",
    "SQLiteRepository",
    "SystemClock",
    "FixedClock",
    "ResolutionError",
    "ValidationError",
    "AuthorizationError",
    "ConflictError",
    "DeadlinePassedError",
    "NotFoundError",
]
