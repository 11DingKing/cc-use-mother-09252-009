"""修订流程：已生效决议只能通过新版本重新表决来改变。"""

from service_09252_009.errors import StateError, ValidationError

from support import ServiceTestCase


def _make_effective(svc, pid, schools, quorum):
    """让 quorum 所院校全部投支持，使无必备条件的议题生效。"""
    for inst, rep in schools[:quorum]:
        r = svc.cast_stance(pid, institution_id=inst, representative_id=rep,
                            vote="favor")
    return r


class RevisionTests(ServiceTestCase):
    def test_revision_supersedes_and_restarts_vote(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        _make_effective(self.svc, pid, schools, 2)
        self.assertEqual(self.svc.get_proposal(pid)["proposal"]["status"],
                         "effective")

        r = self.svc.revise_proposal(
            pid, "联合培养（修订稿）", "新正文",
            requesting_institution_id=a, representative_id=ra,
            reason="培养方案更新", idempotency_key="rev-1")
        self.assertEqual(r["version"], 2)
        self.assertEqual(r["status"], "open")
        self.assertEqual(r["superseded_version"], 1)

        latest = self.svc.get_proposal(pid)["proposal"]
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["status"], "open")
        self.assertEqual(latest["based_on_version"], 1)
        v1 = self.svc.get_proposal(pid, version=1)["proposal"]
        self.assertEqual(v1["status"], "superseded")
        self.assertIsNotNone(v1["superseded_at"])

        # v1 的表决与签署是历史，v2 从零开始
        detail = self.svc.get_proposal(pid)
        self.assertEqual(detail["stances"], [])
        self.assertEqual(detail["signatures"], [])
        revisions = detail["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["from_version"], 1)
        self.assertEqual(revisions[0]["new_version"], 2)
        self.assertEqual(revisions[0]["reason"], "培养方案更新")

    def test_cannot_revise_non_effective(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=True)
        a, ra = schools[0]
        with self.assertRaises(StateError):
            self.svc.revise_proposal(
                pid, "t", "b", requesting_institution_id=a,
                representative_id=ra)

    def test_v2_reaches_effectiveness_independently(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        _make_effective(self.svc, pid, schools, 2)
        self.svc.revise_proposal(
            pid, "v2", "正文", requesting_institution_id=a,
            representative_id=ra, reason="r")
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        r = self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                                 vote="oppose")
        self.assertFalse(r["evaluation"]["effective"])
        # B 改为支持后 v2 生效
        r = self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])

    def test_conditions_on_v1_do_not_leak_to_v2(self):
        pid, schools, cond_id = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=True)
        a, ra = schools[0]
        self.svc.satisfy_condition(
            cond_id, acting_institution_id=a, representative_id=ra)
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.revise_proposal(
            pid, "v2", "正文", requesting_institution_id=a,
            representative_id=ra, reason="r")
        detail = self.svc.get_proposal(pid)
        self.assertEqual(detail["conditions"], [])
        # 旧条件记录仍可按 v1 查到
        v1 = self.svc.get_proposal(pid, version=1)
        self.assertEqual(len(v1["conditions"]), 1)
        self.assertEqual(v1["conditions"][0]["status"], "met")

    def test_invalid_vote_rejected(self):
        pid, schools, _ = self.make_pending_proposal(quorum=1, n_schools=1)
        a, ra = schools[0]
        with self.assertRaises(ValidationError):
            self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote="maybe")
