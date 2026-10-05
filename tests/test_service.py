"""封账领域规则回归测试：覆盖契约的四条不变量。

- 贡献输入摘要：试算报告携带输入摘要与统计口径，输入变则摘要变；
- 双方签署封账：缺任一方不可封账，封账后报告不可变；
- 迟到记录分流：迟到记录进入更正单或下一版，重开须独立批准；
- 汇总下钻解释：汇总值能下钻到纳入/排除记录，导出分块且字节稳定。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_closing.digest import canonical_bytes, chunk_digests, event_fingerprint, sha256_hex
from report_closing.errors import DomainError
from report_closing.service import ReportClosingService
from report_closing.store import Store


class Clock:
    """可推进的测试时钟。"""

    def __init__(self, start: str = "2026-09-01T08:00:00Z") -> None:
        self.t = start

    def __call__(self) -> str:
        return self.t

    def set(self, value: str) -> None:
        self.t = value


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.service = ReportClosingService(Store(":memory:"), clock=self.clock)

    # ---------- 工具 ----------

    def make_event(self, *, verify=True, **overrides) -> dict:
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
        event = self.service.create_event(**params)
        if verify:
            self.service.submit_event(event["event_id"], actor="运营员甲")
            event = self.service.verify_event(event["event_id"], actor="场馆负责人乙")
        return event

    def make_report(self, **overrides) -> dict:
        params = {
            "school_id": "SCH-01",
            "venue_id": "VEN-01",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "actor": "运营员甲",
        }
        params.update(overrides)
        return self.service.create_report(**params)

    def seal_report(self, report_id: str) -> dict:
        self.service.sign_report(report_id, party="学校", signer="校方代表丙")
        self.service.sign_report(report_id, party="场馆", signer="场馆负责人乙")
        return self.service.close_report(report_id, actor="运营员甲")

    def assertConflict(self, code: str, fn, *args, **kwargs) -> DomainError:
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, 409)
        return ctx.exception


class EventStateTest(ServiceTestCase):
    def test_event_lifecycle(self) -> None:
        event = self.service.create_event(
            volunteer_id="V-001", school_id="SCH-01", venue_id="VEN-01",
            theme="秩序维护", service_date="2026-08-05", minutes=90,
            rating=4.2, actor="运营员甲",
        )
        self.assertEqual(event["state"], "草拟")
        # 未提交不能核验
        self.assertConflict("invalid_state", self.service.verify_event, event["event_id"], actor="场馆负责人乙")
        event = self.service.submit_event(event["event_id"], actor="运营员甲")
        self.assertEqual(event["state"], "待核验")
        self.assertIsNotNone(event["submitted_at"])
        # 重复提交被拒绝
        self.assertConflict("invalid_state", self.service.submit_event, event["event_id"], actor="运营员甲")
        event = self.service.verify_event(event["event_id"], actor="场馆负责人乙")
        self.assertEqual(event["state"], "已确认")
        self.assertEqual(event["verified_at"], "2026-09-01T08:00:00Z")

    def test_event_validation(self) -> None:
        with self.assertRaises(DomainError):
            self.service.create_event(
                volunteer_id="V-001", school_id="SCH-01", venue_id="VEN-01",
                theme="讲解导览", service_date="2026-08-05", minutes=0,
                rating=4.0, actor="运营员甲",
            )
        with self.assertRaises(DomainError):
            self.service.create_event(
                volunteer_id="V-001", school_id="SCH-01", venue_id="VEN-01",
                theme="讲解导览", service_date="2026-08-05", minutes=60,
                rating=5.5, actor="运营员甲",
            )
        with self.assertRaises(DomainError):
            self.service.create_event(
                volunteer_id="V-001", school_id="SCH-01", venue_id="VEN-01",
                theme="讲解导览", service_date="2026年8月5日", minutes=60,
                rating=4.0, actor="运营员甲",
            )


class TrialReportTest(ServiceTestCase):
    def test_trial_report_carries_digest_and_caliber(self) -> None:
        self.make_event(minutes=120, rating=4.8, theme="讲解导览")
        self.make_event(minutes=60, rating=4.0, theme="讲解导览", volunteer_id="V-002")
        self.make_event(minutes=90, rating=5.0, theme="秩序维护", verify=False)  # 未确认

        report = self.make_report()

        self.assertEqual(report["state"], "试算")
        self.assertEqual(len(report["input_digest"]), 64)
        caliber = report["caliber"]
        self.assertEqual(caliber["include_states"], ["已确认"])
        self.assertEqual(caliber["verified_cutoff"], report["cutoff"])
        self.assertEqual(caliber["excellent_min_rating"], 4.5)
        self.assertEqual(caliber["scope"]["school_id"], "SCH-01")

        aggregates = report["aggregates"]
        self.assertEqual(aggregates["event_count"], 2)
        self.assertEqual(aggregates["total_minutes"], 180)
        self.assertEqual(aggregates["total_hours"], 3.0)
        self.assertEqual(aggregates["excellent_count"], 1)
        self.assertEqual(aggregates["volunteer_count"], 2)
        themes = {item["theme"]: item for item in aggregates["by_theme"]}
        self.assertEqual(themes["讲解导览"]["total_minutes"], 180)
        self.assertEqual(themes["讲解导览"]["excellent_count"], 1)
        self.assertEqual(report["item_counts"], {"included": 2, "excluded": 1})

    def test_input_digest_stable_and_sensitive(self) -> None:
        self.make_event(minutes=120)
        pending = self.make_event(minutes=60, verify=False)
        report = self.make_report()
        first = report["input_digest"]

        # 输入不变，重算后摘要不变
        refreshed = self.service.refresh_report(report["report_id"], actor="运营员甲")
        self.assertEqual(refreshed["input_digest"], first)

        # 新事件得到确认后进入统计，摘要随之改变
        self.service.submit_event(pending["event_id"], actor="运营员甲")
        self.service.verify_event(pending["event_id"], actor="场馆负责人乙")
        refreshed = self.service.refresh_report(report["report_id"], actor="运营员甲")
        self.assertNotEqual(refreshed["input_digest"], first)
        self.assertEqual(refreshed["aggregates"]["event_count"], 2)

    def test_scope_locked_until_reopen(self) -> None:
        self.make_report()
        self.assertConflict("scope_locked", self.make_report)


class SignAndSealTest(ServiceTestCase):
    def test_dual_signature_then_seal(self) -> None:
        self.make_event()
        report = self.make_report()
        rid = report["report_id"]

        # 未签署不能封账
        self.assertConflict("missing_signatures", self.service.close_report, rid, actor="运营员甲")

        self.service.sign_report(rid, party="学校", signer="校方代表丙")
        # 单方签署仍不能封账
        self.assertConflict("missing_signatures", self.service.close_report, rid, actor="运营员甲")
        # 同一方重复签署被拒绝
        self.assertConflict("duplicate_signature", self.service.sign_report, rid,
                            party="学校", signer="另一位校方")
        # 同一人不能同时代表双方
        self.assertConflict("signer_conflict", self.service.sign_report, rid,
                            party="场馆", signer="校方代表丙")

        self.service.sign_report(rid, party="场馆", signer="场馆负责人乙")
        sealed = self.service.close_report(rid, actor="运营员甲")
        self.assertEqual(sealed["state"], "已封账")
        self.assertEqual(sealed["sealed_at"], "2026-09-01T08:00:00Z")
        self.assertEqual({sig["party"] for sig in sealed["signatures"]}, {"学校", "场馆"})

    def test_sealed_report_is_immutable(self) -> None:
        self.make_event()
        report = self.make_report()
        rid = report["report_id"]
        sealed_digest = self.seal_report(rid)["input_digest"]

        self.assertConflict("invalid_state", self.service.refresh_report, rid, actor="运营员甲")
        self.assertConflict("invalid_state", self.service.sign_report, rid,
                            party="学校", signer="新人")
        self.assertConflict("invalid_state", self.service.close_report, rid, actor="运营员甲")

        # 迟到事件到达后，已封账报告的数值与摘要保持原样
        self.clock.set("2026-09-02T08:00:00Z")
        self.make_event(minutes=999)
        after = self.service.get_report(rid)
        self.assertEqual(after["input_digest"], sealed_digest)
        self.assertEqual(after["aggregates"]["event_count"], 1)

    def test_refresh_invalidates_signatures(self) -> None:
        self.make_event()
        report = self.make_report()
        rid = report["report_id"]
        self.service.sign_report(rid, party="学校", signer="校方代表丙")
        refreshed = self.service.refresh_report(rid, actor="运营员甲")
        self.assertEqual(refreshed["signatures"], [])


class LateRecordTest(ServiceTestCase):
    def test_late_records_divert_to_correction_note(self) -> None:
        self.make_event(minutes=120)
        report = self.make_report()
        rid = report["report_id"]
        sealed = self.seal_report(rid)

        # 封账后补录并核验的事件构成迟到记录
        self.clock.set("2026-09-05T09:00:00Z")
        late = self.make_event(minutes=45, rating=4.9)

        late_view = self.service.late_records(rid)
        self.assertEqual(late_view["late_count"], 1)
        self.assertEqual(late_view["records"][0]["event_id"], late["event_id"])
        self.assertEqual(late_view["records"][0]["routing"][0]["kind"], "未分流")

        # 登记更正单：不影响已封账报告数值
        note = self.service.create_correction(
            rid, actor="运营员甲", reason="补录迟到的核验记录", event_ids=[late["event_id"]]
        )
        self.assertEqual(note["state"], "草拟")
        self.assertEqual(self.service.get_report(rid)["input_digest"], sealed["input_digest"])

        # 更正单需双方确认
        self.service.confirm_correction(note["note_id"], party="学校", signer="校方代表丙")
        note = self.service.confirm_correction(note["note_id"], party="场馆", signer="场馆负责人乙")
        self.assertEqual(note["state"], "已确认")
        self.assertIsNotNone(note["confirmed_at"])
        note = self.service.archive_correction(note["note_id"], actor="运营员甲")
        self.assertEqual(note["state"], "已归档")

        # 分流去向指向更正单
        routing = self.service.late_records(rid)["records"][0]["routing"]
        self.assertEqual(routing[0]["kind"], "更正单")
        self.assertEqual(routing[0]["ref"], note["note_id"])

    def test_correction_rules(self) -> None:
        included = self.make_event(minutes=120)
        report = self.make_report()
        rid = report["report_id"]

        # 试算状态不能登记更正单
        self.assertConflict("invalid_state", self.service.create_correction, rid,
                            actor="运营员甲", reason="x", event_ids=["EVT-9999"])
        self.seal_report(rid)

        # 已纳入的事件无需更正
        self.assertConflict("already_included", self.service.create_correction, rid,
                            actor="运营员甲", reason="x", event_ids=[included["event_id"]])
        # 范围外的事件不能进入更正单
        outsider = self.make_event(school_id="SCH-02")
        self.assertConflict("out_of_scope", self.service.create_correction, rid,
                            actor="运营员甲", reason="x", event_ids=[outsider["event_id"]])
        # 事件不存在
        with self.assertRaises(DomainError) as ctx:
            self.service.create_correction(rid, actor="运营员甲", reason="x", event_ids=["EVT-9999"])
        self.assertEqual(ctx.exception.status, 404)

    def test_reopen_requires_independent_approval(self) -> None:
        self.make_event(minutes=120)
        report = self.make_report()
        rid = report["report_id"]
        self.seal_report(rid)

        self.clock.set("2026-09-05T09:00:00Z")
        late = self.make_event(minutes=45)

        # 签署人、封账操作人都不能充当重开批准人
        self.assertConflict("not_independent_approver", self.service.reopen_report, rid,
                            approver="校方代表丙", reason="补录")
        self.assertConflict("not_independent_approver", self.service.reopen_report, rid,
                            approver="运营员甲", reason="补录")

        # 独立批准人重开：旧版标记已重开，生成下一版试算
        successor = self.service.reopen_report(rid, approver="督导丁", reason="补录迟到记录")
        self.assertEqual(successor["version"], 2)
        self.assertEqual(successor["state"], "试算")
        self.assertEqual(successor["supersedes"], rid)

        old = self.service.get_report(rid)
        self.assertEqual(old["state"], "已重开")
        self.assertEqual(old["reopen_approver"], "督导丁")
        self.assertEqual(old["reopen_reason"], "补录迟到记录")

        # 新一版按新截止纳入迟到记录
        self.assertEqual(successor["aggregates"]["event_count"], 2)
        self.assertEqual(successor["aggregates"]["total_minutes"], 165)

        # 迟到记录分流去向指向下一版
        routing = self.service.late_records(rid)["records"][0]["routing"]
        self.assertEqual(routing[0]["kind"], "下一版")
        self.assertEqual(routing[0]["ref"], successor["report_id"])

        # 已重开的报告不能再次重开
        self.assertConflict("invalid_state", self.service.reopen_report, rid,
                            approver="另一位督导", reason="再次重开")
        # 存在活动版本时同范围不能新建报告
        self.assertConflict("scope_locked", self.make_report)


class DrilldownTest(ServiceTestCase):
    def test_drilldown_explains_included_and_excluded(self) -> None:
        self.make_event(minutes=120, rating=5.0, theme="讲解导览")
        self.make_event(minutes=60, rating=4.0, theme="讲解导览", volunteer_id="V-002")
        self.clock.set("2026-09-01T09:00:00Z")
        cutoff = self.clock()
        self.clock.set("2026-09-03T09:00:00Z")
        late = self.make_event(minutes=30, rating=4.9, theme="秩序维护", volunteer_id="V-003")

        report = self.make_report(cutoff=cutoff)
        rid = report["report_id"]

        # 主题维度下钻：纳入记录与汇总值一致
        result = self.service.drilldown(rid, theme="讲解导览")
        self.assertEqual(result["included"]["count"], 2)
        self.assertEqual(result["included"]["total_minutes"], 180)
        self.assertEqual(result["excluded"]["count"], 0)

        # 优秀评价下钻
        result = self.service.drilldown(rid, excellent=True)
        self.assertEqual(result["included"]["count"], 1)
        self.assertEqual(result["included"]["records"][0]["rating"], 5.0)

        # 排除记录携带原因
        result = self.service.drilldown(rid, included=False)
        self.assertEqual(result["included"]["count"], 2)  # 计数仍在，记录被过滤
        self.assertEqual(result["excluded"]["count"], 1)
        record = result["excluded"]["records"][0]
        self.assertEqual(record["event_id"], late["event_id"])
        self.assertIn("迟到", record["reason"])

        # 下钻合计与报告汇总一致
        all_records = self.service.drilldown(rid)
        self.assertEqual(
            all_records["included"]["total_minutes"],
            self.service.get_report(rid)["aggregates"]["total_minutes"],
        )


class ExportTest(ServiceTestCase):
    def test_export_is_byte_stable_with_chunk_digests(self) -> None:
        for index in range(55):
            self.make_event(minutes=10 + index % 5, volunteer_id=f"V-{index:03d}")
        report = self.make_report()
        rid = report["report_id"]
        self.seal_report(rid)

        first = self.service.export_report(rid)
        second = self.service.export_report(rid)

        # 重复导出字节一致
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))

        # 55 条纳入记录分两块：50 + 5
        self.assertEqual([chunk["count"] for chunk in first["included_chunks"]], [50, 5])
        self.assertEqual([chunk["index"] for chunk in first["included_chunks"]], [0, 1])

        # 分块摘要可由记录内容独立复算
        records = first["records"]["included"]
        self.assertEqual(len(records), 55)
        recomputed = chunk_digests(records)
        self.assertEqual(
            [chunk["digest"] for chunk in recomputed],
            [chunk["digest"] for chunk in first["included_chunks"]],
        )

        # 整体摘要可复算
        expected = sha256_hex(canonical_bytes({
            "report": first["report"],
            "included_chunks": first["included_chunks"],
            "excluded_chunks": first["excluded_chunks"],
        }))
        self.assertEqual(first["export_digest"], expected)

        # 报告头携带输入摘要与统计口径
        self.assertEqual(first["report"]["input_digest"], report["input_digest"])
        self.assertEqual(first["report"]["state"], "已封账")
        self.assertEqual(len(first["report"]["signatures"]), 2)

    def test_event_fingerprint_sensitive_to_content(self) -> None:
        event = self.make_event()
        base = event_fingerprint(event)
        changed = dict(event, minutes=event["minutes"] + 1)
        self.assertNotEqual(event_fingerprint(changed), base)


if __name__ == "__main__":
    unittest.main()
