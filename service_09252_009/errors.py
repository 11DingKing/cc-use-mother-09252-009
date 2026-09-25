"""领域错误。

每个错误携带稳定的机器可读 ``code`` 与对应的 HTTP 状态码，
便于接口层直接渲染为统一的错误响应。
"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则错误的基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class AuthorizationError(DomainError):
    status = 403
    code = "forbidden"


class ConflictError(DomainError):
    """资源当前状态与请求冲突（例如唯一约束、重复表态）。"""

    status = 409
    code = "conflict"


class DeadlinePassedError(DomainError):
    status = 409
    code = "deadline_passed"


class StateError(DomainError):
    status = 409
    code = "invalid_state"
