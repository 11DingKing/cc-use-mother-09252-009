"""并发投票：多线程同时表态/提交，事务串行化，结果一致无重复。"""

import threading
from concurrent.futures import ThreadPoolExecutor

from service_09252_009.errors import AuthorizationError

from support import FileStoreTestCase


class ConcurrentVoteTests(FileStoreTestCase):
    def test_concurrent_stances_from_distinct_schools(self):
        n = 8
        schools = [self.seed_institution(f"S{i:02d}") for i in range(n)]
        pid = self.svc.submit_proposal(
            "并行表决议题", "正文", quorum_required=n,
            author_institution_id=schools[0][0])["proposal_id"]

        errors: list[Exception] = []

        def vote(args):
            inst, rep = args
            try:
                return self.svc.cast_stance(
                    pid, institution_id=inst, representative_id=rep,
                    vote="favor", rationale=f"校{inst}支持")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return None

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(vote, schools))

        self.assertEqual(errors, [])
        detail = self.svc.get_proposal(pid)
        self.assertEqual(len(detail["stances"]), n)
        self.assertEqual(detail["proposal"]["status"], "effective")
        self.assertEqual(detail["evaluation"]["votes"]["favor"], n)
        # 恰好最后完成的那张票触发生效
        self.assertEqual(
            sum(1 for r in results if r is not None and r["evaluation"]["effective"]),
            1)

    def test_concurrent_stances_same_school_single_row(self):
        # 同一院校多线程并发以同一代表投票：UNIQUE 约束 + upsert 保证只有一行。
        # 带必备条件使议题保持 open，投票线程不会因提前生效而被拒。
        schools = [self.seed_institution(f"S{i:02d}") for i in range(3)]
        a, ra = schools[0]
        pid = self.svc.submit_proposal(
            "同校并发", "正文", quorum_required=1,
            author_institution_id=a)["proposal_id"]
        self.svc.add_condition(
            pid, "会后补交材料", acting_institution_id=a,
            representative_id=ra)
        barrier = threading.Barrier(3)

        def cast(vote_value):
            barrier.wait()
            self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote=vote_value)

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(cast, ["favor", "abstain", "oppose"]))

        stances = self.svc.get_proposal(pid)["stances"]
        self.assertEqual(len(stances), 1)
        self.assertEqual(stances[0]["status"], "active")

    def test_concurrent_proposal_submissions_get_distinct_ids(self):
        a, _ = self.seed_institution("ORG")
        ids: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def submit(i):
            barrier.wait()
            r = self.svc.submit_proposal(
                f"议题{i}", "正文", quorum_required=1,
                author_institution_id=a, idempotency_key=f"key-{i}")
            with lock:
                ids.append(r["proposal_id"])

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(submit, range(6)))

        self.assertEqual(sorted(ids), sorted(set(ids)))
        self.assertEqual(len(ids), 6)

    def test_concurrent_idempotent_replays_share_one_outcome(self):
        a, ra = self.seed_institution("ORG")
        barrier = threading.Barrier(4)
        outcomes: list[dict] = []
        lock = threading.Lock()

        def submit():
            barrier.wait()
            r = self.svc.submit_proposal(
                "同一幂等议题", "正文", quorum_required=1,
                author_institution_id=a, idempotency_key="same-key")
            with lock:
                outcomes.append(r)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: submit(), range(4)))

        self.assertEqual(len({o["proposal_id"] for o in outcomes}), 1)
        self.assertEqual(len(self.svc.list_proposals()["proposals"]), 1)

    def test_concurrent_conflict_and_vote_never_produces_effective(self):
        # 线程 1 登记 B 校利益冲突，线程 2 替 B 校投票；
        # 3 所院校法定 3 所且 C 始终不投票，议题始终 open，冲突登记总能进行。
        # 无论投票与登记谁先，最终状态必须自洽：冲突校绝不可能以支持票促成生效。
        schools = [self.seed_institution(f"S{i:02d}") for i in range(3)]
        (a, ra), (b, rb), _ = schools
        pid = self.svc.submit_proposal(
            "竞争事务", "正文", quorum_required=3,
            author_institution_id=a)["proposal_id"]

        def vote_a():
            self.svc.cast_stance(pid, institution_id=a, representative_id=ra,
                                 vote="favor")

        def vote_b():
            try:
                self.svc.cast_stance(pid, institution_id=b,
                                     representative_id=rb, vote="favor")
                return "voted"
            except AuthorizationError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=3) as pool:
            f1 = pool.submit(vote_a)
            f2 = pool.submit(vote_b)
            f3 = pool.submit(self.svc.declare_conflict, pid, b, "亲属关系")
            f1.result(); outcome_b = f2.result(); f3.result()

        detail = self.svc.get_proposal(pid)
        self.assertEqual(detail["proposal"]["status"], "open")
        self.assertFalse(detail["evaluation"]["effective"])
        self.assertEqual(detail["evaluation"]["conflicted_institutions"], [b])
        if outcome_b == "voted":
            # 立场记录可能存在，但评估计数必须排除冲突校：只计 A 一票
            self.assertEqual(detail["evaluation"]["votes"]["favor"], 1)
            # 冲突登记后该校再投票必然被拒
            with self.assertRaises(AuthorizationError):
                self.svc.cast_stance(pid, institution_id=b,
                                     representative_id=rb, vote="favor")
