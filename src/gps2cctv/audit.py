"""감사 로그.

감사 로그에는 자유 텍스트를 넣지 않는다. 행위·자원 유형·자원 ID·결과·사유 코드와
정수/참거짓 값만 기록해 좌표, 사고 사유, 연락처, 인증 정보가 섞여 들어가지 않게 한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Settings
from .timeutil import from_db, to_db

LOCAL_ACTOR = "local-user"
OUTCOMES = frozenset({"success", "denied", "error"})

_CODE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")


@dataclass(frozen=True)
class AuditEvent:
    id: int
    created_at: datetime
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    reason_code: str | None
    detail: dict[str, int | bool]


def safe_id(value: str | None) -> str | None:
    """감사 로그에 넣어도 되는 형식의 ID만 돌려준다(그 밖의 입력은 기록하지 않음)."""
    return value if value is not None and _ID_RE.match(value) else None


def _check_code(value: str, label: str) -> None:
    if not _CODE_RE.match(value):
        raise ValueError(f"감사 로그 {label} 형식이 올바르지 않습니다: {value!r}")


def _clean_detail(detail: dict[str, int | bool] | None) -> str | None:
    if not detail:
        return None
    for key, value in detail.items():
        _check_code(key, "detail 키")
        if not isinstance(value, (int, bool)):
            raise TypeError("감사 로그 detail 값은 정수 또는 참/거짓만 허용합니다.")
    return json.dumps(detail, sort_keys=True)


def record(
    conn: sqlite3.Connection,
    *,
    action: str,
    resource_type: str,
    now: datetime,
    settings: Settings,
    resource_id: str | None = None,
    outcome: str = "success",
    reason_code: str | None = None,
    detail: dict[str, int | bool] | None = None,
    actor_id: str = LOCAL_ACTOR,
) -> None:
    _check_code(action, "action")
    _check_code(resource_type, "resource_type")
    if outcome not in OUTCOMES:
        raise ValueError(f"감사 로그 outcome 값이 올바르지 않습니다: {outcome!r}")
    if reason_code is not None:
        _check_code(reason_code, "reason_code")
    if resource_id is not None and not _ID_RE.match(resource_id):
        raise ValueError("감사 로그 resource_id 형식이 올바르지 않습니다.")
    if not _ID_RE.match(actor_id):
        raise ValueError("감사 로그 actor_id 형식이 올바르지 않습니다.")
    retention_until = now + timedelta(days=settings.audit_retention_days)
    with conn:
        conn.execute(
            """
            INSERT INTO audit_event(created_at, actor_id, action, resource_type, resource_id,
                                    outcome, reason_code, detail, retention_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                to_db(now),
                actor_id,
                action,
                resource_type,
                resource_id,
                outcome,
                reason_code,
                _clean_detail(detail),
                to_db(retention_until),
            ),
        )


def list_events(
    conn: sqlite3.Connection, *, limit: int = 50, resource_id: str | None = None
) -> list[AuditEvent]:
    query = "SELECT * FROM audit_event"
    params: list[object] = []
    if resource_id is not None:
        query += " WHERE resource_id = ?"
        params.append(resource_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    events = []
    for row in conn.execute(query, params):
        events.append(
            AuditEvent(
                id=row["id"],
                created_at=from_db(row["created_at"]),
                actor_id=row["actor_id"],
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                outcome=row["outcome"],
                reason_code=row["reason_code"],
                detail=json.loads(row["detail"]) if row["detail"] else {},
            )
        )
    return events
