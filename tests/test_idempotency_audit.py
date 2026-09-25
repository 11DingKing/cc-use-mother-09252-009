"""幂等性与审计日志。"""

from support import ServiceTestCase


class IdempotencyTests(ServiceTestCase):
    def test_repeated_submit_same_key_returns_first_result(self):
        a, ra = self.seed_institution("A")
        kwargs = dict(
            title="同一议题", body="正文", quorum_required=1,
            author_institution_id=a, idempotency_key="submit-1")
        r1 = self.svc.submit_proposal(**kwargs)
        r2 = self.svc.submit_proposal(**kwargs)
        self.assertEqual(r1, r2)
        # 只产生了一个议题编号
        self.assertEqual(len(self.svc.list_proposals()["proposals"]), 1)

    def test_repeated_vote_same_key_no_duplicate_stance(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        a, ra = schools[0]
        kwargs = dict(proposal_id=pid, institution_id=a,
                      representative_id=ra, vote="favor",
                      rationale="支持", idempotency_key="vote-1")
        r1 = self.svc.cast_stance(**kwargs)
        # 即便重试时带了不同参数，也回放首次结果
        r2 = self.svc.cast_stance(**{**kwargs, "vote": "oppose"})
        self.assertEqual(r1, r2)
        stances = self.svc.get_proposal(pid)["stances"]
        self.assertEqual(len(stances), 1)
        self.assertEqual(stances[0]["vote"], "favor")

    def test_different_keys_apply_changes(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=False)
        a, ra = schools[0]
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="oppose", idempotency_key="k-a")
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor", idempotency_key="k-b")
        stances = self.svc.get_proposal(pid)["stances"]
        self.assertEqual(len(stances), 1)
        self.assertEqual(stances[0]["vote"], "favor")


class AuditTests(ServiceTestCase):
    def test_every_write_is_audited_with_actor_and_order(self):
        a, ra = self.seed_institution("A")
        b, rb = self.seed_institution("B")
        pid = self.svc.submit_proposal(
            "议题", "正文", quorum_required=2, author_institution_id=a,
            idempotency_key="p1")["proposal_id"]
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor", idempotency_key="v1")
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="favor", idempotency_key="v2")

        audit = self.svc.get_audit(proposal_id=pid)["audit"]
        actions = [row["action"] for row in reversed(audit)]
        self.assertIn("proposal.submit", actions)
        self.assertEqual(actions.count("stance.cast"), 2)

        # 演员与目标都可追溯
        casts = [r for r in audit if r["action"] == "stance.cast"]
        self.assertEqual({r["actor_institution_id"] for r in casts}, {a, b})
        self.assertTrue(all(r["target_id"].startswith(f"{pid}:v") for r in casts))

        # seq 严格单调（返回顺序为倒序）
        seqs = [r["seq"] for r in audit]
        self.assertEqual(seqs, sorted(seqs, reverse=True))

    def test_replay_is_marked_in_audit(self):
        a, ra = self.seed_institution("A")
        self.svc.submit_proposal("t", "b", quorum_required=1,
                                 author_institution_id=a, idempotency_key="x")
        self.svc.submit_proposal("t", "b", quorum_required=1,
                                 author_institution_id=a, idempotency_key="x")
        actions = [r["action"] for r in self.svc.get_audit()["audit"]]
        self.assertIn("proposal.submit.replay", actions)
