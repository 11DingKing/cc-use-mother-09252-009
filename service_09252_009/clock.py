"""可替换端口：时钟与标识生成，便于测试复现状态变化。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


class Clock:
    """时间端口。生产实现返回真实 UTC 时间，测试可注入固定时钟。"""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """从固定起点开始，可显式推进的时钟（测试用）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._at = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        if self._at.tzinfo is None:
            self._at = self._at.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._at

    def advance(self, **kwargs) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(**kwargs)

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock.set 需要带时区的时间")
        self._at = at.astimezone(timezone.utc)


def new_id() -> str:
    """生成短横线分隔的稳定标识。"""
    return uuid.uuid4().hex
