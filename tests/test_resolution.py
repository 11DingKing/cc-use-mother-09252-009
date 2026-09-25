"""决议服务的领域与应用服务测试。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from service_09252_009 import (
    AuthorizationError,
    ConflictError,
    DeadlinePassedError,
    FixedClock,
    ResolutionService,
    SQLiteRepository,
    ValidationError,
)


def make_service(path: str | None = None, start=None, register: bool = True) -> tuple[ResolutionService, FixedClock, str]:
    tmp = path or tempfile.mktemp(suffix=".db")
    clock = FixedClock(start or datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
    svc = ResolutionService(SQLiteRepository(tmp), clock)
    if register:
        for code, name, delegate in [
            ("uni-a", "甲国联合大学", "Alice"),
            ("uni-b", "乙国理工学院", "Bruno"),
            ("uni-c", "丙国师范大学", "Chen"),
        ]:
            svc.register_institution(code, name, delegate)
    return svc, clock, tmp


def open_issue(svc: ResolutionService, **kw) -> str:
    params = dict(
        title="联合培养学分互认",
        body="初版：互相承认至多 30 学分",
        author_institution="uni-a",
        deadline_local="2026-09-30 17:00",
        tzname="Asia/Shanghai",
    )
    params.update(kw)
    return svc.create_issue(**params)["issue_id"]


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def test_condition_blocks_adoption_until_fulfilled_and_signed(self) -> None:
        iid = open_issue(self.svc)
        self.svc.attach_condition(iid, "uni-b", "transcript", "须提供成绩单英译件")

        self.svc.cast_vote(iid, "uni-a", "for")
        r = self.svc.cast_vote(iid, "uni-b", "for")
        # 法定人数满足、全赞成，但条件未决
        self.assertTrue(r["evaluation"]["quorum_reached"])
        self.assertFalse(r["evaluation"]["conditions_clear"])
        self.assertFalse(r["evaluation"]["adopted"])
        self.assertEqual(r["issue_status"], "open")

        # 其他机构不能替 uni-b 关闭条件
        with self.assertRaises(AuthorizationError):
            self.svc.resolve_condition(iid, "uni-c", "transcript", True)
        self.svc.resolve_condition(iid, "uni-b", "transcript", True, note="已上传")

        # 条件清除但签署不足，仍不生效
        r = self.svc.sign(iid, "uni-a")
        self.assertTrue(r["evaluation"]["adopted"])
        self.assertFalse(r["evaluation"]["signatures_complete"])
        self.assertEqual(r["issue_status"], "open")

        r = self.svc.sign(iid, "uni-b")
        self.assertEqual(r["issue_status"], "adopted")
        self.assertTrue(r["evaluation"]["signatures_complete"])

        status = self.svc.get_status(iid)
        self.assertEqual(status["issue"]["adopted_seq"], 1)
        self.assertEqual(status["issue"]["status"], "adopted")

    def test_against_and_abstain_block_adoption(self) -> None:
        iid = open_issue(self.svc)
        self.svc.cast_vote(iid, "uni-a", "for")
        r = self.svc.cast_vote(iid, "uni-b", "abstain")
        self.assertTrue(r["evaluation"]["quorum_reached"])
        self.assertFalse(r["evaluation"]["all_for"])
        self.assertFalse(r["evaluation"]["adopted"])

        # 改为反对同样阻止
        r = self.svc.cast_vote(iid, "uni-b", "against", rationale="学分上限过高")
        self.assertEqual(r["evaluation"]["against"], 1)
        with self.assertRaises(AuthorizationError):
            self.svc.sign(iid, "uni-b")

    def test_quorum_not_reached(self) -> None:
        iid = open_issue(self.svc)
        self.svc.cast_vote(iid, "uni-a", "for")
        st = self.svc.get_status(iid)
        self.assertFalse(st["evaluation"]["quorum_reached"])
        self.assertTrue(
            any("法定人数" in reason for reason in st["evaluation"]["reasons"])
        )


class ProposalVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def test_revision_supersedes_conditions_and_old_votes(self) -> None:
        iid = open_issue(self.svc)
        self.svc.attach_condition(iid, "uni-b", "transcript", "需要英译成绩单")
        self.svc.cast_vote(iid, "uni-a", "for", rationale="v1 赞成")

        # 翻译澄清后提交 v2
        r = self.svc.submit_proposal_revision(
            iid, "uni-b", "互相承认至多 30 学分（须附英文成绩单）",
            language="zh", summary="澄清成绩单要求",
        )
        self.assertEqual(r["seq"], 2)

        status = self.svc.get_status(iid)
        cond = status["conditions"][0]
        self.assertEqual(cond["status"], "superseded")
        # v1 的票不计入当前版本
        self.assertEqual(status["evaluation"]["voted_count"], 0)

        self.svc.cast_vote(iid, "uni-a", "for", rationale="v2 仍赞成")
        self.svc.cast_vote(iid, "uni-b", "for", rationale="澄清后赞成")
        self.svc.sign(iid, "uni-a")
        self.svc.sign(iid, "uni-b")
        self.assertEqual(self.svc.get_status(iid)["issue"]["status"], "adopted")

        # 理由查询保留全部历史版本
        hist = self.svc.get_rationale(iid, "uni-a")
        self.assertEqual([p["proposal_seq"] for p in hist["positions"]], [1, 2])

    def test_cannot_revise_after_adoption_without_revision_flow(self) -> None:
        iid = open_issue(self.svc, quorum_ratio=1.0)
        for inst in ("uni-a", "uni-b", "uni-c"):
            self.svc.cast_vote(iid, inst, "for")
            self.svc.sign(iid, inst)
        with self.assertRaises(ConflictError):
            self.svc.submit_proposal_revision(iid, "uni-a", "新版正文")


class WithdrawAndRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def _adopt(self, iid: str) -> None:
        for inst in ("uni-a", "uni-b", "uni-c"):
            self.svc.cast_vote(iid, inst, "for")
        for inst in ("uni-a", "uni-b", "uni-c"):
            self.svc.sign(iid, inst)

    def test_withdraw_recomputes_open_issue(self) -> None:
        iid = open_issue(self.svc, quorum_ratio=1.0)
        for inst in ("uni-a", "uni-b", "uni-c"):
            self.svc.cast_vote(iid, inst, "for")
        # 三人赞成、仅两人签署：未生效
        self.svc.sign(iid, "uni-a")
        self.svc.sign(iid, "uni-b")
        self.assertEqual(self.svc.get_status(iid)["issue"]["status"], "open")

        r = self.svc.withdraw_vote(iid, "uni-c")
        self.assertFalse(r["evaluation"]["quorum_reached"])
        self.assertEqual(r["evaluation"]["voted_count"], 2)

        # 撤回幂等
        again = self.svc.withdraw_vote(iid, "uni-c")
        self.assertTrue(again["replayed"])

    def test_withdraw_after_adoption_starts_revision(self) -> None:
        iid = open_issue(self.svc, quorum_ratio=1.0)
        self._adopt(iid)
        with self.assertRaises(ConflictError):
            self.svc.withdraw_vote(iid, "uni-b")

        r = self.svc.request_revision(
            iid, "uni-b", "成绩单条款需要调整",
            new_deadline_local="2026-10-15 09:00", new_tzname="Europe/Berlin",
        )
        successor = r["successor_issue_id"]
        self.assertEqual(r["parent_status"], "under_revision")

        parent = self.svc.get_status(iid)
        self.assertEqual(parent["issue"]["status"], "under_revision")
        child = self.svc.get_status(successor)
        self.assertEqual(child["issue"]["parent_issue_id"], iid)
        self.assertEqual(child["issue"]["tzname"], "Europe/Berlin")
        self.assertIn("30 学分", child["proposals"][0]["body"])
        self.assertEqual(child["evaluation"]["voted_count"], 0)

        # 重复发起修订被拒绝
        with self.assertRaises(ConflictError):
            self.svc.request_revision(iid, "uni-c", "再次修订")

        # 后继议题独立走表决流程并生效，不影响原决议记录
        for inst in ("uni-a", "uni-b", "uni-c"):
            self.svc.cast_vote(successor, inst, "for")
            self.svc.sign(successor, inst)
        self.assertEqual(self.svc.get_status(successor)["issue"]["status"], "adopted")
        self.assertEqual(self.svc.get_status(iid)["issue"]["status"], "under_revision")


class ConflictRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def test_conflicted_member_excluded_from_base(self) -> None:
        iid = open_issue(self.svc)
        self.svc.declare_conflict(iid, "uni-c", True, reason="该校为联合培养出资方")
        with self.assertRaises(AuthorizationError):
            self.svc.cast_vote(iid, "uni-c", "for")

        # 只剩两家适格，两家全赞即满足法定人数
        self.svc.cast_vote(iid, "uni-a", "for")
        r = self.svc.cast_vote(iid, "uni-b", "for")
        self.assertEqual(r["evaluation"]["eligible_count"], 2)
        self.assertIn("uni-c", r["evaluation"]["conflicted_institutions"])
        self.svc.sign(iid, "uni-a")
        r = self.svc.sign(iid, "uni-b")
        self.assertEqual(r["issue_status"], "adopted")

    def test_conflict_withdrawal_reenlarges_base_and_blocks(self) -> None:
        iid = open_issue(self.svc, quorum_ratio=1.0)
        # c 先回避，a/b 两家全赞 → 生效
        self.svc.declare_conflict(iid, "uni-c", True)
        self.svc.cast_vote(iid, "uni-a", "for")
        self.svc.cast_vote(iid, "uni-b", "for")
        self.svc.sign(iid, "uni-a")
        self.svc.sign(iid, "uni-b")
        self.assertEqual(self.svc.get_status(iid)["issue"]["status"], "adopted")

        # 生效后撤销回避不能改变历史；这里验证 OPEN 议题下基数恢复
        iid2 = open_issue(self.svc, quorum_ratio=1.0)
        self.svc.declare_conflict(iid2, "uni-c", True)
        self.svc.declare_conflict(iid2, "uni-c", False)
        self.svc.cast_vote(iid2, "uni-a", "for")
        r = self.svc.cast_vote(iid2, "uni-b", "for")
        self.assertEqual(r["evaluation"]["eligible_count"], 3)
        self.assertFalse(r["evaluation"]["quorum_reached"])


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def test_explicit_key_replays_result(self) -> None:
        r1 = self.svc.create_issue(
            title="T", body="B", author_institution="uni-a",
            deadline_local="2026-10-01 10:00", tzname="UTC",
            idempotency_key="issue-1",
        )
        r2 = self.svc.create_issue(
            title="T", body="B-different", author_institution="uni-a",
            deadline_local="2026-10-01 10:00", tzname="UTC",
            idempotency_key="issue-1",
        )
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["issue_id"], r2["ref"])
        self.assertEqual(len(self.svc.list_issues()), 1)

    def test_natural_idempotency_same_vote(self) -> None:
        iid = open_issue(self.svc)
        r1 = self.svc.cast_vote(iid, "uni-a", "for", rationale="ok")
        r2 = self.svc.cast_vote(iid, "uni-a", "for", rationale="ok")
        self.assertTrue(r2["replayed"])
        status = self.svc.get_status(iid)
        self.assertEqual(sum(1 for v in status["votes"] if v["active"]), 1)

    def test_condition_code_idempotent(self) -> None:
        iid = open_issue(self.svc)
        self.svc.attach_condition(iid, "uni-b", "c1", "desc")
        r = self.svc.attach_condition(iid, "uni-b", "c1", "desc again")
        self.assertTrue(r["replayed"])


class DelegateReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock, self.path = make_service()

    def test_history_keeps_old_delegate(self) -> None:
        iid = open_issue(self.svc)
        self.svc.cast_vote(iid, "uni-a", "for", rationale="Alice 时期表态")
        r = self.svc.replace_delegate("uni-a", "Alicia")
        self.assertEqual(r["old_delegate"], "Alice")

        self.svc.cast_vote(iid, "uni-a", "for", rationale="换代表后重申")
        hist = self.svc.get_rationale(iid, "uni-a")["positions"]
        self.assertEqual([p["delegate"] for p in hist], ["Alice", "Alicia"])

    def test_institution_without_delegate_cannot_vote(self) -> None:
        self.svc.register_institution("uni-d", "丁国社区学院", None)
        iid = open_issue(self.svc)
        with self.assertRaises(ConflictError):
            self.svc.cast_vote(iid, "uni-d", "for")


class DeadlineTimezoneTests(unittest.TestCase):
    def test_deadline_enforced_in_declared_timezone(self) -> None:
        svc, clock, path = make_service()
        # 上海 17:00 == UTC 09:00
        iid = open_issue(svc, deadline_local="2026-09-25 17:00", tzname="Asia/Shanghai")
        status = svc.get_status(iid)
        self.assertEqual(status["issue"]["deadline_utc"][:16], "2026-09-25T09:00")

        svc.cast_vote(iid, "uni-a", "for")  # 08:00 UTC，未截止
        clock.advance(minutes=59)
        svc.cast_vote(iid, "uni-b", "for")  # 08:59 UTC，仍有效

        clock.advance(minutes=2)
        with self.assertRaises(DeadlinePassedError):
            svc.cast_vote(iid, "uni-c", "for")  # 09:01 UTC，已截止
        with self.assertRaises(DeadlinePassedError):
            svc.attach_condition(iid, "uni-c", "late", "逾期条件")
        with self.assertRaises(DeadlinePassedError):
            svc.submit_proposal_revision(iid, "uni-c", "逾期改版")

    def test_invalid_timezone_rejected(self) -> None:
        svc, _, _ = make_service()
        with self.assertRaises(ValidationError):
            open_issue(svc, tzname="Mars/Olympus")


class RestartPersistenceTests(unittest.TestCase):
    def test_state_and_audit_order_survive_restart(self) -> None:
        svc, clock, path = make_service()
        iid = open_issue(svc)
        svc.attach_condition(iid, "uni-b", "c1", "条件")
        svc.cast_vote(iid, "uni-a", "for")
        before = svc.list_audit(iid)

        svc2, _, _ = make_service(path, register=False)
        status = svc2.get_status(iid)
        self.assertEqual(status["issue"]["current_seq"], 1)
        self.assertEqual(status["conditions"][0]["code"], "c1")
        self.assertEqual(status["votes"][0]["stance"], "for")

        after = svc2.list_audit(iid)
        self.assertEqual([e["seq"] for e in before], [e["seq"] for e in after])
        types = [e["event_type"] for e in after]
        self.assertEqual(types, ["issue_created", "condition_attached", "vote_cast"])

        # 重启后审计序号继续单调递增，不复用
        svc2.cast_vote(iid, "uni-b", "for")
        new_seq = svc2.list_audit(iid)[-1]["seq"]
        self.assertGreater(new_seq, before[-1]["seq"])


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_votes_all_counted_once(self) -> None:
        svc, clock, path = make_service()
        iid = open_issue(svc)

        def vote(inst: str) -> dict:
            return svc.cast_vote(iid, inst, "for", rationale=f"{inst} ok")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(vote, ["uni-a", "uni-b", "uni-c"]))
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 3)

        ev = svc.get_status(iid)["evaluation"]
        self.assertEqual(ev["voted_count"], 3)

    def test_concurrent_duplicate_vote_single_winner(self) -> None:
        svc, clock, path = make_service()
        iid = open_issue(svc)
        errors: list[Exception] = []

        def vote() -> None:
            try:
                svc.cast_vote(iid, "uni-a", "for")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=vote) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 同态重复不报错；最终只有一张有效票
        active = [v for v in svc.get_status(iid)["votes"] if v["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(errors, [])

    def test_concurrent_same_idempotency_key_replays(self) -> None:
        svc, clock, path = make_service()
        created: list[str] = []

        def create() -> None:
            r = svc.create_issue(
                title="并发议题", body="B", author_institution="uni-a",
                deadline_local="2026-10-01 10:00", tzname="UTC",
                idempotency_key="concurrent-key",
            )
            created.append(r.get("ref") or r["issue_id"])

        threads = [threading.Thread(target=create) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(created)), 1)


class MemberScopeTests(unittest.TestCase):
    def test_non_member_has_no_vote(self) -> None:
        svc, _, _ = make_service()
        svc.register_institution("obs", "观察员机构", "Otto", is_member=False)
        iid = open_issue(svc)
        with self.assertRaises(AuthorizationError):
            svc.cast_vote(iid, "obs", "for")


if __name__ == "__main__":
    unittest.main()
