"""SQLite 持久化层。

所有写操作都在 service 层开启的事务里执行；本层只负责 SQL 与行字典转换，
不自行提交，保证多步写入（如重开 = 封旧版 + 开新版）要么全部成功要么全部回滚。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_event (
    event_id     TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL,
    school_id    TEXT NOT NULL,
    venue_id     TEXT NOT NULL,
    theme        TEXT NOT NULL,
    service_date TEXT NOT NULL,
    minutes      INTEGER NOT NULL CHECK (minutes > 0),
    rating       REAL NOT NULL CHECK (rating >= 0 AND rating <= 5),
    state        TEXT NOT NULL,
    submitted_at TEXT,
    verified_at  TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
    report_id        TEXT PRIMARY KEY,
    school_id        TEXT NOT NULL,
    venue_id         TEXT NOT NULL,
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    state            TEXT NOT NULL,
    cutoff           TEXT NOT NULL,
    caliber_json     TEXT NOT NULL,
    input_digest     TEXT NOT NULL,
    aggregates_json  TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    sealed_at        TEXT,
    closed_by        TEXT,
    reopened_at      TEXT,
    reopen_approver  TEXT,
    reopen_reason    TEXT,
    supersedes       TEXT
);

CREATE TABLE IF NOT EXISTS report_item (
    report_id TEXT NOT NULL REFERENCES report(report_id),
    event_id  TEXT NOT NULL REFERENCES service_event(event_id),
    included  INTEGER NOT NULL,
    reason    TEXT,
    PRIMARY KEY (report_id, event_id)
);

CREATE TABLE IF NOT EXISTS signature (
    report_id TEXT NOT NULL REFERENCES report(report_id),
    party     TEXT NOT NULL,
    signer    TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (report_id, party)
);

CREATE TABLE IF NOT EXISTS correction_note (
    note_id      TEXT PRIMARY KEY,
    report_id    TEXT NOT NULL REFERENCES report(report_id),
    reason       TEXT NOT NULL,
    state        TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS correction_item (
    note_id  TEXT NOT NULL REFERENCES correction_note(note_id),
    event_id TEXT NOT NULL REFERENCES service_event(event_id),
    PRIMARY KEY (note_id, event_id)
);

CREATE TABLE IF NOT EXISTS correction_signature (
    note_id   TEXT NOT NULL REFERENCES correction_note(note_id),
    party     TEXT NOT NULL,
    signer    TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (note_id, party)
);
"""

ID_PREFIXES = {
    "service_event": "EVT",
    "report": "RPT",
    "correction_note": "COR",
}


class Store:
    """线程安全的 SQLite 存取层。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """开启一个事务：正常退出提交，抛异常回滚。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 通用 ----------

    def next_id(self, conn: sqlite3.Connection, table: str) -> str:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return f"{ID_PREFIXES[table]}-{row['n'] + 1:04d}"

    @staticmethod
    def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    # ---------- 服务事件 ----------

    def insert_event(self, conn: sqlite3.Connection, event: dict) -> None:
        conn.execute(
            """
            INSERT INTO service_event (
                event_id, volunteer_id, school_id, venue_id, theme, service_date,
                minutes, rating, state, submitted_at, verified_at, created_by, created_at
            ) VALUES (
                :event_id, :volunteer_id, :school_id, :venue_id, :theme, :service_date,
                :minutes, :rating, :state, :submitted_at, :verified_at, :created_by, :created_at
            )
            """,
            event,
        )

    def get_event(self, conn: sqlite3.Connection, event_id: str) -> dict | None:
        return self._one(conn, "SELECT * FROM service_event WHERE event_id = ?", (event_id,))

    def list_events(
        self,
        conn: sqlite3.Connection,
        school_id: str | None = None,
        venue_id: str | None = None,
        state: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM service_event WHERE 1=1"
        params: list[Any] = []
        if school_id:
            sql += " AND school_id = ?"
            params.append(school_id)
        if venue_id:
            sql += " AND venue_id = ?"
            params.append(venue_id)
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY event_id"
        return self._all(conn, sql, tuple(params))

    def events_in_scope(
        self,
        conn: sqlite3.Connection,
        school_id: str,
        venue_id: str,
        period_start: str,
        period_end: str,
    ) -> list[dict]:
        return self._all(
            conn,
            """
            SELECT * FROM service_event
            WHERE school_id = ? AND venue_id = ?
              AND service_date >= ? AND service_date <= ?
            ORDER BY event_id
            """,
            (school_id, venue_id, period_start, period_end),
        )

    def update_event_state(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        state: str,
        **fields: Any,
    ) -> None:
        assignments = ", ".join(["state = ?"] + [f"{key} = ?" for key in fields])
        conn.execute(
            f"UPDATE service_event SET {assignments} WHERE event_id = ?",
            (state, *fields.values(), event_id),
        )

    # ---------- 报告 ----------

    def insert_report(self, conn: sqlite3.Connection, report: dict) -> None:
        conn.execute(
            """
            INSERT INTO report (
                report_id, school_id, venue_id, period_start, period_end, version,
                state, cutoff, caliber_json, input_digest, aggregates_json,
                created_by, created_at, sealed_at, closed_by,
                reopened_at, reopen_approver, reopen_reason, supersedes
            ) VALUES (
                :report_id, :school_id, :venue_id, :period_start, :period_end, :version,
                :state, :cutoff, :caliber_json, :input_digest, :aggregates_json,
                :created_by, :created_at, :sealed_at, :closed_by,
                :reopened_at, :reopen_approver, :reopen_reason, :supersedes
            )
            """,
            report,
        )

    def get_report(self, conn: sqlite3.Connection, report_id: str) -> dict | None:
        return self._one(conn, "SELECT * FROM report WHERE report_id = ?", (report_id,))

    def list_reports(
        self,
        conn: sqlite3.Connection,
        school_id: str | None = None,
        venue_id: str | None = None,
        state: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM report WHERE 1=1"
        params: list[Any] = []
        if school_id:
            sql += " AND school_id = ?"
            params.append(school_id)
        if venue_id:
            sql += " AND venue_id = ?"
            params.append(venue_id)
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY school_id, venue_id, period_start, version"
        return self._all(conn, sql, tuple(params))

    def reports_for_scope(
        self,
        conn: sqlite3.Connection,
        school_id: str,
        venue_id: str,
        period_start: str,
        period_end: str,
    ) -> list[dict]:
        return self._all(
            conn,
            """
            SELECT * FROM report
            WHERE school_id = ? AND venue_id = ? AND period_start = ? AND period_end = ?
            ORDER BY version
            """,
            (school_id, venue_id, period_start, period_end),
        )

    def update_report(self, conn: sqlite3.Connection, report_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(f"UPDATE report SET {assignments} WHERE report_id = ?", (*fields.values(), report_id))

    # ---------- 报告条目（纳入/排除快照） ----------

    def replace_items(self, conn: sqlite3.Connection, report_id: str, items: list[dict]) -> None:
        conn.execute("DELETE FROM report_item WHERE report_id = ?", (report_id,))
        conn.executemany(
            "INSERT INTO report_item (report_id, event_id, included, reason) VALUES (?, ?, ?, ?)",
            [(report_id, item["event_id"], item["included"], item["reason"]) for item in items],
        )

    def list_items(self, conn: sqlite3.Connection, report_id: str) -> list[dict]:
        return self._all(
            conn,
            """
            SELECT i.report_id, i.event_id, i.included, i.reason, e.*
            FROM report_item i JOIN service_event e ON e.event_id = i.event_id
            WHERE i.report_id = ?
            ORDER BY i.event_id
            """,
            (report_id,),
        )

    # ---------- 报告签署 ----------

    def add_signature(self, conn: sqlite3.Connection, report_id: str, party: str, signer: str, signed_at: str) -> None:
        conn.execute(
            "INSERT INTO signature (report_id, party, signer, signed_at) VALUES (?, ?, ?, ?)",
            (report_id, party, signer, signed_at),
        )

    def list_signatures(self, conn: sqlite3.Connection, report_id: str) -> list[dict]:
        return self._all(conn, "SELECT * FROM signature WHERE report_id = ? ORDER BY party", (report_id,))

    def delete_signatures(self, conn: sqlite3.Connection, report_id: str) -> None:
        conn.execute("DELETE FROM signature WHERE report_id = ?", (report_id,))

    # ---------- 更正单 ----------

    def insert_note(self, conn: sqlite3.Connection, note: dict) -> None:
        conn.execute(
            """
            INSERT INTO correction_note (note_id, report_id, reason, state, created_by, created_at, confirmed_at)
            VALUES (:note_id, :report_id, :reason, :state, :created_by, :created_at, :confirmed_at)
            """,
            note,
        )

    def get_note(self, conn: sqlite3.Connection, note_id: str) -> dict | None:
        return self._one(conn, "SELECT * FROM correction_note WHERE note_id = ?", (note_id,))

    def list_notes_for_report(self, conn: sqlite3.Connection, report_id: str) -> list[dict]:
        return self._all(
            conn,
            "SELECT * FROM correction_note WHERE report_id = ? ORDER BY note_id",
            (report_id,),
        )

    def update_note(self, conn: sqlite3.Connection, note_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(f"UPDATE correction_note SET {assignments} WHERE note_id = ?", (*fields.values(), note_id))

    def set_note_items(self, conn: sqlite3.Connection, note_id: str, event_ids: list[str]) -> None:
        conn.executemany(
            "INSERT INTO correction_item (note_id, event_id) VALUES (?, ?)",
            [(note_id, event_id) for event_id in event_ids],
        )

    def list_note_items(self, conn: sqlite3.Connection, note_id: str) -> list[dict]:
        return self._all(
            conn,
            """
            SELECT i.note_id, i.event_id, e.*
            FROM correction_item i JOIN service_event e ON e.event_id = i.event_id
            WHERE i.note_id = ?
            ORDER BY i.event_id
            """,
            (note_id,),
        )

    def notes_containing_event(self, conn: sqlite3.Connection, report_id: str, event_id: str) -> list[dict]:
        return self._all(
            conn,
            """
            SELECT n.* FROM correction_note n
            JOIN correction_item i ON i.note_id = n.note_id
            WHERE n.report_id = ? AND i.event_id = ?
            ORDER BY n.note_id
            """,
            (report_id, event_id),
        )

    def add_note_signature(self, conn: sqlite3.Connection, note_id: str, party: str, signer: str, signed_at: str) -> None:
        conn.execute(
            "INSERT INTO correction_signature (note_id, party, signer, signed_at) VALUES (?, ?, ?, ?)",
            (note_id, party, signer, signed_at),
        )

    def list_note_signatures(self, conn: sqlite3.Connection, note_id: str) -> list[dict]:
        return self._all(conn, "SELECT * FROM correction_signature WHERE note_id = ? ORDER BY party", (note_id,))
