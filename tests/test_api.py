"""HTTP API 端到端测试：真实起服务、走完整封账流程。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import quote
from wsgiref.simple_server import WSGIServer, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_closing.api import create_app
from report_closing.service import ReportClosingService
from report_closing.store import Store


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = create_app(ReportClosingService(Store(":memory:")))
        cls.server = make_server("127.0.0.1", 0, app, server_class=ThreadingWSGIServer)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    # ---------- HTTP 工具 ----------

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        status, payload, _ = self.request_raw(method, path, body)
        return status, json.loads(payload.decode("utf-8"))

    def request_raw(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, raw, dict(response.getheaders())

    def make_verified_event(self, **overrides) -> dict:
        params = {
            "volunteer_id": "V-001",
            "school_id": "SCH-01",
            "venue_id": "VEN-01",
            "theme": "讲解导览",
            "service_date": "2026-08-10",
            "minutes": 120,
            "rating": 4.8,
            "actor": "运营员甲",
        }
        params.update(overrides)
        status, event = self.request("POST", "/events", params)
        self.assertEqual(status, 201)
        event_id = event["event_id"]
        self.assertEqual(self.request("POST", f"/events/{event_id}/submit", {"actor": "运营员甲"})[0], 200)
        status, event = self.request("POST", f"/events/{event_id}/verify", {"actor": "场馆负责人乙"})
        self.assertEqual(status, 200)
        return event

    # ---------- 测试 ----------

    def test_full_closing_flow_over_http(self) -> None:
        # 1. 汇集并核验服务事件
        first = self.make_verified_event(minutes=120, rating=4.8)
        second = self.make_verified_event(minutes=60, rating=4.1, volunteer_id="V-002", theme="秩序维护")

        # 2. 生成试算报告：截止取第一条事件的核验时刻，保证两条都已确认且纳入
        status, report = self.request("POST", "/reports", {
            "school_id": "SCH-01", "venue_id": "VEN-01",
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "cutoff": second["verified_at"], "actor": "运营员甲",
        })
        self.assertEqual(status, 201)
        rid = report["report_id"]
        self.assertEqual(report["state"], "试算")
        self.assertEqual(report["aggregates"]["total_minutes"], 180)
        self.assertEqual(report["aggregates"]["excellent_count"], 1)
        self.assertEqual(len(report["input_digest"]), 64)

        # 3. 汇总下钻：主题与优秀维度都能回到记录
        status, drill = self.request("GET", f"/reports/{rid}/drilldown?theme={quote('讲解导览')}")
        self.assertEqual(status, 200)
        self.assertEqual(drill["included"]["count"], 1)
        self.assertEqual(drill["included"]["records"][0]["event_id"], first["event_id"])
        status, drill = self.request("GET", f"/reports/{rid}/drilldown?excellent=false")
        self.assertEqual(drill["included"]["count"], 1)
        self.assertEqual(drill["included"]["records"][0]["event_id"], second["event_id"])

        # 4. 双方签署后封账
        self.assertEqual(
            self.request("POST", f"/reports/{rid}/close", {"actor": "运营员甲"})[0], 409
        )
        self.assertEqual(
            self.request("POST", f"/reports/{rid}/sign", {"party": "学校", "signer": "校方代表丙"})[0], 200
        )
        self.assertEqual(
            self.request("POST", f"/reports/{rid}/sign", {"party": "场馆", "signer": "场馆负责人乙"})[0], 200
        )
        status, sealed = self.request("POST", f"/reports/{rid}/close", {"actor": "运营员甲"})
        self.assertEqual(status, 200)
        self.assertEqual(sealed["state"], "已封账")

        # 5. 导出：包含分块摘要，重复下载字节一致
        status_one, body_one, headers_one = self.request_raw("GET", f"/reports/{rid}/export")
        status_two, body_two, _ = self.request_raw("GET", f"/reports/{rid}/export")
        self.assertEqual(status_one, 200)
        self.assertEqual(status_two, 200)
        self.assertEqual(body_one, body_two)
        self.assertIn("application/json", headers_one["Content-Type"])
        export = json.loads(body_one.decode("utf-8"))
        self.assertEqual(export["format"], "volunteer-contribution-report")
        self.assertEqual(len(export["included_chunks"]), 1)
        self.assertEqual(export["included_chunks"][0]["count"], 2)
        self.assertEqual(len(export["export_digest"]), 64)

        # 6. 迟到记录：封账后补录核验，已封账报告不变
        time.sleep(1.1)  # 让下一条核验时间严格晚于报告截止（秒级精度）
        late = self.make_verified_event(minutes=45, rating=4.9, volunteer_id="V-003")
        status, current = self.request("GET", f"/reports/{rid}")
        self.assertEqual(current["aggregates"]["total_minutes"], 180)

        status, late_view = self.request("GET", f"/reports/{rid}/late-records")
        self.assertEqual(status, 200)
        self.assertEqual(late_view["late_count"], 1)
        self.assertEqual(late_view["records"][0]["event_id"], late["event_id"])
        self.assertEqual(late_view["records"][0]["routing"][0]["kind"], "未分流")

        # 7. 更正单分流：双方确认后生效
        status, note = self.request("POST", f"/reports/{rid}/corrections", {
            "actor": "运营员甲", "reason": "补录迟到核验", "event_ids": [late["event_id"]],
        })
        self.assertEqual(status, 201)
        note_id = note["note_id"]
        self.assertEqual(
            self.request("POST", f"/corrections/{note_id}/confirm", {"party": "学校", "signer": "校方代表丙"})[0],
            200,
        )
        status, note = self.request(
            "POST", f"/corrections/{note_id}/confirm", {"party": "场馆", "signer": "场馆负责人乙"}
        )
        self.assertEqual(note["state"], "已确认")
        status, late_view = self.request("GET", f"/reports/{rid}/late-records")
        self.assertEqual(late_view["records"][0]["routing"][0]["kind"], "更正单")

        # 8. 重开须独立批准：签署人被拒，独立批准人生成下一版
        status, error = self.request("POST", f"/reports/{rid}/reopen", {
            "approver": "校方代表丙", "reason": "补录",
        })
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "not_independent_approver")

        status, successor = self.request("POST", f"/reports/{rid}/reopen", {
            "approver": "督导丁", "reason": "补录迟到记录",
        })
        self.assertEqual(status, 201)
        self.assertEqual(successor["version"], 2)
        self.assertEqual(successor["aggregates"]["total_minutes"], 225)
        status, old = self.request("GET", f"/reports/{rid}")
        self.assertEqual(old["state"], "已重开")

    def test_error_shapes(self) -> None:
        status, error = self.request("GET", "/reports/RPT-9999")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "not_found")

        status, error = self.request("GET", "/events/EVT-9999")
        self.assertEqual(status, 404)

        status, error = self.request("DELETE", "/events/EVT-0001")
        self.assertEqual(status, 405)

        status, error = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)

        status, error = self.request("POST", "/events", {"actor": "运营员甲"})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "validation")

    def test_invalid_json_body_rejected(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/events", body=b"{not json", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["code"], "validation")


if __name__ == "__main__":
    unittest.main()
