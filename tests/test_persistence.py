"""SQLite 重启后保持顺序与状态：编号、版本号、审计序号均单调且不复用。"""

from support import FileStoreTestCase


class PersistenceOrderTests(FileStoreTestCase):
    def test_state_and_order_survive_restart(self):
        a, ra = self.seed_institution("A")
        b, rb = self.seed_institution("B")

        p1 = self.svc.submit_proposal(
            "第一议题", "正文", quorum_required=2,
            author_institution_id=a)["proposal_id"]
        p2 = self.svc.submit_proposal(
            "第二议题", "正文", quorum_required=1,
            author_institution_id=a)["proposal_id"]
        self.assertEqual((p1, p2), (1, 2))

        self.svc.cast_stance(p1, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(p1, institution_id=b, representative_id=rb,
                             vote="favor")
        self.assertEqual(self.svc.get_proposal(p1)["proposal"]["status"],
                         "effective")

        # 修订产生 v2
        self.svc.revise_proposal(
            p1, "第一议题（修订）", "新正文",
            requesting_institution_id=a, representative_id=ra, reason="更新")
        last_audit_seq_before = self.svc.get_audit()["audit"][0]["seq"]

        self.reopen()

        # 状态、立场、版本全部保留
        d1 = self.svc.get_proposal(p1)
        self.assertEqual(d1["proposal"]["version"], 2)
        self.assertEqual(d1["proposal"]["status"], "open")
        self.assertEqual(self.svc.get_proposal(p1, version=1)["proposal"]["status"],
                         "superseded")
        self.assertEqual(self.svc.get_proposal(p2)["proposal"]["status"], "open")
        self.assertEqual(len(self.svc.get_proposal(p1, version=1)["stances"]), 2)

        # 新议题编号严格延续，不复用
        p3 = self.svc.submit_proposal(
            "第三议题", "正文", quorum_required=1,
            author_institution_id=a)["proposal_id"]
        self.assertEqual(p3, 3)

        # 新版本号延续：v2 法定人数仍为 2，两校支持后生效，再修订出 v3
        self.svc.cast_stance(p1, institution_id=a, representative_id=ra,
                             vote="favor")
        self.svc.cast_stance(p1, institution_id=b, representative_id=rb,
                             vote="favor")
        self.assertEqual(self.svc.get_proposal(p1)["proposal"]["status"],
                         "effective")
        r2 = self.svc.revise_proposal(
            p1, "第一议题（再修订）", "正文",
            requesting_institution_id=a, representative_id=ra, reason="再更新")
        self.assertEqual(r2["version"], 3)

        # 审计日志在重启后继续单调递增
        audit = self.svc.get_audit()["audit"]
        self.assertGreater(audit[0]["seq"], last_audit_seq_before)
        seqs = [row["seq"] for row in audit]
        self.assertEqual(seqs, sorted(seqs, reverse=True))

    def test_wal_file_created_for_file_db(self):
        # 文件库启用 WAL（-wal/-shm 可能在检查点后消失，只验证 journal_mode）
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")
