"""领域错误类型。"""
from __future__ import annotations


class ResolutionError(Exception):
    """所有决议服务错误的基类。"""

    code = "resolution_error"
    http_status = 400


class ValidationError(ResolutionError):
    """请求内容不满足领域约束。"""

    code = "validation_error"
    http_status = 422


class AuthorizationError(ResolutionError):
    """操作者无权执行该动作（机构只能修改自己的立场）。"""

    code = "forbidden"
    http_status = 403


class NotFoundError(ResolutionError):
    """目标资源不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(ResolutionError):
    """动作与当前状态冲突，例如重复投票、已生效后撤回。"""

    code = "conflict"
    http_status = 409


class DeadlinePassedError(ConflictError):
    """议题已超过时区截止时间。"""

    code = "deadline_passed"
    http_status = 409
