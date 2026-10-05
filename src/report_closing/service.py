"""封账领域服务：试算、签署、封账、重开、迟到分流、下钻。"""
from __future__ import annotations

from .digest import content_digest
from .errors import bad_request, conflict
from .models import (
    Correction,
    CorrectionKind,
    EventStatus,
    ReportStatus,
    ReportVersion,
    ServiceEvent,
    Signature,
)
from .store import Store

RATINGS = ("优秀", "良好", "合格")
PARTIES = ("school", "venue")


def _require_text(value: object, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise bad_request(f"缺少必填字段：{field}")
    return text


def compute_stats(events: list[ServiceEvent]) -> dict:
    """统计口径：仅已确认事件计入；时长按分钟汇总；主题覆盖为去重主题数；
    优秀评价为 rating=优秀 的事件数及其占比（千分比，避免浮点）。"""
    total_minutes = sum(e.minutes for e in events)
    themes = sorted({e.theme for e in events})
    excellent = sum(1 for e in events if e.rating == "优秀")
    count = len(events)
    return {
        "event_count": count,
        "total_minutes": total_minutes,
        "total_hours_text": f"{total_minutes // 60}小时{total_minutes % 60}分",
        "theme_coverage": themes,
        "theme_count": len(themes),
        "excellent_count": excellent,
        "excellent_per_mille": (excellent * 1000 // count) if count else 0,
        "volunteer_count": len({e.volunteer_id for e in events}),
    }


def build_caliber() -> dict:
    """统计口径说明，随报告冻结，供双方核对。"""
    return {
        "included": "统计周期内、状态为「已确认」的服务事件",
        "excluded": "待核验与已排除事件不计入，逐条列出原因",
        "hours": "时长以分钟求和，展示时折算为小时",
        "theme_coverage": "按主题去重统计覆盖面",
        "excellent": "评价为「优秀」的事件数及千分比",
    }


class ClosingService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ---- 事件汇集与核验 ----

    def submit_event(self, data: dict, *, now: str) -> ServiceEvent:
        event = ServiceEvent(
            event_id=_require_text(data.get("event_id"), "event_id"),
            volunteer_id=_require_text(data.get("volunteer_id"), "volunteer_id"),
            venue_id=_require_text(data.get("venue_id"), "venue_id"),
            school_id=_require_text(data.get("school_id"), "school_id"),
            theme=_require_text(data.get("theme"), "theme"),
            minutes=int(data.get("minutes") or 0),
            served_on=_require_text(data.get("served_on"), "served_on"),
            rating=_require_text(data.get("rating"), "rating"),
            note=str(data.get("note", "")),
        )
        if event.minutes <= 0:
            raise bad_request("minutes 必须为正整数")
        if event.rating not in RATINGS:
            raise bad_request(f"rating 必须是：{'、'.join(RATINGS)}")
        self.store.add_event(event)
        return event

    def verify_event(self, event_id: str, *, approve: bool, note: str = "") -> ServiceEvent:
        event = self.store.get_event(event_id)
        if event.status is not EventStatus.PENDING:
            raise conflict("event_finalized", f"事件已终态（{event.status.value}），不能重复核验")
        updated = ServiceEvent(
            **{**event.to_dict(), "status": EventStatus.CONFIRMED if approve else EventStatus.EXCLUDED,
               "note": note or event.note}
        )
        self.store.replace_event(updated)
        return updated

    # ---- 试算报告 ----

    def generate_trial(self, data: dict, *, now: str) -> ReportVersion:
        report_id = _require_text(data.get("report_id"), "report_id")
        school_id = _require_text(data.get("school_id"), "school_id")
        venue_id = _require_text(data.get("venue_id"), "venue_id")
        period_start = _require_text(data.get("period_start"), "period_start")
        period_end = _require_text(data.get("period_end"), "period_end")
        if period_start > period_end:
            raise bad_request("period_start 不能晚于 period_end")

        existing = self.store.versions_of(report_id)
        if existing and existing[-1].status is ReportStatus.DRAFT:
            raise conflict("draft_open", "已有试算中的版本，请先签署或封账")
        if existing and existing[-1].status is ReportStatus.CLOSED:
            raise conflict("closed", "已封账，须先经独立批准重开才能产生新版本")
        version = existing[-1].version + 1 if existing else 1

        included: list[ServiceEvent] = []
        excluded: list[dict] = []
        for event in self.store.all_events():
            if event.school_id != school_id or event.venue_id != venue_id:
                continue
            if not (period_start <= event.served_on <= period_end):
                continue
            if event.status is EventStatus.CONFIRMED:
                included.append(event)
            else:
                reason = "未通过核验" if event.status is EventStatus.EXCLUDED else "尚未完成核验"
                excluded.append({"event_id": event.event_id, "reason": reason})

        included.sort(key=lambda e: e.event_id)
        digest = content_digest({
            "report_id": report_id,
            "version": version,
            "period": [period_start, period_end],
            "included": [e.to_dict() for e in included],
            "excluded": excluded,
        })
        report = ReportVersion(
            report_id=report_id,
            version=version,
            period_start=period_start,
            period_end=period_end,
            school_id=school_id,
            venue_id=venue_id,
            included_ids=[e.event_id for e in included],
            excluded=excluded,
            stats=compute_stats(included),
            input_digest=digest,
            caliber=build_caliber(),
            created_at=now,
        )
        self.store.add_report(report)
        return report

    # ---- 签署与封账 ----

    def sign_report(self, report_id: str, data: dict, *, now: str) -> ReportVersion:
        report = self.store.get_report(report_id, data.get("version"))
        if report.status is not ReportStatus.DRAFT:
            raise conflict("not_draft", f"当前状态（{report.status.value}）不能签署")
        party = _require_text(data.get("party"), "party")
        if party not in PARTIES:
            raise bad_request("party 必须是 school 或 venue")
        if any(sig.party == party for sig in report.signatures):
            raise conflict("duplicate_sign", f"{party} 已签署过该版本")
        report.signatures.append(Signature(
            party=party,
            signer=_require_text(data.get("signer"), "signer"),
            signed_at=now,
            input_digest=report.input_digest,
        ))
        if {sig.party for sig in report.signatures} == set(PARTIES):
            report.status = ReportStatus.SIGNED
        self.store.replace_report(report)
        return report

    def close_report(self, report_id: str, data: dict | None = None, *, now: str) -> ReportVersion:
        report = self.store.get_report(report_id, (data or {}).get("version"))
        if report.status is ReportStatus.CLOSED:
            raise conflict("already_closed", "报告已封账")
        if report.status is not ReportStatus.SIGNED:
            raise conflict("unsigned", "学校与场馆双方签署后才能封账")
        for sig in report.signatures:
            if sig.input_digest != report.input_digest:
                raise conflict("digest_changed", "签署后输入摘要发生变化，禁止封账")
        report.status = ReportStatus.CLOSED
        report.closed_at = now
        self.store.replace_report(report)
        return report

    def reopen_report(self, report_id: str, data: dict, *, now: str) -> ReportVersion:
        """重开已封账报告：必须由未签署过该版本的独立批准人批准。"""
        report = self.store.get_report(report_id, data.get("version"))
        if report.status is not ReportStatus.CLOSED:
            raise conflict("not_closed", "仅已封账的报告可以重开")
        approver = _require_text(data.get("approver"), "approver")
        signers = {sig.signer for sig in report.signatures}
        if approver in signers:
            raise conflict("not_independent", "重开批准人必须独立于双方签署人")
        report.status = ReportStatus.REOPENED
        report.reopen_approval = {
            "approver": approver,
            "approved_at": now,
            "reason": _require_text(data.get("reason"), "reason"),
        }
        self.store.replace_report(report)
        return report

    # ---- 迟到记录分流 ----

    def route_late_event(self, data: dict, *, now: str) -> Correction:
        """封账后到达的事件：默认补录进下一版；显式 kind=更正 则开更正单。"""
        report = self.store.get_report(_require_text(data.get("report_id"), "report_id"),
                                       data.get("version"))
        if report.status is not ReportStatus.CLOSED:
            raise conflict("not_closed", "仅已封账的报告需要迟到分流")
        event = self.store.get_event(_require_text(data.get("event_id"), "event_id"))
        if event.event_id in report.included_ids:
            raise conflict("already_included", "事件已纳入该报告")
        kind_text = str(data.get("kind", CorrectionKind.LATE_ENTRY.value))
        try:
            kind = CorrectionKind(kind_text)
        except ValueError:
            raise bad_request("kind 必须是 补录 或 更正") from None
        target = report.version + 1 if kind is CorrectionKind.LATE_ENTRY else report.version
        correction = Correction(
            correction_id=_require_text(data.get("correction_id"), "correction_id"),
            kind=kind,
            event=event,
            reason=_require_text(data.get("reason"), "reason"),
            created_at=now,
            target_version=target,
        )
        self.store.add_correction(correction)
        return correction

    # ---- 下钻与导出 ----

    def drilldown(self, report_id: str, version: int | None = None) -> dict:
        """从汇总值下钻：返回被纳入与被排除的明细记录。"""
        report = self.store.get_report(report_id, version)
        included = [self.store.get_event(eid).to_dict() for eid in report.included_ids]
        excluded = [
            {**self.store.get_event(item["event_id"]).to_dict(), "excluded_reason": item["reason"]}
            for item in report.excluded
        ]
        return {
            "report_id": report.report_id,
            "version": report.version,
            "status": report.status.value,
            "stats": dict(report.stats),
            "included": included,
            "excluded": excluded,
        }

    def export_report(self, report_id: str, version: int | None = None, *,
                      chunk_size: int = 10) -> dict:
        """分块摘要导出：每个分块独立摘要，清单再摘要；同一版本重复导出结果一致。"""
        if chunk_size <= 0:
            raise bad_request("chunk_size 必须为正整数")
        report = self.store.get_report(report_id, version)
        included = [self.store.get_event(eid).to_dict() for eid in report.included_ids]
        excluded = [
            {**self.store.get_event(item["event_id"]).to_dict(), "excluded_reason": item["reason"]}
            for item in report.excluded
        ]
        records = included + excluded
        chunks = []
        for index in range(0, len(records), chunk_size):
            part = records[index:index + chunk_size]
            chunks.append({
                "seq": len(chunks) + 1,
                "record_count": len(part),
                "digest": content_digest(part),
            })
        manifest = {
            "report_id": report.report_id,
            "version": report.version,
            "status": report.status.value,
            "input_digest": report.input_digest,
            "stats": dict(report.stats),
            "caliber": dict(report.caliber),
            "chunk_size": chunk_size,
            "chunks": chunks,
        }
        manifest["manifest_digest"] = content_digest(manifest)
        return {"manifest": manifest, "records": records}
