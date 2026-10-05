"""志愿贡献报告封账的核心业务规则。

不变量（对应 domain/contract.json）：

- 贡献输入摘要：试算报告携带对纳入事件集合敏感的 input_digest，输入变则摘要变；
- 双方签署封账：学校与场馆双方（且非同一人）签署后才能封账，封账后报告不可变；
- 迟到记录分流：统计截止后核验的记录不改动已封账报告，只能进入下一版（重开须
  独立批准）或更正单（双方确认）；
- 汇总下钻解释：报告保存纳入/排除快照，任何汇总值都能下钻到记录及排除原因。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from .digest import chunk_digests, input_digest, sha256_hex, canonical_bytes
from .errors import DomainError, not_found, require
from .store import Store

# 事件状态机：草拟 -> 待核验 -> 已确认
EVENT_DRAFT = "草拟"
EVENT_PENDING = "待核验"
EVENT_VERIFIED = "已确认"

# 报告状态机：试算 -> 已封账 -> 已重开（由后继版本接替）
REPORT_TRIAL = "试算"
REPORT_SEALED = "已封账"
REPORT_REOPENED = "已重开"

# 更正单状态机：草拟 -> 已确认 -> 已归档
NOTE_DRAFT = "草拟"
NOTE_CONFIRMED = "已确认"
NOTE_ARCHIVED = "已归档"

# 封账双方
PARTIES = ("学校", "场馆")

REASON_LATE = "迟到记录：核验时间晚于统计截止"
DEFAULT_EXCELLENT_MIN_RATING = 4.5


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_dt(value: Any, field: str) -> str:
    """把输入时间规范为 UTC 秒级 ISO 字符串，保证字典序即时间序。"""
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise DomainError("validation", f"{field} 不是合法时间：{value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_date(value: Any, field: str) -> str:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except (ValueError, TypeError):
        raise DomainError("validation", f"{field} 不是合法日期（YYYY-MM-DD）：{value!r}")
    return str(value)


def _require_text(value: Any, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    require(bool(text), "validation", f"{field} 不能为空")
    return text


class ReportClosingService:
    """封账领域服务。clock 可注入以便测试复现确定的时间序列。"""

    def __init__(self, store: Store, clock: Callable[[], str] = utcnow_iso) -> None:
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------------
    # 服务事件：汇集经过核验的服务事件
    # ------------------------------------------------------------------

    def create_event(
        self,
        *,
        volunteer_id: str,
        school_id: str,
        venue_id: str,
        theme: str,
        service_date: str,
        minutes: Any,
        rating: Any,
        actor: str,
    ) -> dict:
        actor = _require_text(actor, "操作人")
        event = {
            "volunteer_id": _require_text(volunteer_id, "志愿者"),
            "school_id": _require_text(school_id, "学校"),
            "venue_id": _require_text(venue_id, "场馆"),
            "theme": _require_text(theme, "服务主题"),
            "service_date": _norm_date(service_date, "服务日期"),
            "minutes": self._norm_minutes(minutes),
            "rating": self._norm_rating(rating),
            "state": EVENT_DRAFT,
            "submitted_at": None,
            "verified_at": None,
            "created_by": actor,
            "created_at": self.clock(),
        }
        with self.store.tx() as conn:
            event["event_id"] = self.store.next_id(conn, "service_event")
            self.store.insert_event(conn, event)
        return event

    @staticmethod
    def _norm_minutes(value: Any) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise DomainError("validation", f"服务时长（分钟）必须是整数：{value!r}")
        require(minutes > 0, "validation", "服务时长（分钟）必须大于 0")
        return minutes

    @staticmethod
    def _norm_rating(value: Any) -> float:
        try:
            rating = float(value)
        except (TypeError, ValueError):
            raise DomainError("validation", f"评价分必须是数字：{value!r}")
        require(0 <= rating <= 5, "validation", "评价分必须在 0 到 5 之间")
        return rating

    def submit_event(self, event_id: str, *, actor: str) -> dict:
        """草拟 -> 待核验。"""
        _require_text(actor, "操作人")
        with self.store.tx() as conn:
            event = self._event_or_404(conn, event_id)
            require(
                event["state"] == EVENT_DRAFT,
                "invalid_state",
                f"只有草拟状态的事件可以提交核验（当前：{event['state']}）",
                409,
            )
            self.store.update_event_state(conn, event_id, EVENT_PENDING, submitted_at=self.clock())
            return self.store.get_event(conn, event_id)

    def verify_event(self, event_id: str, *, actor: str) -> dict:
        """待核验 -> 已确认；核验时间决定事件是否构成迟到记录。"""
        _require_text(actor, "操作人")
        with self.store.tx() as conn:
            event = self._event_or_404(conn, event_id)
            require(
                event["state"] == EVENT_PENDING,
                "invalid_state",
                f"只有待核验状态的事件可以确认（当前：{event['state']}）",
                409,
            )
            self.store.update_event_state(conn, event_id, EVENT_VERIFIED, verified_at=self.clock())
            return self.store.get_event(conn, event_id)

    def get_event(self, event_id: str) -> dict:
        with self.store.tx() as conn:
            return self._event_or_404(conn, event_id)

    def list_events(self, *, school_id=None, venue_id=None, state=None) -> list[dict]:
        with self.store.tx() as conn:
            return self.store.list_events(conn, school_id=school_id, venue_id=venue_id, state=state)

    def _event_or_404(self, conn, event_id: str) -> dict:
        event = self.store.get_event(conn, event_id)
        if event is None:
            raise not_found("服务事件", event_id)
        return event

    # ------------------------------------------------------------------
    # 试算报告：带输入摘要与统计口径
    # ------------------------------------------------------------------

    def create_report(
        self,
        *,
        school_id: str,
        venue_id: str,
        period_start: str,
        period_end: str,
        actor: str,
        cutoff: str | None = None,
        excellent_min_rating: float = DEFAULT_EXCELLENT_MIN_RATING,
    ) -> dict:
        """为（学校, 场馆, 周期）生成第 1 版试算报告。

        同一范围只允许存在一个未被封账更替的活动报告；封账后要出新版必须走重开。
        """
        actor = _require_text(actor, "操作人")
        school_id = _require_text(school_id, "学校")
        venue_id = _require_text(venue_id, "场馆")
        period_start = _norm_date(period_start, "周期开始")
        period_end = _norm_date(period_end, "周期结束")
        require(period_start <= period_end, "validation", "周期开始不能晚于周期结束")
        cutoff = _norm_dt(cutoff, "统计截止") if cutoff else self.clock()
        excellent_min_rating = self._norm_rating(excellent_min_rating)

        with self.store.tx() as conn:
            active = [
                row
                for row in self.store.reports_for_scope(conn, school_id, venue_id, period_start, period_end)
                if row["state"] in (REPORT_TRIAL, REPORT_SEALED)
            ]
            if active:
                raise DomainError(
                    "scope_locked",
                    f"该范围已存在活动报告 {active[0]['report_id']}（{active[0]['state']}）；"
                    "如需出新版，请先申请独立批准重开",
                    409,
                )
            report = self._build_report(
                conn,
                school_id=school_id,
                venue_id=venue_id,
                period_start=period_start,
                period_end=period_end,
                version=1,
                cutoff=cutoff,
                excellent_min_rating=excellent_min_rating,
                actor=actor,
                supersedes=None,
            )
            return self._report_view(conn, report["report_id"])

    def _build_report(self, conn, *, school_id, venue_id, period_start, period_end,
                      version, cutoff, excellent_min_rating, actor, supersedes) -> dict:
        snapshot = self._snapshot(
            conn,
            school_id=school_id,
            venue_id=venue_id,
            period_start=period_start,
            period_end=period_end,
            cutoff=cutoff,
            excellent_min_rating=excellent_min_rating,
        )
        report = {
            "report_id": self.store.next_id(conn, "report"),
            "school_id": school_id,
            "venue_id": venue_id,
            "period_start": period_start,
            "period_end": period_end,
            "version": version,
            "state": REPORT_TRIAL,
            "cutoff": cutoff,
            "caliber_json": json.dumps(snapshot["caliber"], ensure_ascii=False, sort_keys=True),
            "input_digest": snapshot["input_digest"],
            "aggregates_json": json.dumps(snapshot["aggregates"], ensure_ascii=False, sort_keys=True),
            "created_by": actor,
            "created_at": self.clock(),
            "sealed_at": None,
            "closed_by": None,
            "reopened_at": None,
            "reopen_approver": None,
            "reopen_reason": None,
            "supersedes": supersedes,
        }
        self.store.insert_report(conn, report)
        self.store.replace_items(conn, report["report_id"], snapshot["items"])
        return report

    def _snapshot(self, conn, *, school_id, venue_id, period_start, period_end,
                  cutoff, excellent_min_rating) -> dict:
        """按统计口径对范围内事件做纳入/排除判定并汇总。"""
        events = self.store.events_in_scope(conn, school_id, venue_id, period_start, period_end)
        items: list[dict] = []
        included: list[dict] = []
        for event in events:
            if event["state"] != EVENT_VERIFIED:
                reason = f"状态未确认（当前：{event['state']}）"
                items.append({"event_id": event["event_id"], "included": 0, "reason": reason})
            elif event["verified_at"] > cutoff:
                items.append({"event_id": event["event_id"], "included": 0, "reason": REASON_LATE})
            else:
                items.append({"event_id": event["event_id"], "included": 1, "reason": None})
                included.append(event)
        caliber = {
            "include_states": [EVENT_VERIFIED],
            "verified_cutoff": cutoff,
            "excellent_min_rating": excellent_min_rating,
            "scope": {
                "school_id": school_id,
                "venue_id": venue_id,
                "period_start": period_start,
                "period_end": period_end,
            },
            "generated_at": self.clock(),
        }
        return {
            "items": items,
            "caliber": caliber,
            "aggregates": self._aggregate(included, excellent_min_rating),
            "input_digest": input_digest(included),
        }

    @staticmethod
    def _aggregate(included: list[dict], excellent_min_rating: float) -> dict:
        by_theme: dict[str, dict] = {}
        for event in included:
            bucket = by_theme.setdefault(
                event["theme"],
                {"theme": event["theme"], "event_count": 0, "total_minutes": 0, "excellent_count": 0},
            )
            bucket["event_count"] += 1
            bucket["total_minutes"] += event["minutes"]
            if event["rating"] >= excellent_min_rating:
                bucket["excellent_count"] += 1
        themes = sorted(by_theme.values(), key=lambda item: item["theme"])
        total_minutes = sum(event["minutes"] for event in included)
        return {
            "event_count": len(included),
            "total_minutes": total_minutes,
            "total_hours": round(total_minutes / 60, 2),
            "excellent_count": sum(bucket["excellent_count"] for bucket in themes),
            "volunteer_count": len({event["volunteer_id"] for event in included}),
            "by_theme": themes,
        }

    def refresh_report(self, report_id: str, *, actor: str, cutoff: str | None = None) -> dict:
        """重算试算报告。输入集合可能变化，因此已有签署一律作废。"""
        _require_text(actor, "操作人")
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            require(report["state"] == REPORT_TRIAL, "invalid_state",
                    f"只有试算状态的报告可以重算（当前：{report['state']}）", 409)
            cutoff = _norm_dt(cutoff, "统计截止") if cutoff else self.clock()
            caliber = json.loads(report["caliber_json"])
            snapshot = self._snapshot(
                conn,
                school_id=report["school_id"],
                venue_id=report["venue_id"],
                period_start=report["period_start"],
                period_end=report["period_end"],
                cutoff=cutoff,
                excellent_min_rating=caliber["excellent_min_rating"],
            )
            self.store.update_report(
                conn,
                report_id,
                cutoff=cutoff,
                caliber_json=json.dumps(snapshot["caliber"], ensure_ascii=False, sort_keys=True),
                input_digest=snapshot["input_digest"],
                aggregates_json=json.dumps(snapshot["aggregates"], ensure_ascii=False, sort_keys=True),
            )
            self.store.replace_items(conn, report_id, snapshot["items"])
            self.store.delete_signatures(conn, report_id)
            return self._report_view(conn, report_id)

    # ------------------------------------------------------------------
    # 双方签署封账
    # ------------------------------------------------------------------

    def sign_report(self, report_id: str, *, party: str, signer: str) -> dict:
        party = _require_text(party, "签署方")
        signer = _require_text(signer, "签署人")
        require(party in PARTIES, "validation", f"签署方必须是：{'、'.join(PARTIES)}")
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            require(report["state"] == REPORT_TRIAL, "invalid_state",
                    f"只有试算状态的报告可以签署（当前：{report['state']}）", 409)
            signatures = self.store.list_signatures(conn, report_id)
            require(
                all(sig["party"] != party for sig in signatures),
                "duplicate_signature",
                f"{party}方已签署过报告 {report_id}",
                409,
            )
            require(
                all(sig["signer"] != signer for sig in signatures),
                "signer_conflict",
                "同一签署人不能同时代表学校与场馆双方",
                409,
            )
            self.store.add_signature(conn, report_id, party, signer, self.clock())
            return self._report_view(conn, report_id)

    def close_report(self, report_id: str, *, actor: str) -> dict:
        """双方签署齐全后封账；封账后报告及汇总不可再变。"""
        actor = _require_text(actor, "操作人")
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            require(report["state"] == REPORT_TRIAL, "invalid_state",
                    f"只有试算状态的报告可以封账（当前：{report['state']}）", 409)
            signed_parties = {sig["party"] for sig in self.store.list_signatures(conn, report_id)}
            missing = [party for party in PARTIES if party not in signed_parties]
            require(not missing, "missing_signatures",
                    f"封账需要双方签署，尚缺：{'、'.join(missing)}", 409)
            self.store.update_report(
                conn, report_id, state=REPORT_SEALED, sealed_at=self.clock(), closed_by=actor
            )
            return self._report_view(conn, report_id)

    def reopen_report(self, report_id: str, *, approver: str, reason: str) -> dict:
        """重开封账报告：必须由未参与签署与封账操作的独立批准人批准。

        批准后原报告标记为已重开（保持只读可查），并生成下一版试算报告，
        迟到记录在新一版中按新截止时间重新判定。
        """
        approver = _require_text(approver, "批准人")
        reason = _require_text(reason, "重开理由")
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            require(report["state"] == REPORT_SEALED, "invalid_state",
                    f"只有已封账的报告可以重开（当前：{report['state']}）", 409)
            insiders = {sig["signer"] for sig in self.store.list_signatures(conn, report_id)}
            insiders.add(report["closed_by"])
            require(
                approver not in insiders,
                "not_independent_approver",
                "重开批准人必须独立于该报告的签署人与封账操作人",
                409,
            )
            now = self.clock()
            self.store.update_report(
                conn, report_id, state=REPORT_REOPENED,
                reopened_at=now, reopen_approver=approver, reopen_reason=reason,
            )
            caliber = json.loads(report["caliber_json"])
            successor = self._build_report(
                conn,
                school_id=report["school_id"],
                venue_id=report["venue_id"],
                period_start=report["period_start"],
                period_end=report["period_end"],
                version=report["version"] + 1,
                cutoff=now,
                excellent_min_rating=caliber["excellent_min_rating"],
                actor=approver,
                supersedes=report_id,
            )
            return self._report_view(conn, successor["report_id"])

    # ------------------------------------------------------------------
    # 更正单：迟到记录分流的去向之一
    # ------------------------------------------------------------------

    def create_correction(self, report_id: str, *, actor: str, reason: str, event_ids: list[str]) -> dict:
        """针对已封账报告登记更正单，补录未纳入的事件（典型为迟到记录）。

        更正单不改动已封账报告的数值，双方确认后成为报告的正式附件。
        """
        actor = _require_text(actor, "操作人")
        reason = _require_text(reason, "更正理由")
        require(isinstance(event_ids, list) and event_ids, "validation", "更正单至少包含一条事件")
        require(len(set(event_ids)) == len(event_ids), "validation", "更正单内事件编号不能重复")
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            require(report["state"] == REPORT_SEALED, "invalid_state",
                    f"只有已封账的报告可以登记更正单（当前：{report['state']}）", 409)
            included_ids = {
                item["event_id"] for item in self.store.list_items(conn, report_id) if item["included"]
            }
            for event_id in event_ids:
                event = self.store.get_event(conn, event_id)
                if event is None:
                    raise not_found("服务事件", event_id)
                in_scope = (
                    event["school_id"] == report["school_id"]
                    and event["venue_id"] == report["venue_id"]
                    and report["period_start"] <= event["service_date"] <= report["period_end"]
                )
                require(in_scope, "out_of_scope",
                        f"事件 {event_id} 不在报告 {report_id} 的学校/场馆/周期范围内", 409)
                require(
                    event_id not in included_ids,
                    "already_included",
                    f"事件 {event_id} 已被报告 {report_id} 纳入，无需更正",
                    409,
                )
            note = {
                "note_id": self.store.next_id(conn, "correction_note"),
                "report_id": report_id,
                "reason": reason,
                "state": NOTE_DRAFT,
                "created_by": actor,
                "created_at": self.clock(),
                "confirmed_at": None,
            }
            self.store.insert_note(conn, note)
            self.store.set_note_items(conn, note["note_id"], sorted(event_ids))
            return self._note_view(conn, note["note_id"])

    def confirm_correction(self, note_id: str, *, party: str, signer: str) -> dict:
        """更正单双方确认；双方签署齐全后更正单生效。"""
        party = _require_text(party, "确认方")
        signer = _require_text(signer, "确认人")
        require(party in PARTIES, "validation", f"确认方必须是：{'、'.join(PARTIES)}")
        with self.store.tx() as conn:
            note = self._note_or_404(conn, note_id)
            require(note["state"] == NOTE_DRAFT, "invalid_state",
                    f"只有草拟状态的更正单可以确认（当前：{note['state']}）", 409)
            signatures = self.store.list_note_signatures(conn, note_id)
            require(
                all(sig["party"] != party for sig in signatures),
                "duplicate_signature",
                f"{party}方已确认过更正单 {note_id}",
                409,
            )
            require(
                all(sig["signer"] != signer for sig in signatures),
                "signer_conflict",
                "同一确认人不能同时代表学校与场馆双方",
                409,
            )
            self.store.add_note_signature(conn, note_id, party, signer, self.clock())
            if {sig["party"] for sig in self.store.list_note_signatures(conn, note_id)} == set(PARTIES):
                self.store.update_note(conn, note_id, state=NOTE_CONFIRMED, confirmed_at=self.clock())
            return self._note_view(conn, note_id)

    def archive_correction(self, note_id: str, *, actor: str) -> dict:
        _require_text(actor, "操作人")
        with self.store.tx() as conn:
            note = self._note_or_404(conn, note_id)
            require(note["state"] == NOTE_CONFIRMED, "invalid_state",
                    f"只有已确认的更正单可以归档（当前：{note['state']}）", 409)
            self.store.update_note(conn, note_id, state=NOTE_ARCHIVED)
            return self._note_view(conn, note_id)

    def list_corrections(self, report_id: str) -> list[dict]:
        with self.store.tx() as conn:
            self._report_or_404(conn, report_id)
            return [self._note_view(conn, note["note_id"])
                    for note in self.store.list_notes_for_report(conn, report_id)]

    def get_correction(self, note_id: str) -> dict:
        with self.store.tx() as conn:
            return self._note_view(conn, note_id)

    # ------------------------------------------------------------------
    # 汇总下钻与迟到分流去向
    # ------------------------------------------------------------------

    def drilldown(self, report_id: str, *, theme=None, excellent=None, included=None) -> dict:
        """从汇总值下钻到记录：返回符合条件的纳入记录与排除记录（含原因）。"""
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            caliber = json.loads(report["caliber_json"])
            items = self.store.list_items(conn, report_id)
            if theme is not None:
                items = [item for item in items if item["theme"] == theme]
            if excellent is not None:
                threshold = caliber["excellent_min_rating"]
                items = [item for item in items if (item["rating"] >= threshold) == excellent]
            included_items = [item for item in items if item["included"]]
            excluded_items = [item for item in items if not item["included"]]
            result = {
                "report_id": report_id,
                "filter": {"theme": theme, "excellent": excellent},
                "included": {
                    "count": len(included_items),
                    "total_minutes": sum(item["minutes"] for item in included_items),
                    "records": [self._item_view(item) for item in included_items],
                },
                "excluded": {
                    "count": len(excluded_items),
                    "records": [self._item_view(item) for item in excluded_items],
                },
            }
            if included is True:
                result["excluded"] = {"count": len(excluded_items), "records": []}
            elif included is False:
                result["included"] = {"count": len(included_items), "total_minutes": 0, "records": []}
            return result

    def late_records(self, report_id: str) -> dict:
        """列出报告的迟到记录及其分流去向：下一版、更正单，或尚未分流。

        迟到判定实时进行：范围内已确认但核验时间晚于报告截止的事件都算迟到，
        包括封账后才补录核验、因而未进入报告条目快照的事件。
        """
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            events = self.store.events_in_scope(
                conn, report["school_id"], report["venue_id"],
                report["period_start"], report["period_end"],
            )
            late = [
                event for event in events
                if event["state"] == EVENT_VERIFIED and event["verified_at"] > report["cutoff"]
            ]
            later_versions = [
                row for row in self.store.reports_for_scope(
                    conn, report["school_id"], report["venue_id"],
                    report["period_start"], report["period_end"],
                )
                if row["version"] > report["version"]
            ]
            entries = []
            for event in late:
                routing = []
                for note in self.store.notes_containing_event(conn, report_id, event["event_id"]):
                    routing.append({"kind": "更正单", "ref": note["note_id"], "state": note["state"]})
                for version in later_versions:
                    version_items = self.store.list_items(conn, version["report_id"])
                    hit = next((row for row in version_items if row["event_id"] == event["event_id"]), None)
                    if hit and hit["included"]:
                        routing.append({"kind": "下一版", "ref": version["report_id"], "state": version["state"]})
                entries.append({
                    "event_id": event["event_id"],
                    "verified_at": event["verified_at"],
                    "cutoff": report["cutoff"],
                    "routing": routing or [{"kind": "未分流", "ref": None, "state": None}],
                })
            return {"report_id": report_id, "late_count": len(entries), "records": entries}

    # ------------------------------------------------------------------
    # 导出：分块摘要，重复下载字节稳定
    # ------------------------------------------------------------------

    def export_report(self, report_id: str) -> dict:
        """生成确定性导出文档。

        文档只引用已落库的数据（不含导出时刻），序列化采用规范化 JSON，
        因此同一报告重复下载得到字节一致的内容；纳入与排除记录分别分块
        并给出每块摘要，整体再给出 export_digest 便于校验。
        """
        with self.store.tx() as conn:
            report = self._report_or_404(conn, report_id)
            items = self.store.list_items(conn, report_id)
            included = [item for item in items if item["included"]]
            excluded = [item for item in items if not item["included"]]
            header = {
                "report_id": report["report_id"],
                "version": report["version"],
                "state": report["state"],
                "school_id": report["school_id"],
                "venue_id": report["venue_id"],
                "period_start": report["period_start"],
                "period_end": report["period_end"],
                "cutoff": report["cutoff"],
                "caliber": json.loads(report["caliber_json"]),
                "input_digest": report["input_digest"],
                "aggregates": json.loads(report["aggregates_json"]),
                "created_by": report["created_by"],
                "created_at": report["created_at"],
                "sealed_at": report["sealed_at"],
                "closed_by": report["closed_by"],
                "supersedes": report["supersedes"],
                "signatures": self.store.list_signatures(conn, report_id),
            }
            included_chunks = chunk_digests(included)
            excluded_chunks = chunk_digests(excluded)
            export_digest = sha256_hex(canonical_bytes({
                "report": header,
                "included_chunks": included_chunks,
                "excluded_chunks": excluded_chunks,
            }))
            return {
                "format": "volunteer-contribution-report",
                "format_version": 1,
                "report": header,
                "included_chunks": included_chunks,
                "excluded_chunks": excluded_chunks,
                "records": {
                    "included": [self._item_view(item) for item in included],
                    "excluded": [self._item_view(item) for item in excluded],
                },
                "export_digest": export_digest,
            }

    # ------------------------------------------------------------------
    # 查询与视图
    # ------------------------------------------------------------------

    def get_report(self, report_id: str) -> dict:
        with self.store.tx() as conn:
            return self._report_view(conn, report_id)

    def list_reports(self, *, school_id=None, venue_id=None, state=None) -> list[dict]:
        with self.store.tx() as conn:
            return [
                self._report_view(conn, row["report_id"])
                for row in self.store.list_reports(conn, school_id=school_id, venue_id=venue_id, state=state)
            ]

    def _report_or_404(self, conn, report_id: str) -> dict:
        report = self.store.get_report(conn, report_id)
        if report is None:
            raise not_found("报告", report_id)
        return report

    def _note_or_404(self, conn, note_id: str) -> dict:
        note = self.store.get_note(conn, note_id)
        if note is None:
            raise not_found("更正单", note_id)
        return note

    def _report_view(self, conn, report_id: str) -> dict:
        report = self._report_or_404(conn, report_id)
        items = self.store.list_items(conn, report_id)
        return {
            "report_id": report["report_id"],
            "school_id": report["school_id"],
            "venue_id": report["venue_id"],
            "period_start": report["period_start"],
            "period_end": report["period_end"],
            "version": report["version"],
            "state": report["state"],
            "cutoff": report["cutoff"],
            "caliber": json.loads(report["caliber_json"]),
            "input_digest": report["input_digest"],
            "aggregates": json.loads(report["aggregates_json"]),
            "created_by": report["created_by"],
            "created_at": report["created_at"],
            "sealed_at": report["sealed_at"],
            "closed_by": report["closed_by"],
            "reopened_at": report["reopened_at"],
            "reopen_approver": report["reopen_approver"],
            "reopen_reason": report["reopen_reason"],
            "supersedes": report["supersedes"],
            "signatures": self.store.list_signatures(conn, report_id),
            "item_counts": {
                "included": sum(1 for item in items if item["included"]),
                "excluded": sum(1 for item in items if not item["included"]),
            },
        }

    def _note_view(self, conn, note_id: str) -> dict:
        note = self._note_or_404(conn, note_id)
        return {
            "note_id": note["note_id"],
            "report_id": note["report_id"],
            "reason": note["reason"],
            "state": note["state"],
            "created_by": note["created_by"],
            "created_at": note["created_at"],
            "confirmed_at": note["confirmed_at"],
            "events": [self._item_view(item) for item in self.store.list_note_items(conn, note_id)],
            "signatures": self.store.list_note_signatures(conn, note_id),
        }

    @staticmethod
    def _item_view(item: dict) -> dict:
        return {
            "event_id": item["event_id"],
            "volunteer_id": item["volunteer_id"],
            "school_id": item["school_id"],
            "venue_id": item["venue_id"],
            "theme": item["theme"],
            "service_date": item["service_date"],
            "minutes": item["minutes"],
            "rating": item["rating"],
            "state": item["state"],
            "verified_at": item["verified_at"],
            "included": bool(item["included"]) if "included" in item else None,
            "reason": item.get("reason"),
        }
