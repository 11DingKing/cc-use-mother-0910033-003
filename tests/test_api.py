"""HTTP API 端到端测试：内存服务 + 真实 HTTP 往返。"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_closing.api import create_server
from report_closing.service import ClosingService

NOW = "2026-10-05T09:00:00+08:00"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(host="127.0.0.1", port=0,
                                   service=ClosingService(), now=NOW)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_closing_flow(self) -> None:
        # 1. 汇集并核验事件
        for event_id, approve in (("E-101", True), ("E-102", True), ("E-103", False)):
            status, _ = self.call("POST", "/events", {
                "event_id": event_id, "volunteer_id": "V-01", "venue_id": "馆-01",
                "school_id": "校-01", "theme": "讲解服务", "minutes": 60,
                "served_on": "2026-06-01", "rating": "优秀",
            })
            self.assertEqual(status, 201)
            status, body = self.call("POST", f"/events/{event_id}/verify",
                                     {"approve": approve})
            self.assertEqual(status, 200)
        # 2. 试算报告带输入摘要与统计口径
        status, trial = self.call("POST", "/reports/trial", {
            "report_id": "RPT-API", "school_id": "校-01", "venue_id": "馆-01",
            "period_start": "2026-01-01", "period_end": "2026-12-31",
        })
        self.assertEqual(status, 201)
        self.assertEqual(trial["status"], "试算中")
        self.assertTrue(trial["input_digest"])
        self.assertIn("included", trial["caliber"])
        self.assertEqual(trial["stats"]["event_count"], 2)
        # 3. 双方签署后封账
        self.call("POST", "/reports/RPT-API/sign", {"party": "school", "signer": "校长-甲"})
        status, body = self.call("POST", "/reports/RPT-API/close")
        self.assertEqual(status, 409)  # 单方签署不能封账
        self.call("POST", "/reports/RPT-API/sign", {"party": "venue", "signer": "馆长-乙"})
        status, closed = self.call("POST", "/reports/RPT-API/close")
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "已封账")
        # 4. 下钻
        status, detail = self.call("GET", "/reports/RPT-API/drilldown")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["included"]), 2)
        self.assertEqual(detail["excluded"][0]["excluded_reason"], "未通过核验")
        # 5. 分块摘要导出，重复下载稳定
        status, first = self.call("GET", "/reports/RPT-API/export?chunk_size=1")
        self.assertEqual(status, 200)
        _, second = self.call("GET", "/reports/RPT-API/export?chunk_size=1")
        self.assertEqual(first, second)
        self.assertEqual(len(first["manifest"]["chunks"]), 3)
        # 6. 迟到记录分流为补录
        self.call("POST", "/events", {
            "event_id": "E-104", "volunteer_id": "V-02", "venue_id": "馆-01",
            "school_id": "校-01", "theme": "秩序引导", "minutes": 30,
            "served_on": "2026-06-02", "rating": "良好",
        })
        self.call("POST", "/events/E-104/verify", {"approve": True})
        status, correction = self.call("POST", "/reports/RPT-API/late-events", {
            "event_id": "E-104", "correction_id": "COR-API-1", "reason": "迟到补录",
        })
        self.assertEqual(status, 201)
        self.assertEqual(correction["kind"], "补录")
        self.assertEqual(correction["target_version"], 2)
        # 7. 重开须独立批准
        status, body = self.call("POST", "/reports/RPT-API/reopen",
                                 {"approver": "校长-甲", "reason": "补录"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "not_independent")
        status, reopened = self.call("POST", "/reports/RPT-API/reopen",
                                     {"approver": "督导-丙", "reason": "补录"})
        self.assertEqual(status, 200)
        self.assertEqual(reopened["status"], "已重开")
        # 8. 重开后生成第二版
        status, v2 = self.call("POST", "/reports/trial", {
            "report_id": "RPT-API", "school_id": "校-01", "venue_id": "馆-01",
            "period_start": "2026-01-01", "period_end": "2026-12-31",
        })
        self.assertEqual(status, 201)
        self.assertEqual(v2["version"], 2)
        self.assertIn("E-104", v2["included_ids"])  # 补录进入下一版

    def test_unknown_route_and_bad_json(self) -> None:
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/events", data=b"{bad", method="POST")
        status, body = self.call_raw(request)
        self.assertEqual(status, 400)

    def call_raw(self, request) -> tuple[int, object]:
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
