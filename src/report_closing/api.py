"""HTTP API 层：标准库 WSGI 实现，无外部依赖。

所有响应都使用规范化 JSON（键排序、紧凑分隔符），保证同一数据字节稳定，
这也是导出端点"重复下载稳定"的基础。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs

from .digest import canonical_bytes
from .errors import DomainError
from .service import ReportClosingService

Handler = Callable[..., tuple[int, Any]]


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise DomainError("validation", f"无法解析布尔参数：{value!r}")


class Request:
    def __init__(self, environ: dict) -> None:
        self.method = environ["REQUEST_METHOD"].upper()
        self.path = environ["PATH_INFO"]
        self.query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", "")).items()}
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length > 0 else b""
        if raw:
            try:
                self.body = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise DomainError("validation", "请求体不是合法 JSON")
            if not isinstance(self.body, dict):
                raise DomainError("validation", "请求体必须是 JSON 对象")
        else:
            self.body = {}


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method, regex, handler))

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        try:
            request = Request(environ)
            status, payload = self._dispatch(request)
        except DomainError as error:
            status, payload = error.status, {"error": {"code": error.code, "message": error.message}}
        except Exception as error:  # noqa: BLE001 - 兜底，避免堆栈泄露给客户端
            status, payload = 500, {"error": {"code": "internal", "message": f"服务内部错误：{error}"}}
        body = canonical_bytes(payload)
        start_response(
            f"{status} {STATUS_TEXT.get(status, 'Unknown')}",
            [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]

    def _dispatch(self, request: Request) -> tuple[int, Any]:
        path_params: dict[str, str] = {}
        allowed = False
        for method, regex, handler in self._routes:
            match = regex.match(request.path)
            if not match:
                continue
            if method != request.method:
                allowed = True
                continue
            path_params = match.groupdict()
            try:
                return handler(request, **path_params)
            except TypeError as error:
                # 请求体缺字段或字段名与服务签名不符
                raise DomainError("validation", f"请求参数不完整或不合法：{error}")
        if allowed:
            raise DomainError("method_not_allowed", f"{request.method} {request.path} 不支持该请求方法", 405)
        raise DomainError("not_found", f"路由不存在：{request.method} {request.path}", 404)


STATUS_TEXT = {
    200: "OK",
    201: "Created",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    500: "Internal Server Error",
}


def create_app(service: ReportClosingService) -> Router:
    """注册全部路由，返回 WSGI 应用。"""
    app = Router()

    # ---------- 服务事件 ----------
    def create_event(req: Request) -> tuple[int, Any]:
        return 201, service.create_event(**req.body)

    def get_event(req: Request, event_id: str) -> tuple[int, Any]:
        return 200, service.get_event(event_id)

    def list_events(req: Request) -> tuple[int, Any]:
        return 200, {
            "events": service.list_events(
                school_id=req.query.get("school_id"),
                venue_id=req.query.get("venue_id"),
                state=req.query.get("state"),
            )
        }

    def submit_event(req: Request, event_id: str) -> tuple[int, Any]:
        return 200, service.submit_event(event_id, **req.body)

    def verify_event(req: Request, event_id: str) -> tuple[int, Any]:
        return 200, service.verify_event(event_id, **req.body)

    app.add("POST", "/events", create_event)
    app.add("GET", "/events", list_events)
    app.add("GET", "/events/{event_id}", get_event)
    app.add("POST", "/events/{event_id}/submit", submit_event)
    app.add("POST", "/events/{event_id}/verify", verify_event)

    # ---------- 试算报告与封账 ----------
    def create_report(req: Request) -> tuple[int, Any]:
        return 201, service.create_report(**req.body)

    def list_reports(req: Request) -> tuple[int, Any]:
        return 200, {
            "reports": service.list_reports(
                school_id=req.query.get("school_id"),
                venue_id=req.query.get("venue_id"),
                state=req.query.get("state"),
            )
        }

    def get_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.get_report(report_id)

    def refresh_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.refresh_report(report_id, **req.body)

    def sign_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.sign_report(report_id, **req.body)

    def close_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.close_report(report_id, **req.body)

    def reopen_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 201, service.reopen_report(report_id, **req.body)

    app.add("POST", "/reports", create_report)
    app.add("GET", "/reports", list_reports)
    app.add("GET", "/reports/{report_id}", get_report)
    app.add("POST", "/reports/{report_id}/refresh", refresh_report)
    app.add("POST", "/reports/{report_id}/sign", sign_report)
    app.add("POST", "/reports/{report_id}/close", close_report)
    app.add("POST", "/reports/{report_id}/reopen", reopen_report)

    # ---------- 下钻、迟到分流、导出 ----------
    def drilldown(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.drilldown(
            report_id,
            theme=req.query.get("theme"),
            excellent=_parse_bool(req.query.get("excellent")),
            included=_parse_bool(req.query.get("included")),
        )

    def late_records(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.late_records(report_id)

    def export_report(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, service.export_report(report_id)

    app.add("GET", "/reports/{report_id}/drilldown", drilldown)
    app.add("GET", "/reports/{report_id}/late-records", late_records)
    app.add("GET", "/reports/{report_id}/export", export_report)

    # ---------- 更正单 ----------
    def create_correction(req: Request, report_id: str) -> tuple[int, Any]:
        return 201, service.create_correction(report_id, **req.body)

    def list_corrections(req: Request, report_id: str) -> tuple[int, Any]:
        return 200, {"corrections": service.list_corrections(report_id)}

    def get_correction(req: Request, note_id: str) -> tuple[int, Any]:
        return 200, service.get_correction(note_id)

    def confirm_correction(req: Request, note_id: str) -> tuple[int, Any]:
        return 200, service.confirm_correction(note_id, **req.body)

    def archive_correction(req: Request, note_id: str) -> tuple[int, Any]:
        return 200, service.archive_correction(note_id, **req.body)

    app.add("POST", "/reports/{report_id}/corrections", create_correction)
    app.add("GET", "/reports/{report_id}/corrections", list_corrections)
    app.add("GET", "/corrections/{note_id}", get_correction)
    app.add("POST", "/corrections/{note_id}/confirm", confirm_correction)
    app.add("POST", "/corrections/{note_id}/archive", archive_correction)

    return app
