"""HTTP 接口边界测试（标准库 ThreadingHTTPServer + urllib）。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from service_09252_009.api import create_server


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = tempfile.mktemp(suffix=".db")
        self.server = create_server("127.0.0.1", 0, self.db)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, body: dict | None = None,
                 idem_key: str | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _register(self) -> None:
        for code, name, delegate in [
            ("uni-a", "甲国联合大学", "Alice"),
            ("uni-b", "乙国理工学院", "Bruno"),
            ("uni-c", "丙国师范大学", "Chen"),
        ]:
            status, _ = self._request("POST", "/institutions",
                                      {"code": code, "name": name, "delegate": delegate})
            self.assertEqual(status, 200)

    def _create_issue(self, key: str | None = None) -> str:
        status, r = self._request(
            "POST", "/issues",
            {"title": "学分互认", "body": "承认 30 学分", "author_institution": "uni-a",
             "deadline_local": "2099-01-01 12:00", "tzname": "UTC"},
            idem_key=key,
        )
        self.assertEqual(status, 200, r)
        return r.get("issue_id") or r["ref"]

    def test_full_flow_over_http(self) -> None:
        self._register()
        iid = self._create_issue()

        status, _ = self._request(
            "POST", f"/issues/{iid}/conditions",
            {"institution": "uni-b", "condition_code": "transcript",
             "description": "需要英译成绩单"})
        self.assertEqual(status, 200)

        # 跨机构关闭条件 → 403
        status, err = self._request(
            "POST", f"/issues/{iid}/conditions/transcript/resolve",
            {"institution": "uni-c", "fulfilled": True})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "forbidden")

        status, _ = self._request(
            "POST", f"/issues/{iid}/conditions/transcript/resolve",
            {"institution": "uni-b", "fulfilled": True})
        self.assertEqual(status, 200)

        for inst in ("uni-a", "uni-b", "uni-c"):
            s, _ = self._request(
                "POST", f"/issues/{iid}/votes",
                {"institution": inst, "stance": "for", "rationale": "同意"})
            self.assertEqual(s, 200)
        for inst in ("uni-a", "uni-b", "uni-c"):
            s, _ = self._request(
                "POST", f"/issues/{iid}/signatures", {"institution": inst})
            self.assertEqual(s, 200)

        status, r = self._request("GET", f"/issues/{iid}")
        self.assertEqual(status, 200)
        self.assertEqual(r["issue"]["status"], "adopted")

        # 已生效后直接撤回 → 409，需修订
        status, err = self._request("DELETE", f"/issues/{iid}/votes/uni-b")
        self.assertEqual(status, 409)
        status, rev = self._request(
            "POST", f"/issues/{iid}/revisions",
            {"institution": "uni-b", "reason": "条款微调",
             "new_deadline_local": "2099-06-01 09:00", "new_tzname": "Europe/Berlin"})
        self.assertEqual(status, 200)
        self.assertEqual(rev["parent_status"], "under_revision")

        # 理由查询
        status, rationale = self._request(
            "GET", f"/issues/{iid}/rationale/uni-b")
        self.assertEqual(status, 200)
        self.assertEqual(rationale["positions"][0]["stance"], "for")
        self.assertEqual(rationale["conditions"][0]["code"], "transcript")

        # 审计顺序
        status, audit = self._request("GET", f"/issues/{iid}/audit")
        self.assertEqual(status, 200)
        seqs = [e["seq"] for e in audit["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertIn("issue_adopted", [e["event_type"] for e in audit["events"]])

    def test_idempotency_header_and_missing_field(self) -> None:
        self._register()
        iid1 = self._create_issue(key="stable-key")
        iid2 = self._create_issue(key="stable-key")
        self.assertEqual(iid1, iid2)

        status, err = self._request("POST", "/issues", {"title": "缺字段"})
        self.assertEqual(status, 422)
        self.assertIn("body", err["error"]["message"])

    def test_concurrent_votes_over_http(self) -> None:
        self._register()
        iid = self._create_issue()

        def vote(inst: str) -> int:
            status, _ = self._request(
                "POST", f"/issues/{iid}/votes",
                {"institution": inst, "stance": "for"})
            return status

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(vote, ["uni-a", "uni-b", "uni-c"]))
        self.assertEqual(statuses, [200, 200, 200])

        _, r = self._request("GET", f"/issues/{iid}")
        self.assertEqual(r["evaluation"]["voted_count"], 3)

    def test_health_and_unknown_route(self) -> None:
        status, r = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(r["status"], "ok")
        status, err = self._request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
