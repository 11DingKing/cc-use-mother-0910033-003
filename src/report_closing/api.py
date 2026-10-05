"""HTTP API：基于标准库 http.server 的 JSON 接口。

路由一览：
  POST /events                     提交服务事件（待核验）
  POST /events/{id}/verify         核验事件 {"approve": true|false, "note": ...}
  POST /reports/trial              生成试算报告
  POST /reports/{id}/sign          单方签署 {"party": "school"|"venue", "signer": ...}
  POST /reports/{id}/close         双方签署后封账
  POST /reports/{id}/reopen        独立批准人重开 {"approver": ..., "reason": ...}
  POST /reports/{id}/late-events   迟到记录分流（补录下一版 / 更正单）
  GET  /reports/{id}               查看报告（含输入摘要与统计口径）
  GET  /reports/{id}/drilldown     从汇总值下钻到纳入/排除记录
  GET  /reports/{id}/export        分块摘要导出（重复下载结果一致）
  GET  /corrections                更正单列表
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, bad_request, not_found
from .service import ClosingService

NOW = "2026-10-05T00:00:00+08:00"  # 演示用固定时钟；生产环境注入真实时钟


def make_handler(service: ClosingService, now: str = NOW) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReportClosing/0.1"

        # ---- 基础工具 ----

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise bad_request("请求体不是合法 JSON") from None
            if not isinstance(data, dict):
                raise bad_request("请求体必须是 JSON 对象")
            return data

        def log_message(self, fmt: str, *args: object) -> None:  # 静默访问日志
            return

        # ---- 路由 ----

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._send(*self._route_post(urlparse(self.path).path, self._body()))
            except DomainError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                self._send(*self._route_get(parsed.path, query))
            except DomainError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})

        def _route_post(self, path: str, body: dict) -> tuple[int, object]:
            if path == "/events":
                return 201, service.submit_event(body, now=now).to_dict()

            match = re.fullmatch(r"/events/([^/]+)/verify", path)
            if match:
                approve = body.get("approve")
                if not isinstance(approve, bool):
                    raise bad_request("approve 必须是布尔值")
                event = service.verify_event(match.group(1), approve=approve,
                                             note=str(body.get("note", "")))
                return 200, event.to_dict()

            if path == "/reports/trial":
                return 201, service.generate_trial(body, now=now).to_dict()

            match = re.fullmatch(r"/reports/([^/]+)/sign", path)
            if match:
                return 200, service.sign_report(match.group(1), body, now=now).to_dict()

            match = re.fullmatch(r"/reports/([^/]+)/close", path)
            if match:
                return 200, service.close_report(match.group(1), body, now=now).to_dict()

            match = re.fullmatch(r"/reports/([^/]+)/reopen", path)
            if match:
                return 200, service.reopen_report(match.group(1), body, now=now).to_dict()

            match = re.fullmatch(r"/reports/([^/]+)/late-events", path)
            if match:
                correction = service.route_late_event(
                    {**body, "report_id": match.group(1)}, now=now)
                return 201, correction.to_dict()

            raise not_found(f"接口不存在：POST {path}")

        def _route_get(self, path: str, query: dict) -> tuple[int, object]:
            def _version() -> int | None:
                raw = query.get("version", [None])[0]
                return int(raw) if raw is not None else None

            match = re.fullmatch(r"/reports/([^/]+)/drilldown", path)
            if match:
                return 200, service.drilldown(match.group(1), _version())

            match = re.fullmatch(r"/reports/([^/]+)/export", path)
            if match:
                chunk_size = int(query.get("chunk_size", ["10"])[0])
                return 200, service.export_report(match.group(1), _version(),
                                                  chunk_size=chunk_size)

            match = re.fullmatch(r"/reports/([^/]+)", path)
            if match:
                return 200, service.store.get_report(match.group(1), _version()).to_dict()

            if path == "/corrections":
                return 200, [c.to_dict() for c in service.store.all_corrections()]

            raise not_found(f"接口不存在：GET {path}")

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  service: ClosingService | None = None,
                  now: str = NOW) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service or ClosingService(), now))


if __name__ == "__main__":
    httpd = create_server()
    print("志愿贡献报告封账服务已启动：http://127.0.0.1:8080")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
