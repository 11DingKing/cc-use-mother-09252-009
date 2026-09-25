"""测试公共夹具：内存/文件库 + 可控时钟 + 院校快速建档。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from service_09252_009.service import Clock, ResolutionService
from service_09252_009.storage import SQLiteStore


class FakeClock(Clock):
    def __init__(self, dt: datetime | None = None):
        self.t = dt or datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.t

    def set(self, dt: datetime) -> None:
        self.t = dt


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = SQLiteStore(":memory:")
        self.svc = ResolutionService(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()

    def seed_institution(self, code: str, name: str | None = None,
                         rep_name: str = "代表"):
        """注册院校并任命代表，返回 (institution_id, representative_id)。"""
        inst_id = self.svc.register_institution(
            code, name or f"{code}校")["institution"]["id"]
        rep_id = self.svc.appoint_representative(
            inst_id, f"{rep_name}-{code}")["representative"]["id"]
        return inst_id, rep_id

    def make_pending_proposal(self, quorum: int = 2, n_schools: int = 3,
                              deadline=None, deadline_tz="UTC",
                              with_condition=True):
        """建档 n_schools 所院校并提交一个带必备条件的议题（默认不会立刻生效）。"""
        schools = [self.seed_institution(chr(ord("A") + i))
                   for i in range(n_schools)]
        author, rep = schools[0]
        pid = self.svc.submit_proposal(
            "联合教研议题", "正文", quorum_required=quorum,
            author_institution_id=author,
            deadline=deadline, deadline_timezone=deadline_tz)["proposal_id"]
        cond_id = None
        if with_condition:
            cond_id = self.svc.add_condition(
                pid, "签署互认协议", acting_institution_id=author,
                representative_id=rep)["condition"]["id"]
        return pid, schools, cond_id


class FileStoreTestCase(unittest.TestCase):
    """每个用例使用独立临时 SQLite 文件。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "resolution.db")
        self.clock = FakeClock()
        self.store = SQLiteStore(self.db_path)
        self.svc = ResolutionService(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def seed_institution(self, code: str, name: str | None = None,
                         rep_name: str = "代表"):
        inst_id = self.svc.register_institution(
            code, name or f"{code}校")["institution"]["id"]
        rep_id = self.svc.appoint_representative(
            inst_id, f"{rep_name}-{code}")["representative"]["id"]
        return inst_id, rep_id

    def reopen(self) -> None:
        """关闭后以同路径重开，模拟服务重启。"""
        self.store.close()
        self.store = SQLiteStore(self.db_path)
        self.svc = ResolutionService(self.store, clock=self.clock)
