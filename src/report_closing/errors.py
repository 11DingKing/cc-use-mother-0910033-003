"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突，携带 HTTP 状态码与稳定错误码。"""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def not_found(message: str) -> DomainError:
    return DomainError("not_found", message, 404)


def bad_request(message: str) -> DomainError:
    return DomainError("bad_request", message, 400)


def conflict(code: str, message: str) -> DomainError:
    return DomainError(code, message, 409)
