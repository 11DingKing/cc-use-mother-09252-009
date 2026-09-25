"""HTTP JSON API 端到端测试：鉴权头、幂等头、错误映射与完整协商流程。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

from service_09252_009.api import make_server
from service_09252_009.service import Clock, ResolutionService
from service_09252_009.storage import SQLiteStore


class FixedClock(Clock):
    def __init__(self):
        self.t = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def now(self):
        return self.t


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.store = SQLiteStore(":memory:")
        self.svc = ResolutionService(self.store, clock=self.clock)
        self.httpd = make_server(self.svc, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.store.close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url(path), data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ---- 基础 ----------------------------------------------------------

    def test_health(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_unknown_route_404(self):
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_validation_error_maps_to_422(self):
        status, body = self.request("POST", "/institutions",
                                    {"code": "", "name": ""})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_missing_identity_headers_401(self):
        status, _ = self.request("POST", "/proposals")  # 先建议题
        # 直接对不存在但同形状的路由：用 stances 缺少身份头
        status, body = self.request("POST", "/proposals/1/stances",
                                    {"vote": "favor"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "missing_identity")

    # ---- 完整流程 ------------------------------------------------------

    def _register_school(self, code, name):
        _, body = self.request("POST", "/institutions",
                               {"code": code, "name": name})
        inst = body["institution"]["id"]
        _, body = self.request(
            "POST", f"/institutions/{inst}/representatives",
            {"name": f"{name}代表"})
        return inst, body["representative"]["id"]

    def test_full_negotiation_over_http(self):
        a, ra = self._register_school("A", "甲校")
        b, rb = self._register_school("B", "乙校")
        auth_a = {"X-Institution-Id": str(a),
                  "X-Representative-Id": str(ra)}
        auth_b = {"X-Institution-Id": str(b),
                  "X-Representative-Id": str(rb)}

        status, body = self.request("POST", "/proposals", {
            "title": "跨国联合教研",
            "body": "课程互认方案",
            "quorum_required": 2,
            "author_institution_id": a,
            "deadline": "2026-09-01T18:00:00",
            "deadline_timezone": "Asia/Shanghai",
        }, headers={"Idempotency-Key": "submit"})
        self.assertEqual(status, 201)
        pid = body["proposal_id"]
        self.assertEqual(body["version"], 1)

        status, body = self.request(
            "POST", f"/proposals/{pid}/conditions",
            {"title": "签署数据共享附录", "kind": "required"},
            headers=auth_a)
        self.assertEqual(status, 201)

        status, body = self.request(
            "POST", f"/proposals/{pid}/stances",
            {"vote": "favor", "rationale": "原则同意，待附录签署"},
            headers=auth_a)
        self.assertEqual(status, 200)
        self.assertFalse(body["evaluation"]["effective"])

        # B 校跨校身份被拒
        status, body = self.request(
            "POST", f"/proposals/{pid}/stances",
            {"vote": "favor"},
            headers={"X-Institution-Id": str(a),
                     "X-Representative-Id": str(rb)})
        self.assertEqual(status, 403)

        status, body = self.request(
            "POST", f"/proposals/{pid}/stances",
            {"vote": "favor", "rationale": "同意"},
            headers=auth_b)
        self.assertFalse(body["evaluation"]["effective"])
        self.assertEqual(
            body["evaluation"]["pending_required_conditions"][0]["seq"], 1)

        # 查询理由
        status, body = self.request(
            "GET", f"/proposals/{pid}/rationale?institution_id={b}")
        self.assertEqual(body["stances"][0]["rationale"], "同意")

        # 条件满足后生效
        conds = self.request("GET", f"/proposals/{pid}")[1]["conditions"]
        status, body = self.request(
            "POST", f"/conditions/{conds[0]['id']}/satisfy", {},
            headers=auth_b)
        self.assertTrue(body["evaluation"]["effective"])

        # 签署
        status, body = self.request(
            "POST", f"/proposals/{pid}/signatures",
            {"note": "甲校签署"}, headers=auth_a)
        self.assertEqual(status, 201)
        status, body = self.request(
            "POST", f"/proposals/{pid}/signatures",
            {"note": "乙校签署"}, headers=auth_b)
        self.assertEqual(status, 201)

        # 撤回被拒，引导修订
        status, body = self.request(
            "POST", f"/proposals/{pid}/stances/withdraw", {},
            headers=auth_b)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_state")

        # 修订 v2
        status, body = self.request(
            "POST", f"/proposals/{pid}/revisions",
            {"title": "跨国联合教研 v2", "body": "更新方案",
             "reason": "附录升级"}, headers=auth_a)
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["status"], "open")

        # 审计可查
        status, body = self.request("GET", f"/proposals/{pid}/audit")
        self.assertEqual(status, 200)
        actions = {row["action"] for row in body["audit"]}
        self.assertIn("proposal.revise", actions)
        self.assertIn("resolution.sign", actions)

    def test_idempotency_key_replays_over_http(self):
        a, _ = self._register_school("A", "甲校")
        headers = {"Idempotency-Key": "abc-123"}
        s1, b1 = self.request("POST", "/proposals", {
            "title": "幂等议题", "body": "x", "quorum_required": 1,
            "author_institution_id": a}, headers=headers)
        s2, b2 = self.request("POST", "/proposals", {
            "title": "幂等议题", "body": "x", "quorum_required": 1,
            "author_institution_id": a}, headers=headers)
        self.assertEqual((s1, b1), (201, b2))

    def test_concurrent_http_votes(self):
        schools = [self._register_school(f"S{i}", f"校{i}") for i in range(5)]
        _, body = self.request("POST", "/proposals", {
            "title": "HTTP 并行", "body": "x",
            "quorum_required": 5,
            "author_institution_id": schools[0][0]})
        pid = body["proposal_id"]

        import concurrent.futures

        def vote(pair):
            inst, rep = pair
            return self.request(
                "POST", f"/proposals/{pid}/stances", {"vote": "favor"},
                headers={"X-Institution-Id": str(inst),
                         "X-Representative-Id": str(rep)})

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(vote, schools))
        self.assertTrue(all(s == 200 for s, _ in results))
        _, body = self.request("GET", f"/proposals/{pid}")
        self.assertEqual(body["proposal"]["status"], "effective")


if __name__ == "__main__":
    unittest.main()
