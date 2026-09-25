"""HTTP JSON API（仅标准库实现）。

路由约定
--------

- 机构/代表/议题管理（会议秘书处视角）：
    POST   /institutions                              注册院校
    POST   /institutions/{id}/representatives         任命代表
    POST   /institutions/{id}/representatives/replace 更换代表
    POST   /proposals                                 提交议题（v1）
    POST   /proposals/{id}/cancel                     作废表决中的议题
    POST   /proposals/{id}/conflicts                  登记利益冲突
- 院校代表操作（需 X-Institution-Id 与 X-Representative-Id 头）：
    POST   /proposals/{id}/conditions                 附加条件
    POST   /conditions/{id}/satisfy                   确认条件满足
    POST   /conditions/{id}/waive                     解除条件
    POST   /proposals/{id}/stances                    表态（可重复修改本校立场）
    POST   /proposals/{id}/stances/withdraw           撤回表态
    POST   /proposals/{id}/signatures                 签署生效决议
    POST   /proposals/{id}/revisions                  对已生效决议发起修订
- 查询（无需鉴权）：
    GET    /proposals                                 列表（?status=）
    GET    /proposals/{id}                            状态与评估（?version=）
    GET    /proposals/{id}/rationale                  各院校表态与理由
    GET    /proposals/{id}/audit                      审计日志
    GET    /healthz

幂等：写操作接受 ``Idempotency-Key`` 请求头；重试返回首次结果。
错误统一为 ``{"error": {"code", "message"}}``，状态码由错误类型决定。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import ResolutionService


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    # sort_keys：首次响应与幂等回放响应字节一致；同时输出稳定便于审计比对。
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, exc: DomainError) -> None:
    _json(handler, exc.status, {"error": exc.to_dict()})


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("请求体必须是合法 JSON")
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return data


def _int_path(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValidationError(f"路径参数 {field} 必须是整数: {value!r}")


def _opt_int(value, field: str):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是整数")


def _actor(handler: BaseHTTPRequestHandler) -> tuple[int, int]:
    """从请求头读取院校与代表身份。"""
    inst = handler.headers.get("X-Institution-Id")
    rep = handler.headers.get("X-Representative-Id")
    if not inst or not rep:
        raise ValidationError(
            "缺少身份头：需要 X-Institution-Id 与 X-Representative-Id",
            code="missing_identity", status=401)
    try:
        return int(inst), int(rep)
    except ValueError:
        raise ValidationError("身份头必须是整数 id", code="bad_identity", status=401)


def _idem_key(handler: BaseHTTPRequestHandler) -> Optional[str]:
    key = handler.headers.get("Idempotency-Key")
    return key.strip() if key and key.strip() else None


class ApiHandler(BaseHTTPRequestHandler):
    """请求分发。service 由类属性注入。"""

    service: ResolutionService = None  # type: ignore[assignment]
    server_version = "ResolutionService/1.0"

    # 静默默认访问日志，测试输出保持干净；需要时可在子类打开。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    # ---- 分发 ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            query = parse_qs(parsed.query)
            result = self._route(method, parts, query)
            if result is None:
                _json(self, 404, {"error": {
                    "code": "not_found",
                    "message": f"未知路由: {method} {parsed.path}"}})
            else:
                status, payload = result
                _json(self, status, payload)
        except DomainError as exc:
            _error(self, exc)
        except Exception as exc:  # 兜底：不把堆栈泄露给客户端
            _json(self, 500, {"error": {
                "code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}})

    def _route(self, method: str, parts: list[str],
               query: dict[str, list[str]]) -> Optional[tuple[int, dict]]:
        svc = self.service

        if method == "GET" and parts == ["healthz"]:
            return 200, {"status": "ok"}

        if method == "POST" and parts == ["institutions"]:
            body = _read_body(self)
            return 201, svc.register_institution(
                body.get("code", ""), body.get("name", ""),
                idempotency_key=_idem_key(self))

        if len(parts) == 3 and parts[0] == "institutions" \
                and parts[2] == "representatives" and method == "POST":
            body = _read_body(self)
            return 201, svc.appoint_representative(
                _int_path(parts[1], "institution_id"), body.get("name", ""),
                idempotency_key=_idem_key(self))

        if len(parts) == 4 and parts[0] == "institutions" \
                and parts[2] == "representatives" and parts[3] == "replace" \
                and method == "POST":
            body = _read_body(self)
            return 201, svc.appoint_representative(
                _int_path(parts[1], "institution_id"), body.get("name", ""),
                replace=True, idempotency_key=_idem_key(self))

        if method == "POST" and parts == ["proposals"]:
            body = _read_body(self)
            quorum = body.get("quorum_required", 1)
            try:
                quorum = int(quorum)
            except (TypeError, ValueError):
                raise ValidationError("quorum_required 必须是整数")
            author = _opt_int(body.get("author_institution_id"),
                              "author_institution_id")
            return 201, svc.submit_proposal(
                body.get("title", ""), body.get("body", ""),
                language=body.get("language", "zh"),
                author_institution_id=author,
                quorum_required=quorum,
                deadline=body.get("deadline"),
                deadline_timezone=body.get("deadline_timezone", "UTC"),
                idempotency_key=_idem_key(self))

        if method == "GET" and parts == ["proposals"]:
            status = query.get("status", [None])[0]
            return 200, svc.list_proposals(status=status)

        if len(parts) >= 2 and parts[0] == "proposals":
            proposal_id = _int_path(parts[1], "proposal_id")
            rest = parts[2:]

            if method == "GET" and not rest:
                version = query.get("version", [None])[0]
                return 200, svc.get_proposal(
                    proposal_id,
                    version=int(version) if version else None)

            if method == "GET" and rest == ["rationale"]:
                institution_id = query.get("institution_id", [None])[0]
                if institution_id is None:
                    raise ValidationError("查询理由需指定 ?institution_id=")
                return 200, svc.get_stance_rationale(
                    proposal_id, _int_path(institution_id, "institution_id"))

            if method == "GET" and rest == ["audit"]:
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    raise ValidationError("limit 必须是整数")
                return 200, svc.get_audit(proposal_id=proposal_id, limit=limit)

            if method == "POST" and rest == ["cancel"]:
                return 200, svc.cancel_proposal(
                    proposal_id, idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["conflicts"]:
                body = _read_body(self)
                return 201, svc.declare_conflict(
                    proposal_id,
                    _opt_int(body.get("institution_id"), "institution_id") or 0,
                    body.get("reason", ""), idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["conditions"]:
                body = _read_body(self)
                inst, rep = _actor(self)
                return 201, svc.add_condition(
                    proposal_id, body.get("title", ""),
                    detail=body.get("detail", ""),
                    kind=body.get("kind", "required"),
                    owner_institution_id=_opt_int(
                        body.get("owner_institution_id"), "owner_institution_id"),
                    acting_institution_id=inst, representative_id=rep,
                    idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["stances"]:
                body = _read_body(self)
                inst, rep = _actor(self)
                return 200, svc.cast_stance(
                    proposal_id, institution_id=inst, representative_id=rep,
                    vote=body.get("vote", ""),
                    rationale=body.get("rationale", ""),
                    idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["stances", "withdraw"]:
                body = _read_body(self)
                inst, rep = _actor(self)
                return 200, svc.withdraw_stance(
                    proposal_id, institution_id=inst, representative_id=rep,
                    reason=body.get("reason", ""),
                    idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["signatures"]:
                body = _read_body(self)
                inst, rep = _actor(self)
                return 201, svc.sign(
                    proposal_id, institution_id=inst, representative_id=rep,
                    note=body.get("note", ""),
                    idempotency_key=_idem_key(self))

            if method == "POST" and rest == ["revisions"]:
                body = _read_body(self)
                inst, rep = _actor(self)
                return 201, svc.revise_proposal(
                    proposal_id, body.get("title", ""), body.get("body", ""),
                    language=body.get("language", "zh"),
                    requesting_institution_id=inst, representative_id=rep,
                    reason=body.get("reason", ""),
                    deadline=body.get("deadline"),
                    deadline_timezone=body.get("deadline_timezone", "UTC"),
                    idempotency_key=_idem_key(self))

        if len(parts) == 3 and parts[0] == "conditions" and method == "POST":
            condition_id = _int_path(parts[1], "condition_id")
            inst, rep = _actor(self)
            body = _read_body(self)
            if parts[2] == "satisfy":
                return 200, svc.satisfy_condition(
                    condition_id, acting_institution_id=inst,
                    representative_id=rep, idempotency_key=_idem_key(self))
            if parts[2] == "waive":
                return 200, svc.waive_condition(
                    condition_id, waiver=body.get("waiver", ""),
                    acting_institution_id=inst, representative_id=rep,
                    idempotency_key=_idem_key(self))

        return None


def make_server(service: ResolutionService, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    """构建 HTTP 服务（线程池式，适配 SQLite 串行写事务）。"""

    class _Handler(ApiHandler):
        pass

    _Handler.service = service
    return ThreadingHTTPServer((host, port), _Handler)
