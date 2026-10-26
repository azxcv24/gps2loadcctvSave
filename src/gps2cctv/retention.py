"""보존 기간 만료·삭제 요청 처리(Retention & Deletion).

- 사건을 지우면 경로 지점·후보·보존 요청이 함께 지워진다(외래 키 CASCADE).
- 삭제 후 관련 행이 남아 있지 않은지 다시 확인해 검증 결과를 돌려준다.
- SQLite ``secure_delete`` 가 켜져 있어 지워진 내용은 파일 안에서 0으로 덮어쓰인다.
- 이 프로그램은 자체 백업·캐시를 만들지 않는다. 사용자가 따로 만든 DB 복사본은
  이 기능으로 지울 수 없으므로 README에 안내한다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import audit
from .config import Settings
from .errors import NotFoundError
from .timeutil import to_db

_DEPENDENT_TABLES = ("route_point", "camera_candidate", "preservation_request")


@dataclass
class DeletionReport:
    incident_ids: list[str] = field(default_factory=list)
    route_points: int = 0
    candidates: int = 0
    preservation_requests: int = 0
    audit_events: int = 0
    leftovers: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def verified(self) -> bool:
        return not any(self.leftovers.values())


def _count(conn: sqlite3.Connection, table: str, ids: list[str]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    column = "id" if table == "incident" else "incident_id"
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", ids
    ).fetchone()[0]


def verify_deleted(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    """삭제한 사건과 연결된 행이 남아 있는지 표별로 센다(모두 0이어야 정상)."""
    return {table: _count(conn, table, ids) for table in ("incident", *_DEPENDENT_TABLES)}


def _delete(conn: sqlite3.Connection, ids: list[str], report: DeletionReport) -> None:
    report.incident_ids = list(ids)
    report.route_points = _count(conn, "route_point", ids)
    report.candidates = _count(conn, "camera_candidate", ids)
    report.preservation_requests = _count(conn, "preservation_request", ids)
    if report.dry_run or not ids:
        return
    marks = ",".join("?" * len(ids))
    with conn:
        conn.execute(f"DELETE FROM incident WHERE id IN ({marks})", ids)
    report.leftovers = verify_deleted(conn, ids)


def delete_incident(
    conn: sqlite3.Connection, incident_id: str, now: datetime, settings: Settings
) -> DeletionReport:
    """사용자 삭제 요청에 따라 사건과 관련 자료를 즉시 지운다."""
    exists = conn.execute("SELECT 1 FROM incident WHERE id = ?", (incident_id,)).fetchone()
    if exists is None:
        raise NotFoundError(f"사건을 찾을 수 없습니다: {incident_id}")
    report = DeletionReport()
    _delete(conn, [incident_id], report)
    audit.record(
        conn,
        action="incident.delete",
        resource_type="incident",
        resource_id=incident_id,
        outcome="success" if report.verified else "error",
        reason_code="user_request" if report.verified else "deletion_unverified",
        now=now,
        settings=settings,
        detail={
            "route_points": report.route_points,
            "candidates": report.candidates,
            "preservation_requests": report.preservation_requests,
        },
    )
    return report


def purge_expired(
    conn: sqlite3.Connection, now: datetime, settings: Settings, *, dry_run: bool = False
) -> DeletionReport:
    """보존 종료 시각이 지난 사건과 감사 로그를 지운다."""
    stamp = to_db(now)
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM incident WHERE retention_until <= ? ORDER BY id", (stamp,)
        )
    ]
    report = DeletionReport(dry_run=dry_run)
    _delete(conn, ids, report)
    report.audit_events = conn.execute(
        "SELECT COUNT(*) FROM audit_event WHERE retention_until <= ?", (stamp,)
    ).fetchone()[0]
    if dry_run:
        return report
    with conn:
        conn.execute("DELETE FROM audit_event WHERE retention_until <= ?", (stamp,))
    for incident_id in ids:
        audit.record(
            conn,
            action="incident.purge",
            resource_type="incident",
            resource_id=incident_id,
            outcome="success" if report.verified else "error",
            reason_code="retention_expired",
            now=now,
            settings=settings,
        )
    audit.record(
        conn,
        action="retention.purge",
        resource_type="incident",
        outcome="success" if report.verified else "error",
        reason_code="retention_expired" if report.verified else "deletion_unverified",
        now=now,
        settings=settings,
        detail={
            "incidents": len(ids),
            "route_points": report.route_points,
            "candidates": report.candidates,
            "preservation_requests": report.preservation_requests,
            "audit_events": report.audit_events,
        },
    )
    return report
