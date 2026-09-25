"""生效评估规则：法定人数、利益冲突、弃权、条件与撤回重算。"""

from service_09252_009.errors import (
    AuthorizationError,
    ConflictError,
    StateError,
)
from service_09252_009.models import COND_CLASS_ADVISORY

from support import ServiceTestCase


class QuorumAndMajorityTests(ServiceTestCase):
    def test_quorum_abstention_and_majority(self):
        # 4 所院校，法定人数 3；弃权不计入有效表态。
        pid, schools, _ = self.make_pending_proposal(
            quorum=3, n_schools=4, with_condition=False)
        (a, ra), (b, rb), (c, rc), (d, rd) = schools

        r = self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote="favor")
        self.assertFalse(r["evaluation"]["effective"])
        self.assertIn("法定人数不足", r["evaluation"]["reasons"][0])

        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="abstain")
        detail = self.svc.get_proposal(pid)["evaluation"]["votes"]
        self.assertEqual((detail["favor"], detail["abstain"], detail["counted"]),
                         (1, 1, 1))  # 弃权不计入分母计数

        r = self.svc.cast_stance(pid, institution_id=c, representative_id=rc,
                                 vote="oppose")
        # 有效表态达到 3 所，但支持 1 < 反对 1? 实际 favor=1 oppose=1，未过半数
        self.assertFalse(r["evaluation"]["effective"])
        self.assertTrue(any("未过半数" in x for x in r["evaluation"]["reasons"]))

        r = self.svc.cast_stance(pid, institution_id=d, representative_id=rd,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])
        self.assertEqual(self.svc.get_proposal(pid)["proposal"]["status"],
                         "effective")

    def test_unanimous_with_one_conflicted_school(self):
        # 3 所院校、法定人数 2，其中 1 所存在利益冲突：分母只剩 2 所。
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=3, with_condition=False)
        (a, ra), (b, rb), (c, rc) = schools

        self.svc.declare_conflict(pid, c, reason="该校承担相关采购")
        detail = self.svc.get_proposal(pid)["evaluation"]
        self.assertEqual(detail["eligible_institutions"], 2)
        self.assertIn(c, detail["conflicted_institutions"])

        # 冲突院校表态被拒绝
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=c, representative_id=rc,
                                 vote="favor")

        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        r = self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])

    def test_conflict_declaration_recomputes_open_proposal(self):
        # 2 所院校法定人数 2：两票支持即生效前，先登记一所冲突，
        # 使另一所单独支持无法达到法定人数。
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        r = self.svc.declare_conflict(pid, b, reason="评委亲属在校")
        # 合格院校只剩 1 所，法定人数要求被钳为 1，单票即过半数——仍不能生效
        # （quorum_required 保留为 2，分母不足时规则必须保守失败）
        self.assertFalse(r["evaluation"]["effective"])
        self.assertTrue(
            any("法定人数不足" in x for x in r["evaluation"]["reasons"]))


class ConditionTests(ServiceTestCase):
    def test_required_condition_blocks_and_unblocks(self):
        pid, schools, cond_id = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=True)
        (a, ra), (b, rb) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        r = self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                                 vote="favor")
        self.assertFalse(r["evaluation"]["effective"])
        pending = r["evaluation"]["pending_required_conditions"]
        self.assertEqual([c["seq"] for c in pending], [1])

        r = self.svc.satisfy_condition(
            cond_id, acting_institution_id=b, representative_id=rb)
        self.assertTrue(r["evaluation"]["effective"])
        self.assertEqual(r["condition"]["status"], "met")

    def test_advisory_condition_does_not_block(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        self.svc.add_condition(
            pid, "会后提交纪要模板", kind=COND_CLASS_ADVISORY,
            acting_institution_id=a, representative_id=ra)
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        r = self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])
        self.assertEqual(
            len(r["evaluation"]["pending_advisory_conditions"]), 1)

    def test_double_satisfy_is_conflict(self):
        pid, schools, cond_id = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=True)
        a, ra = schools[0]
        self.svc.satisfy_condition(
            cond_id, acting_institution_id=a, representative_id=ra)
        with self.assertRaises(ConflictError):
            self.svc.satisfy_condition(
                cond_id, acting_institution_id=a, representative_id=ra)

    def test_waive_condition_with_reason_unblocks(self):
        pid, schools, cond_id = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=True)
        (a, ra), (b, rb) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="favor")
        r = self.svc.waive_condition(
            cond_id, waiver="该前置审批已并入校级流程",
            acting_institution_id=a, representative_id=ra)
        self.assertTrue(r["evaluation"]["effective"])
        self.assertEqual(r["condition"]["status"], "waived")


class WithdrawRecomputeTests(ServiceTestCase):
    def test_withdraw_recomputes_not_yet_effective(self):
        # 必备条件尚未满足，议题处于 open：撤回导致法定人数不足。
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=3, with_condition=True)
        (a, ra), (b, rb), _ = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="favor")
        r = self.svc.withdraw_stance(
            pid, institution_id=b, representative_id=rb, reason="需补充授权")
        self.assertEqual(r["status"], "withdrawn")
        self.assertFalse(r["evaluation"]["effective"])
        self.assertEqual(r["evaluation"]["votes"]["counted"], 1)
        self.assertTrue(
            any("法定人数不足" in x for x in r["evaluation"]["reasons"]))
        # 议题仍在表决中，条件满足后仍因人数不足不生效
        cond_id = self.svc.get_proposal(pid)["conditions"][0]["id"]
        r = self.svc.satisfy_condition(
            cond_id, acting_institution_id=a, representative_id=ra)
        self.assertFalse(r["evaluation"]["effective"])

    def test_withdraw_without_active_stance_conflicts(self):
        pid, schools, _ = self.make_pending_proposal(quorum=1, n_schools=1)
        a, ra = schools[0]
        with self.assertRaises(ConflictError):
            self.svc.withdraw_stance(pid, institution_id=a,
                                     representative_id=ra)

    def test_withdraw_after_effective_requires_revision(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="favor")
        with self.assertRaises(StateError) as ctx:
            self.svc.withdraw_stance(pid, institution_id=b,
                                     representative_id=rb)
        self.assertIn("修订", str(ctx.exception))
        # 生效地位不变
        self.assertEqual(self.svc.get_proposal(pid)["proposal"]["status"],
                         "effective")

    def test_change_own_stance_is_allowed_others_are_not(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="oppose")
        r = self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote="favor")
        self.assertTrue(r["changed"])
        # B 的代表不能替 A 操作
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=a, representative_id=rb,
                                 vote="oppose")


class CancelTests(ServiceTestCase):
    def test_cancel_open_proposal_blocks_further_writes(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        self.svc.cancel_proposal(pid)
        with self.assertRaises(StateError):
            self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote="favor")
        # 列表状态过滤可用
        self.assertEqual(
            self.svc.list_proposals(status="cancelled")["proposals"][0]["id"],
            pid)
        self.assertEqual(
            self.svc.list_proposals(status="open")["proposals"], [])

    def test_cancel_effective_rejected(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=False)
        a, ra = schools[0]
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        with self.assertRaises(StateError):
            self.svc.cancel_proposal(pid)
        # 向已作废议题投票也应被拒绝
        b, rb = self.seed_institution("Z")
        pid2 = self.svc.submit_proposal(
            "待作废", "正文", quorum_required=1,
            author_institution_id=b)["proposal_id"]
        self.svc.cancel_proposal(pid2)
        with self.assertRaises(StateError):
            self.svc.cast_stance(pid2, institution_id=b,
                                 representative_id=rb, vote="favor")
