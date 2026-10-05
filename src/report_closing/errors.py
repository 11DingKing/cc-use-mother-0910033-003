"""领域错误与断言工具。"""
from __future__ import annotations


class DomainError(Exception):
    """可预期的业务规则冲突，映射为 HTTP 4xx。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def require(condition: bool, code: str, message: str, status: int = 400) -> None:
    if not condition:
        raise DomainError(code, message, status)


def not_found(what: str, ident: str) -> DomainError:
    return DomainError("not_found", f"{what}不存在：{ident}", 404)
