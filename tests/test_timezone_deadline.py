"""时区截止：本地时间 + IANA 时区折算 UTC，截止时刻边界可操作。"""

from datetime import datetime, timezone

from service_09252_009.errors import DeadlinePassedError
from service_09252_009.models import iso

from support import ServiceTestCase


class DeadlineTests(ServiceTestCase):
    def _proposal_open_at_boundary(self):
        # 上海时间 2026-09-01 18:00 == UTC 10:00
        pid, schools, _ = self.make_pending_proposal(
            quorum=1, n_schools=1, with_condition=False,
            deadline="2026-09-01T18:00:00", deadline_tz="Asia/Shanghai")
        return pid, schools[0]

    def test_deadline_is_normalized_to_utc(self):
        pid, _ = self._proposal_open_at_boundary()
        p = self.svc.get_proposal(pid)["proposal"]
        self.assertEqual(p["deadline"], "2026-09-01T10:00:00Z")

    def test_vote_allowed_before_and_exactly_at_deadline(self):
        pid, (a, ra) = self._proposal_open_at_boundary()
        self.clock.set(datetime(2026, 9, 1, 9, 59, 59, tzinfo=timezone.utc))
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        self.assertEqual(self.svc.get_proposal(pid)["proposal"]["status"],
                         "effective")

    def test_open_proposal_blocks_writes_after_deadline(self):
        pid, (a, ra) = self._proposal_open_at_boundary()
        # 第二所院校也建档但不投票，制造 open 议题
        b, rb = self.seed_institution("B")
        # 重新提交一个法定人数 2 的议题，保证截止时仍 open
        pid2 = self.svc.submit_proposal(
            "截止议题", "正文", quorum_required=2,
            author_institution_id=a,
            deadline="2026-09-01T18:00:00",
            deadline_timezone="Asia/Shanghai")["proposal_id"]
        self.svc.cast_stance(pid2, institution_id=a, representative_id=ra,
                             vote="favor")

        # 恰好截止时刻（含）仍可操作
        self.clock.set(datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc))
        r = self.svc.cast_stance(pid2, institution_id=b, representative_id=rb,
                                 vote="favor")
        self.assertTrue(r["evaluation"]["effective"])

        # 新议题：过截止一秒后拒绝
        pid3 = self.svc.submit_proposal(
            "逾期议题", "正文", quorum_required=1, author_institution_id=a,
            deadline="2026-09-01T18:00:00",
            deadline_timezone="Asia/Shanghai")["proposal_id"]
        self.clock.set(datetime(2026, 9, 1, 10, 0, 1, tzinfo=timezone.utc))
        with self.assertRaises(DeadlinePassedError) as ctx:
            self.svc.cast_stance(pid3, institution_id=a,
                                 representative_id=ra, vote="favor")
        self.assertEqual(ctx.exception.code, "deadline_passed")
        eval_ = self.svc.get_proposal(pid3)["evaluation"]
        self.assertFalse(eval_["deadline_open"])
        self.assertTrue(any("截止" in r for r in eval_["reasons"]))

    def test_effective_resolution_remains_in_force_after_deadline(self):
        pid, (a, ra) = self._proposal_open_at_boundary()
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
        # 越过截止时刻后查询：决议地位保持
        self.clock.set(datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc))
        detail = self.svc.get_proposal(pid)
        self.assertEqual(detail["proposal"]["status"], "effective")
        self.assertTrue(detail["evaluation"]["effective"])
        self.assertIsNotNone(detail["proposal"]["effective_at"])

    def test_aware_deadline_string_takes_precedence_over_tz_param(self):
        a, _ = self.seed_institution("A")
        # 自带 +09:00 偏移（东京 19:00 == UTC 10:00），即便给了上海时区也以串为准
        pid = self.svc.submit_proposal(
            "显式偏移", "正文", quorum_required=1, author_institution_id=a,
            deadline="2026-09-01T19:00:00+09:00",
            deadline_timezone="Asia/Shanghai")["proposal_id"]
        self.assertEqual(
            self.svc.get_proposal(pid)["proposal"]["deadline"],
            "2026-09-01T10:00:00Z")

    def test_unknown_timezone_rejected(self):
        a, _ = self.seed_institution("A")
        from service_09252_009.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.submit_proposal(
                "t", "b", quorum_required=1, author_institution_id=a,
                deadline="2026-09-01T18:00:00",
                deadline_timezone="Mars/Olympus")

    def test_negative_timezone_offset_example(self):
        # 纽约落后 UTC：纽约 9/1 06:00（EDT, -04:00）== UTC 10:00
        a, ra = self.seed_institution("A")
        pid = self.svc.submit_proposal(
            "跨洲会议", "正文", quorum_required=1, author_institution_id=a,
            deadline="2026-09-01T06:00:00",
            deadline_timezone="America/New_York")["proposal_id"]
        self.assertEqual(
            self.svc.get_proposal(pid)["proposal"]["deadline"],
            "2026-09-01T10:00:00Z")
        self.clock.set(datetime(2026, 9, 1, 9, 59, tzinfo=timezone.utc))
        self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                             vote="favor")
