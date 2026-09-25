"""代表更换：旧代表失效，立场随院校保留并由新代表继承。"""

from service_09252_009.errors import AuthorizationError

from support import ServiceTestCase


class RepresentativeReplacementTests(ServiceTestCase):
    def test_old_representative_rejected_after_replacement(self):
        # 带必备条件使议题保持 open，从而更换代表后仍可改投
        pid, schools, _ = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=True)
        a, old = schools[0]
        self.svc.cast_stance(pid, institution_id=a, representative_id=old,
                             vote="favor")

        new = self.svc.appoint_representative(
            a, "新代表", replace=True)["representative"]["id"]
        self.assertNotEqual(old, new)

        # 旧代表令牌立即失效
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=a,
                                 representative_id=old, vote="oppose")

        # 院校立场仍在（继承），新代表可以查看并修改
        detail = self.svc.get_proposal(pid)
        self.assertEqual(len(detail["stances"]), 1)
        self.assertEqual(detail["stances"][0]["vote"], "favor")
        self.assertEqual(detail["stances"][0]["status"], "active")

        r = self.svc.cast_stance(pid, institution_id=a,
                                 representative_id=new, vote="abstain",
                                 rationale="新代表重新评估后弃权")
        self.assertEqual(r["stance"]["representative_id"], new)
        stances = self.svc.get_proposal(pid)["stances"]
        self.assertEqual(len(stances), 1)  # 没有产生第二条立场
        self.assertEqual(stances[0]["vote"], "abstain")

    def test_representative_of_other_school_rejected(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=2, n_schools=2, with_condition=False)
        (a, ra), (b, rb) = schools
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=a, representative_id=rb,
                                 vote="favor")
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=b, representative_id=ra,
                                 vote="favor")

    def test_unknown_representative_rejected(self):
        pid, schools, _ = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=False)
        a, _ = schools[0]
        with self.assertRaises(AuthorizationError):
            self.svc.cast_stance(pid, institution_id=a,
                                 representative_id=99999, vote="favor")

    def test_replacement_mid_vote_keeps_institution_count(self):
        # 3 所院校、法定人数 3；B 在表决中途换代表，立场不丢。
        # 第三所院校未投票前议题保持 open，使新代表可以确认沿用立场。
        pid, schools, _ = self.make_pending_proposal(
            quorum=3, n_schools=3, with_condition=False)
        (a, ra), (b, rb), (c, rc) = schools
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb,
                             vote="favor")
        rb2 = self.svc.appoint_representative(
            b, "乙校继任者", replace=True)["representative"]["id"]
        # 新代表确认沿用立场（同样的票重投一次）
        self.svc.cast_stance(pid, institution_id=b, representative_id=rb2,
                             vote="favor")
        r = self.svc.cast_stance(pid, institution_id=c, representative_id=rc,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])
        self.assertEqual(r["evaluation"]["votes"]["counted"], 3)
