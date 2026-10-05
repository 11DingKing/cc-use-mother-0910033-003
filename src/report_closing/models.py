"""封账领域的核心数据模型。

状态机：
- 服务事件：待核验 -> 已确认 / 已排除（终态）
- 报告版本：试算中 -> 双方已签 -> 已封账（终态）；封账后可经独立批准重开为 已重开，
  其修正内容进入下一版试算或更正单。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EventStatus(str, enum.Enum):
    PENDING = "待核验"
    CONFIRMED = "已确认"
    EXCLUDED = "已排除"


class ReportStatus(str, enum.Enum):
    DRAFT = "试算中"
    SIGNED = "双方已签"
    CLOSED = "已封账"
    REOPENED = "已重开"


class CorrectionKind(str, enum.Enum):
    LATE_ENTRY = "补录"
    FIX = "更正"


@dataclass(frozen=True)
class ServiceEvent:
    """一条志愿服务事件。hours 以分钟存储，避免浮点误差。"""

    event_id: str
    volunteer_id: str
    venue_id: str
    school_id: str
    theme: str
    minutes: int
    served_on: str  # ISO 日期，YYYY-MM-DD
    rating: str  # 优秀 / 良好 / 合格
    status: EventStatus = EventStatus.PENDING
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "volunteer_id": self.volunteer_id,
            "venue_id": self.venue_id,
            "school_id": self.school_id,
            "theme": self.theme,
            "minutes": self.minutes,
            "served_on": self.served_on,
            "rating": self.rating,
            "status": self.status.value,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict) -> "ServiceEvent":
        return ServiceEvent(
            event_id=str(data["event_id"]),
            volunteer_id=str(data["volunteer_id"]),
            venue_id=str(data["venue_id"]),
            school_id=str(data["school_id"]),
            theme=str(data["theme"]),
            minutes=int(data["minutes"]),
            served_on=str(data["served_on"]),
            rating=str(data["rating"]),
            status=EventStatus(data.get("status", EventStatus.PENDING.value)),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class Signature:
    """一方对某个报告版本的签署。"""

    party: str  # school / venue
    signer: str
    signed_at: str  # ISO 时间戳
    input_digest: str  # 签署时看到的输入摘要，防止签后改数

    def to_dict(self) -> dict:
        return {
            "party": self.party,
            "signer": self.signer,
            "signed_at": self.signed_at,
            "input_digest": self.input_digest,
        }

    @staticmethod
    def from_dict(data: dict) -> "Signature":
        return Signature(
            party=str(data["party"]),
            signer=str(data["signer"]),
            signed_at=str(data["signed_at"]),
            input_digest=str(data["input_digest"]),
        )


@dataclass(frozen=True)
class Correction:
    """迟到记录分流：补录进入下一版，或以更正单修订已封账口径外的错误。"""

    correction_id: str
    kind: CorrectionKind
    event: ServiceEvent
    reason: str
    created_at: str
    target_version: int  # 补录指向的下一版本号；更正单指向被修订版本号

    def to_dict(self) -> dict:
        return {
            "correction_id": self.correction_id,
            "kind": self.kind.value,
            "event": self.event.to_dict(),
            "reason": self.reason,
            "created_at": self.created_at,
            "target_version": self.target_version,
        }

    @staticmethod
    def from_dict(data: dict) -> "Correction":
        return Correction(
            correction_id=str(data["correction_id"]),
            kind=CorrectionKind(data["kind"]),
            event=ServiceEvent.from_dict(data["event"]),
            reason=str(data["reason"]),
            created_at=str(data["created_at"]),
            target_version=int(data["target_version"]),
        )


@dataclass
class ReportVersion:
    """一个试算报告版本。封账后 included/excluded 快照与统计结果不可变。"""

    report_id: str
    version: int
    period_start: str
    period_end: str
    school_id: str
    venue_id: str
    status: ReportStatus = ReportStatus.DRAFT
    included_ids: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)  # [{"event_id", "reason"}]
    stats: dict = field(default_factory=dict)
    input_digest: str = ""
    caliber: dict = field(default_factory=dict)  # 统计口径说明
    signatures: list[Signature] = field(default_factory=list)
    reopen_approval: dict | None = None  # {"approver", "approved_at", "reason"}
    created_at: str = ""
    closed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "version": self.version,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "school_id": self.school_id,
            "venue_id": self.venue_id,
            "status": self.status.value,
            "included_ids": list(self.included_ids),
            "excluded": [dict(item) for item in self.excluded],
            "stats": dict(self.stats),
            "input_digest": self.input_digest,
            "caliber": dict(self.caliber),
            "signatures": [sig.to_dict() for sig in self.signatures],
            "reopen_approval": dict(self.reopen_approval) if self.reopen_approval else None,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "ReportVersion":
        return ReportVersion(
            report_id=str(data["report_id"]),
            version=int(data["version"]),
            period_start=str(data["period_start"]),
            period_end=str(data["period_end"]),
            school_id=str(data["school_id"]),
            venue_id=str(data["venue_id"]),
            status=ReportStatus(data["status"]),
            included_ids=[str(x) for x in data.get("included_ids", [])],
            excluded=[dict(item) for item in data.get("excluded", [])],
            stats=dict(data.get("stats", {})),
            input_digest=str(data.get("input_digest", "")),
            caliber=dict(data.get("caliber", {})),
            signatures=[Signature.from_dict(s) for s in data.get("signatures", [])],
            reopen_approval=(dict(data["reopen_approval"]) if data.get("reopen_approval") else None),
            created_at=str(data.get("created_at", "")),
            closed_at=str(data.get("closed_at", "")),
        )
