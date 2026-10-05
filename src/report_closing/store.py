"""内存存储：事件、报告版本、更正单。"""
from __future__ import annotations

from .errors import conflict, not_found
from .models import Correction, ReportVersion, ServiceEvent


class Store:
    """按主键保存领域对象；报告版本以 (report_id, version) 唯一标识。"""

    def __init__(self) -> None:
        self._events: dict[str, ServiceEvent] = {}
        self._reports: dict[tuple[str, int], ReportVersion] = {}
        self._corrections: dict[str, Correction] = {}

    # ---- 服务事件 ----

    def add_event(self, event: ServiceEvent) -> None:
        if event.event_id in self._events:
            raise conflict("event_exists", f"服务事件已存在：{event.event_id}")
        self._events[event.event_id] = event

    def get_event(self, event_id: str) -> ServiceEvent:
        try:
            return self._events[event_id]
        except KeyError:
            raise not_found(f"服务事件不存在：{event_id}") from None

    def replace_event(self, event: ServiceEvent) -> None:
        self.get_event(event.event_id)
        self._events[event.event_id] = event

    def all_events(self) -> list[ServiceEvent]:
        return [self._events[key] for key in sorted(self._events)]

    # ---- 报告版本 ----

    def add_report(self, report: ReportVersion) -> None:
        key = (report.report_id, report.version)
        if key in self._reports:
            raise conflict("report_exists", f"报告版本已存在：{report.report_id} v{report.version}")
        self._reports[key] = report

    def get_report(self, report_id: str, version: int | None = None) -> ReportVersion:
        if version is None:
            version = self.latest_version(report_id)
        try:
            return self._reports[(report_id, version)]
        except KeyError:
            raise not_found(f"报告不存在：{report_id} v{version}") from None

    def replace_report(self, report: ReportVersion) -> None:
        key = (report.report_id, report.version)
        if key not in self._reports:
            raise not_found(f"报告不存在：{report.report_id} v{report.version}")
        self._reports[key] = report

    def versions_of(self, report_id: str) -> list[ReportVersion]:
        versions = [r for (rid, _), r in self._reports.items() if rid == report_id]
        return sorted(versions, key=lambda r: r.version)

    def latest_version(self, report_id: str) -> int:
        versions = self.versions_of(report_id)
        if not versions:
            raise not_found(f"报告不存在：{report_id}")
        return versions[-1].version

    def all_reports(self) -> list[ReportVersion]:
        return sorted(self._reports.values(), key=lambda r: (r.report_id, r.version))

    # ---- 更正单 ----

    def add_correction(self, correction: Correction) -> None:
        if correction.correction_id in self._corrections:
            raise conflict("correction_exists", f"更正单已存在：{correction.correction_id}")
        self._corrections[correction.correction_id] = correction

    def get_correction(self, correction_id: str) -> Correction:
        try:
            return self._corrections[correction_id]
        except KeyError:
            raise not_found(f"更正单不存在：{correction_id}") from None

    def all_corrections(self) -> list[Correction]:
        return [self._corrections[key] for key in sorted(self._corrections)]
