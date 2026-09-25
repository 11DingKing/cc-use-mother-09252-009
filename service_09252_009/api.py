"""HTTP 接口边界（仅依赖标准库）。

路由：
POST   /institutions
POST   /institutions/{code}/delegate
POST   /issues
GET    /issues/{id}
POST   /issues/{id}/proposals
POST   /issues/{id}/conditions
POST   /issues/{id}/conditions/{code}/resolve
POST   /issues/{id}/conflicts
POST   /issues/{id}/votes
DELETE /issues/{id}/votes/{institution}
POST   /issues/{id}/signatures
POST   /issues/{id}/revisions
GET    /issues/{id}/rationale/{institution}
GET    /issues/{id}/audit
GET    /health

幂等：请求头 Idempotency-Key（或 JSON 字段 idempotency_key）。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .clock import SystemClock
from .errors import ResolutionError
from .service import ResolutionService
from .storage import SQLiteRepository


def _json_default(obj: Any) -> str:
    from datetime import datetime

    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def default_db_path() -> Path:
    env = os.environ.get("RESOLUTION_DB")
    if env:
        return Path(env)
    return Path.home() / ".resolution_service" / "resolution.db"


def build_service(db_path: str | Path | None = None) -> ResolutionService:
    repo = SQLiteRepository(db_path or default_db_path())
    return ResolutionService(repo, SystemClock())


class _Handler(BaseHTTPRequestHandler):
    server_version = "ResolutionService/1.0"

    @property
    def service(self) -> ResolutionService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静输出
        if os.environ.get("RESOLUTION_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------
    def _send_json(self, status: int, body: dict[str, Any] | list[Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ResolutionError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ResolutionError("请求体必须是 JSON 对象")
        return parsed

    def _idempotency_key(self, body: dict[str, Any]) -> str | None:
        return self.headers.get("Idempotency-Key") or body.get("idempotency_key")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ------------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        try:
            body = self._read_json() if method in ("POST", "PUT") else {}
            key = self._idempotency_key(body)
            svc = self.service

            if method == "GET" and parts == ["health"]:
                self._send_json(200, {"status": "ok"})
                return

            if method == "POST" and parts == ["institutions"]:
                self._ok(svc.register_institution(
                    body["code"], body["name"],
                    body.get("delegate"), body.get("is_member", True)))
                return

            if method == "POST" and len(parts) == 3 and parts[0] == "institutions" and parts[2] == "delegate":
                self._ok(svc.replace_delegate(parts[1], body["new_delegate"]))
                return

            if method == "POST" and parts == ["issues"]:
                self._ok(svc.create_issue(
                    title=body["title"], body=body["body"],
                    author_institution=body["author_institution"],
                    deadline_local=body["deadline_local"],
                    tzname=body["tzname"],
                    language=body.get("language", "zh"),
                    summary=body.get("summary", ""),
                    quorum_ratio=float(body.get("quorum_ratio", 2 / 3)),
                    signature_ratio=float(body.get("signature_ratio", 1.0)),
                    parent_issue_id=body.get("parent_issue_id"),
                    idempotency_key=key))
                return

            if method == "GET" and parts == ["issues"]:
                self._send_json(200, {"issues": svc.list_issues()})
                return

            if len(parts) == 2 and parts[0] == "issues":
                issue_id = parts[1]
                if method == "GET":
                    self._ok(svc.get_status(issue_id))
                    return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "proposals":
                if method == "POST":
                    self._ok(svc.submit_proposal_revision(
                        issue_id=parts[1], institution=body["institution"],
                        body=body["body"], title=body.get("title"),
                        language=body.get("language", "zh"),
                        summary=body.get("summary", ""),
                        idempotency_key=key))
                    return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "conditions":
                if method == "POST":
                    self._ok(svc.attach_condition(
                        issue_id=parts[1], institution=body["institution"],
                        condition_code=body["condition_code"],
                        description=body["description"],
                        idempotency_key=key))
                    return

            if (len(parts) == 5 and parts[0] == "issues"
                    and parts[2] == "conditions" and parts[4] == "resolve"):
                if method == "POST":
                    self._ok(svc.resolve_condition(
                        issue_id=parts[1], institution=body["institution"],
                        condition_code=body.get("condition_code", parts[3]),
                        fulfilled=bool(body["fulfilled"]),
                        note=body.get("note", ""),
                        idempotency_key=key))
                    return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "conflicts":
                if method == "POST":
                    self._ok(svc.declare_conflict(
                        issue_id=parts[1], institution=body["institution"],
                        declared=bool(body["declared"]),
                        reason=body.get("reason")))
                    return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "votes":
                if method == "POST":
                    self._ok(svc.cast_vote(
                        issue_id=parts[1], institution=body["institution"],
                        stance=body["stance"], rationale=body.get("rationale", ""),
                        idempotency_key=key))
                    return

            if (len(parts) == 4 and parts[0] == "issues"
                    and parts[2] == "votes" and method == "DELETE"):
                self._ok(svc.withdraw_vote(parts[1], parts[3]))
                return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "signatures":
                if method == "POST":
                    self._ok(svc.sign(
                        issue_id=parts[1], institution=body["institution"],
                        signer=body.get("signer"), idempotency_key=key))
                    return

            if len(parts) == 3 and parts[0] == "issues" and parts[2] == "revisions":
                if method == "POST":
                    self._ok(svc.request_revision(
                        issue_id=parts[1], institution=body["institution"],
                        reason=body["reason"],
                        new_deadline_local=body.get("new_deadline_local"),
                        new_tzname=body.get("new_tzname"),
                        idempotency_key=key))
                    return

            if (len(parts) == 4 and parts[0] == "issues"
                    and parts[2] == "rationale" and method == "GET"):
                self._ok(svc.get_rationale(parts[1], parts[3]))
                return

            if (len(parts) == 3 and parts[0] == "issues"
                    and parts[2] == "audit" and method == "GET"):
                self._send_json(200, {"events": svc.list_audit(parts[1])})
                return

            self._send_json(404, {"error": {"code": "not_found", "message": "未知路由"}})
        except KeyError as exc:
            self._send_json(422, {"error": {"code": "validation_error",
                                            "message": f"缺少必填字段: {exc.args[0]}"}})
        except ResolutionError as exc:
            self._send_json(exc.http_status,
                            {"error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def _ok(self, result: Any) -> None:
        self._send_json(200, result)


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  db_path: str | Path | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = build_service(db_path)  # type: ignore[attr-defined]
    return server


def main() -> None:
    host = os.environ.get("RESOLUTION_HOST", "127.0.0.1")
    port = int(os.environ.get("RESOLUTION_PORT", "8080"))
    server = create_server(host, port)
    print(f"决议服务监听 http://{host}:{port}  数据库: {default_db_path()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
