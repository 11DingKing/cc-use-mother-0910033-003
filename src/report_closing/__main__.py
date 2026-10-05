"""服务入口：python -m report_closing [--port 8080] [--db report_closing.db]"""
from __future__ import annotations

import argparse
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from .api import create_app
from .service import ReportClosingService
from .store import Store


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="志愿贡献报告封账后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径，默认内存库")
    args = parser.parse_args()

    service = ReportClosingService(Store(args.db))
    app = create_app(service)
    with make_server(args.host, args.port, app, server_class=ThreadingWSGIServer) as server:
        print(f"志愿贡献报告封账服务已启动：http://{args.host}:{args.port}（数据库：{args.db}）")
        server.serve_forever()


if __name__ == "__main__":
    main()
