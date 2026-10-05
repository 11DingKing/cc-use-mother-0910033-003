"""封账领域服务的业务规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_closing.errors import DomainError
from report_closing.models import EventStatus, ReportStatus
from report_closing.service import ClosingService

NOW = "2026-10-05T09:00:00+08:00"


def make_service() -> ClosingService:
    service = ClosingService()
    events = [
        # 纳入：周期内已确认
        ("E-001", "讲解服务", 120, "优秀", True),
        ("E-002", "秩序引导", 90, "良好", True),
        ("E-003", "讲解服务", 60, "优秀", True),
        # 排除：未核验 / 核验不通过
        ("E-004", "文创协助", 45, "合格", None),
        ("E-005", "讲解服务", 30, "合格", False),
    ]
    for event_id, theme, minutes, rating, approve in events:
        service.submit_event({
            "event_id": event_id, "volunteer_id": "V-01", "venue_id": "馆-01",
            "school_id": "校-01", "theme": theme, "minutes": minutes,
            "served_on": "2026-06-01", "rating": rating,
        }, now=NOW)
        if approve is not None:
            service.verify_event(event_id, approve=approve)
    return service


def trial_payload() -> dict:
    return {
        "report_id": "RPT-2026", "school_id": "校-01", "venue_id": "馆-01",
        "period_start": "2026-01-01", "period_end": "2026-12-31",
    }


def signed_and_closed(service: ClosingService):
    report = service.generate_trial(trial_payload(), now=NOW)
    service.sign_report("RPT-2026", {"party": "school", "signer": "校长-甲"}, now=NOW)
    service.sign_report("RPT-2026", {"party": "venue", "signer": "馆长-乙"}, now=NOW)
    return service.close_report("RPT-2026", now=NOW)


class TrialTest(unittest.TestCase):
    def test_trial_carries_digest_caliber_and_stats(self) -> None:
        report = make_service().generate_trial(trial_payload(), now=NOW)
        self.assertEqual(report.status, ReportStatus.DRAFT)
        self.assertEqual(report.included_ids, ["E-001", "E-002", "E-003"])
        self.assertEqual(
            report.excluded,
            [{"event_id": "E-004", "reason": "尚未完成核验"},
             {"event_id": "E-005", "reason": "未通过核验"}],
        )
        self.assertEqual(report.stats["total_minutes"], 270)
        self.assertEqual(report.stats["total_hours_text"], "4小时30分")
        self.assertEqual(report.stats["theme_coverage"], ["秩序引导", "讲解服务"])
        self.assertEqual(report.stats["excellent_count"], 2)
        self.assertEqual(report.stats["excellent_per_mille"], 666)
        self.assertTrue(report.input_digest)
        self.assertIn("included", report.caliber)

    def test_digest_is_deterministic(self) -> None:
        first = make_service().generate_trial(trial_payload(), now=NOW)
        second = make_service().generate_trial(trial_payload(), now=NOW)
        self.assertEqual(first.input_digest, second.input_digest)

    def test_second_trial_blocked_until_closed(self) -> None:
        service = make_service()
        service.generate_trial(trial_payload(), now=NOW)
        with self.assertRaises(DomainError):
            service.generate_trial(trial_payload(), now=NOW)


class SignCloseTest(unittest.TestCase):
    def test_close_requires_both_parties(self) -> None:
        service = make_service()
        service.generate_trial(trial_payload(), now=NOW)
        service.sign_report("RPT-2026", {"party": "school", "signer": "校长-甲"}, now=NOW)
        with self.assertRaises(DomainError):
            service.close_report("RPT-2026", now=NOW)
        report = service.sign_report(
            "RPT-2026", {"party": "venue", "signer": "馆长-乙"}, now=NOW)
        self.assertEqual(report.status, ReportStatus.SIGNED)
        closed = service.close_report("RPT-2026", now=NOW)
        self.assertEqual(closed.status, ReportStatus.CLOSED)
        self.assertTrue(closed.closed_at)

    def test_duplicate_sign_rejected(self) -> None:
        service = make_service()
        service.generate_trial(trial_payload(), now=NOW)
        service.sign_report("RPT-2026", {"party": "school", "signer": "校长-甲"}, now=NOW)
        with self.assertRaises(DomainError):
            service.sign_report("RPT-2026", {"party": "school", "signer": "别人"}, now=NOW)

    def test_signature_binds_input_digest(self) -> None:
        service = make_service()
        report = service.generate_trial(trial_payload(), now=NOW)
        signed = service.sign_report(
            "RPT-2026", {"party": "school", "signer": "校长-甲"}, now=NOW)
        self.assertEqual(signed.signatures[0].input_digest, report.input_digest)


class ReopenTest(unittest.TestCase):
    def test_reopen_requires_independent_approver(self) -> None:
        service = make_service()
        signed_and_closed(service)
        with self.assertRaises(DomainError) as ctx:
            service.reopen_report(
                "RPT-2026", {"approver": "校长-甲", "reason": "补录"}, now=NOW)
        self.assertEqual(ctx.exception.code, "not_independent")

    def test_reopen_then_next_version(self) -> None:
        service = make_service()
        signed_and_closed(service)
        with self.assertRaises(DomainError):
            service.generate_trial(trial_payload(), now=NOW)  # 封账中不可直接新版
        reopened = service.reopen_report(
            "RPT-2026", {"approver": "督导-丙", "reason": "迟到补录"}, now=NOW)
        self.assertEqual(reopened.status, ReportStatus.REOPENED)
        self.assertEqual(reopened.reopen_approval["approver"], "督导-丙")
        nxt = service.generate_trial(trial_payload(), now=NOW)
        self.assertEqual(nxt.version, 2)


class LateEventTest(unittest.TestCase):
    def test_late_entry_routes_to_next_version(self) -> None:
        service = make_service()
        signed_and_closed(service)
        service.submit_event({
            "event_id": "E-006", "volunteer_id": "V-02", "venue_id": "馆-01",
            "school_id": "校-01", "theme": "文创协助", "minutes": 50,
            "served_on": "2026-06-02", "rating": "良好",
        }, now=NOW)
        service.verify_event("E-006", approve=True)
        correction = service.route_late_event(
            {"report_id": "RPT-2026", "event_id": "E-006",
             "correction_id": "COR-001", "reason": "迟到补录"}, now=NOW)
        self.assertEqual(correction.kind.value, "补录")
        self.assertEqual(correction.target_version, 2)  # 进入下一版

    def test_correction_slip_targets_current_version(self) -> None:
        service = make_service()
        signed_and_closed(service)
        service.submit_event({
            "event_id": "E-006", "volunteer_id": "V-02", "venue_id": "馆-01",
            "school_id": "校-01", "theme": "文创协助", "minutes": 50,
            "served_on": "2026-06-02", "rating": "良好",
        }, now=NOW)
        correction = service.route_late_event(
            {"report_id": "RPT-2026", "event_id": "E-006", "kind": "更正",
             "correction_id": "COR-002", "reason": "时长登记错误"}, now=NOW)
        self.assertEqual(correction.kind.value, "更正")
        self.assertEqual(correction.target_version, 1)  # 更正单钉在当前版本

    def test_included_event_cannot_be_late_entry(self) -> None:
        service = make_service()
        signed_and_closed(service)
        with self.assertRaises(DomainError) as ctx:
            service.route_late_event(
                {"report_id": "RPT-2026", "event_id": "E-001",
                 "correction_id": "COR-003", "reason": "重复"}, now=NOW)
        self.assertEqual(ctx.exception.code, "already_included")

    def test_late_route_requires_closed_report(self) -> None:
        service = make_service()
        service.generate_trial(trial_payload(), now=NOW)
        with self.assertRaises(DomainError):
            service.route_late_event(
                {"report_id": "RPT-2026", "event_id": "E-001",
                 "correction_id": "COR-004", "reason": "太早"}, now=NOW)


class DrilldownExportTest(unittest.TestCase):
    def test_drilldown_from_summary_to_records(self) -> None:
        service = make_service()
        signed_and_closed(service)
        detail = service.drilldown("RPT-2026")
        self.assertEqual(detail["stats"]["event_count"], 3)
        self.assertEqual([r["event_id"] for r in detail["included"]],
                         ["E-001", "E-002", "E-003"])
        reasons = {r["event_id"]: r["excluded_reason"] for r in detail["excluded"]}
        self.assertEqual(reasons, {"E-004": "尚未完成核验", "E-005": "未通过核验"})

    def test_export_chunks_and_repeat_stability(self) -> None:
        service = make_service()
        signed_and_closed(service)
        first = service.export_report("RPT-2026", chunk_size=2)
        second = service.export_report("RPT-2026", chunk_size=2)
        self.assertEqual(first, second)  # 重复下载稳定
        chunks = first["manifest"]["chunks"]
        self.assertEqual([c["record_count"] for c in chunks], [2, 2, 1])
        self.assertEqual(len({c["digest"] for c in chunks}), 3)
        self.assertTrue(first["manifest"]["manifest_digest"])
        # 分块摘要覆盖全部记录
        self.assertEqual(sum(c["record_count"] for c in chunks), 5)

    def test_export_digest_changes_with_chunk_size(self) -> None:
        service = make_service()
        signed_and_closed(service)
        by_two = service.export_report("RPT-2026", chunk_size=2)
        by_five = service.export_report("RPT-2026", chunk_size=5)
        self.assertNotEqual(by_two["manifest"]["manifest_digest"],
                            by_five["manifest"]["manifest_digest"])


class EventGuardTest(unittest.TestCase):
    def test_invalid_event_rejected(self) -> None:
        service = ClosingService()
        with self.assertRaises(DomainError):
            service.submit_event({"event_id": "X"}, now=NOW)
        with self.assertRaises(DomainError):
            service.submit_event({
                "event_id": "X", "volunteer_id": "V", "venue_id": "馆",
                "school_id": "校", "theme": "讲解", "minutes": 10,
                "served_on": "2026-01-01", "rating": "特优",
            }, now=NOW)

    def test_verified_event_is_final(self) -> None:
        service = ClosingService()
        service.submit_event({
            "event_id": "E-1", "volunteer_id": "V", "venue_id": "馆",
            "school_id": "校", "theme": "讲解", "minutes": 10,
            "served_on": "2026-01-01", "rating": "优秀",
        }, now=NOW)
        event = service.verify_event("E-1", approve=True)
        self.assertEqual(event.status, EventStatus.CONFIRMED)
        with self.assertRaises(DomainError):
            service.verify_event("E-1", approve=False)


if __name__ == "__main__":
    unittest.main()
