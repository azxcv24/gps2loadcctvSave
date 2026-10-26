"""공식 보존 요청 준비와 상태 관리(Preservation Request Service).

이 모듈은 요청을 제공처에 직접 제출하지 않는다. 사용자가 제공처의 공식 절차로 제출할 수
있도록 요청 초안을 만들고, 사용자가 알려준 제출·응답 상태와 참조번호만 기록한다.
연락처 등 요청인 개인정보는 저장하지 않으며 초안에 직접 기재하도록 비워 둔다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import DISCLAIMER, audit
from .candidates import CONFIDENCE_TEXT, REASON_TEXT, get_candidate
from .catalog import get_camera, get_source
from .config import Settings
from .errors import Gps2CctvError, NotFoundError, PolicyDeniedError
from .incidents import get_incident
from .policy import check_policy
from .timeutil import fmt_local, from_db, to_db

ACTIVE_STATUSES = frozenset({"draft", "submitted", "acknowledged"})
TERMINAL_STATUSES = frozenset({"fulfilled", "rejected", "expired", "withdrawn"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "withdrawn"}),
    "submitted": frozenset({"acknowledged", "fulfilled", "rejected", "expired", "withdrawn"}),
    "acknowledged": frozenset({"fulfilled", "rejected", "expired", "withdrawn"}),
}
STATUS_TEXT = {
    "draft": "초안",
    "submitted": "제출함",
    "acknowledged": "접수됨",
    "fulfilled": "보존·제공 완료",
    "rejected": "거절됨",
    "expired": "기한 만료",
    "withdrawn": "철회함",
}
_REFERENCE_RE = re.compile(r"^[\w\-./: #()]{1,100}$", re.UNICODE)


class PreservationError(Gps2CctvError):
    """보존 요청 처리 오류."""


class DuplicateRequestError(PreservationError):
    def __init__(self, existing_id: str) -> None:
        super().__init__(
            f"같은 카메라·겹치는 시간 범위의 진행 중인 요청이 이미 있습니다: {existing_id}"
        )
        self.existing_id = existing_id


@dataclass(frozen=True)
class PreservationRequest:
    id: str
    incident_id: str
    candidate_id: str | None
    camera_id: str
    source_id: str
    provider_camera_id: str
    requested_start: datetime
    requested_end: datetime
    estimated_passage_at: datetime
    camera_clock_offset_seconds: float
    uncertainty_seconds: float
    provider_deletion_estimate: datetime | None
    calculation_snapshot: dict[str, object]
    policy_snapshot_id: str
    status: str
    provider_request_id: str | None
    submitted_at: datetime | None
    responded_at: datetime | None
    response_reference: str | None
    created_at: datetime
    updated_at: datetime


def _opt_dt(value: str | None) -> datetime | None:
    return from_db(value) if value else None


def _from_row(row: sqlite3.Row) -> PreservationRequest:
    return PreservationRequest(
        id=row["id"],
        incident_id=row["incident_id"],
        candidate_id=row["candidate_id"],
        camera_id=row["camera_id"],
        source_id=row["source_id"],
        provider_camera_id=row["provider_camera_id"],
        requested_start=from_db(row["requested_start"]),
        requested_end=from_db(row["requested_end"]),
        estimated_passage_at=from_db(row["estimated_passage_at"]),
        camera_clock_offset_seconds=row["camera_clock_offset_seconds"],
        uncertainty_seconds=row["uncertainty_seconds"],
        provider_deletion_estimate=_opt_dt(row["provider_deletion_estimate"]),
        calculation_snapshot=json.loads(row["calculation_snapshot"]),
        policy_snapshot_id=row["policy_snapshot_id"],
        status=row["status"],
        provider_request_id=row["provider_request_id"],
        submitted_at=_opt_dt(row["submitted_at"]),
        responded_at=_opt_dt(row["responded_at"]),
        response_reference=row["response_reference"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
    )


def get_request(conn: sqlite3.Connection, request_id: str) -> PreservationRequest:
    row = conn.execute("SELECT * FROM preservation_request WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"보존 요청을 찾을 수 없습니다: {request_id}")
    return _from_row(row)


def list_requests(conn: sqlite3.Connection, incident_id: str) -> list[PreservationRequest]:
    rows = conn.execute(
        "SELECT * FROM preservation_request WHERE incident_id = ? ORDER BY created_at, id",
        (incident_id,),
    ).fetchall()
    return [_from_row(row) for row in rows]


def prepare_request(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    now: datetime,
    settings: Settings,
    reviewed: bool = False,
) -> PreservationRequest:
    """후보에서 보존 요청 초안을 만든다. 정책 검증을 통과해야 하며 제출은 사용자가 한다."""
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise NotFoundError(f"후보를 찾을 수 없습니다: {candidate_id}")

    def deny(reason_code: str, message: str, exc_type=PreservationError):
        audit.record(
            conn,
            action="preservation.prepare",
            resource_type="camera_candidate",
            resource_id=candidate_id,
            outcome="denied",
            reason_code=reason_code,
            now=now,
            settings=settings,
        )
        if exc_type is PolicyDeniedError:
            raise PolicyDeniedError(message, reason_code)
        raise exc_type(message)

    decision = check_policy(
        conn, candidate.source_id, "preservation_request_prepare", now, settings
    )
    if not decision.allowed:
        deny(
            decision.reason_code, f"보존 요청 준비 거부 — {decision.reason_text}", PolicyDeniedError
        )
    if candidate.needs_manual_review and not reviewed:
        deny(
            "manual_review_required",
            "이 후보는 신뢰도가 낮거나 범위가 넓어 사용자 검토가 필요합니다. "
            "근거를 직접 확인했다면 --reviewed 를 붙여 다시 실행하세요.",
        )
    overlap = conn.execute(
        f"""
        SELECT id FROM preservation_request
        WHERE incident_id = ? AND camera_id = ?
          AND status IN ({",".join("?" * len(ACTIVE_STATUSES))})
          AND requested_start < ? AND requested_end > ?
        LIMIT 1
        """,
        (
            candidate.incident_id,
            candidate.camera_id,
            *sorted(ACTIVE_STATUSES),
            to_db(candidate.request_window_end),
            to_db(candidate.request_window_start),
        ),
    ).fetchone()
    if overlap is not None:
        audit.record(
            conn,
            action="preservation.prepare",
            resource_type="camera_candidate",
            resource_id=candidate_id,
            outcome="denied",
            reason_code="duplicate_request",
            now=now,
            settings=settings,
        )
        raise DuplicateRequestError(overlap["id"])

    snapshot = {
        "calculation_method": candidate.calculation_method,
        "calculation_details": candidate.calculation_details,
        "confidence": candidate.confidence,
        "reasons": list(candidate.reasons),
        "review_window": [to_db(candidate.review_window_start), to_db(candidate.review_window_end)],
        "camera_clock_window": [
            to_db(candidate.camera_clock_window_start),
            to_db(candidate.camera_clock_window_end),
        ],
        "manual_review_acknowledged": bool(reviewed and candidate.needs_manual_review),
    }
    request_id = f"pr_{uuid.uuid4().hex[:12]}"
    stamp = to_db(now)
    with conn:
        conn.execute(
            """
            INSERT INTO preservation_request(id, incident_id, candidate_id, camera_id, source_id,
                provider_camera_id, requested_start, requested_end, estimated_passage_at,
                camera_clock_offset_seconds, uncertainty_seconds, provider_deletion_estimate,
                calculation_snapshot, policy_snapshot_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                request_id,
                candidate.incident_id,
                candidate.id,
                candidate.camera_id,
                candidate.source_id,
                candidate.provider_camera_id,
                to_db(candidate.request_window_start),
                to_db(candidate.request_window_end),
                to_db(candidate.estimated_passage_at),
                candidate.camera_clock_offset_seconds,
                candidate.uncertainty_seconds,
                to_db(candidate.provider_deletion_estimate)
                if candidate.provider_deletion_estimate
                else None,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                decision.snapshot_id,
                stamp,
                stamp,
            ),
        )
    audit.record(
        conn,
        action="preservation.prepare",
        resource_type="preservation_request",
        resource_id=request_id,
        now=now,
        settings=settings,
        detail={"manual_review_acknowledged": bool(reviewed and candidate.needs_manual_review)},
    )
    return get_request(conn, request_id)


def update_status(
    conn: sqlite3.Connection,
    request_id: str,
    new_status: str,
    *,
    now: datetime,
    settings: Settings,
    at: datetime | None = None,
    provider_request_id: str | None = None,
    response_reference: str | None = None,
) -> PreservationRequest:
    """사용자가 공식 절차로 진행한 결과(제출·접수·응답)를 기록한다."""
    request = get_request(conn, request_id)
    if new_status not in STATUS_TEXT:
        raise PreservationError(f"알 수 없는 상태입니다: {new_status}")
    allowed = TRANSITIONS.get(request.status, frozenset())
    if new_status not in allowed:
        raise PreservationError(
            f"'{STATUS_TEXT[request.status]}' 상태에서 '{STATUS_TEXT[new_status]}'(으)로 "
            "바꿀 수 없습니다."
        )
    for label, value in (
        ("제공처 접수번호", provider_request_id),
        ("응답 참조번호", response_reference),
    ):
        if value is not None and not _REFERENCE_RE.match(value):
            raise PreservationError(f"{label} 형식이 올바르지 않습니다(100자 이하 영숫자·기호).")
    event_at = at or now
    if event_at > now:
        raise PreservationError("상태 변경 시각이 현재보다 미래입니다.")
    fields: dict[str, object] = {"status": new_status, "updated_at": to_db(now)}
    if provider_request_id is not None:
        fields["provider_request_id"] = provider_request_id
    if response_reference is not None:
        fields["response_reference"] = response_reference
    if new_status == "submitted":
        fields["submitted_at"] = to_db(event_at)
    elif new_status in {"acknowledged", "fulfilled", "rejected"}:
        fields["responded_at"] = to_db(event_at)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with conn:
        conn.execute(
            f"UPDATE preservation_request SET {assignments} WHERE id = ?",
            (*fields.values(), request_id),
        )
        if new_status == "submitted":
            conn.execute(
                """
                UPDATE incident SET status = 'preservation_requested'
                WHERE id = ? AND status = 'draft'
                """,
                (request.incident_id,),
            )
    audit.record(
        conn,
        action="preservation.update",
        resource_type="preservation_request",
        resource_id=request_id,
        now=now,
        settings=settings,
        reason_code=new_status,
    )
    return get_request(conn, request_id)


def render_draft(conn: sqlite3.Connection, request: PreservationRequest, settings: Settings) -> str:
    """제공처 공식 절차에 옮겨 적을 수 있는 요청 초안(텍스트)."""
    tz = settings.display_timezone
    incident = get_incident(conn, request.incident_id)
    source = get_source(conn, request.source_id)
    try:
        camera = get_camera(conn, request.camera_id)
        camera_name = camera["name"] or "-"
        road = " / ".join(x for x in (camera["road_name"], camera["road_direction"]) if x) or "-"
    except NotFoundError:
        camera_name, road = "-", "-"
    details = request.calculation_snapshot.get("calculation_details", {})
    clock = details.get("clock", {}) if isinstance(details, dict) else {}
    evidence = {
        "manual_observation": "사용자 직접 관측",
        "provider_declared": "제공처 고지",
        "unknown": "정보 없음(보수적 기본값 적용)",
    }.get(clock.get("evidence"), "정보 없음")
    reasons = request.calculation_snapshot.get("reasons", [])
    reason_lines = [f"  - {REASON_TEXT.get(code, code)}" for code in reasons] or ["  - 없음"]
    confidence = CONFIDENCE_TEXT.get(str(request.calculation_snapshot.get("confidence")), "-")

    lines = [
        "[영상정보 보존 요청 초안]",
        "※ 제출 전 내용을 직접 확인하고, 아래 제공처의 공식 절차로 제출하세요.",
        "※ 이 프로그램은 요청을 대신 제출하지 않으며 연락처를 저장하지 않습니다.",
        "",
        f"요청 ID(내부 관리용): {request.id}",
        f"상태: {STATUS_TEXT[request.status]}",
        "",
        "■ 제공처",
        f"  기관·서비스: {source['provider_name']}",
        f"  공식 절차 안내: {source['preservation_procedure']}",
        f"  약관: {source['terms_url']}",
        "",
        "■ 대상 카메라",
        f"  제공처 카메라 ID: {request.provider_camera_id}",
        f"  명칭: {camera_name}",
        f"  도로·방향: {road}",
        "",
        "■ 보존 요청 시간 범위 (CCTV 화면 표시 시각 기준)",
        (
            f"  {fmt_local(request.requested_start, tz)} ~ "
            f"{fmt_local(request.requested_end, tz, with_date=False)}"
        ),
        f"  표준시 기준 예상 통과 시각: {fmt_local(request.estimated_passage_at, tz)}",
        (
            f"  CCTV 시각 오차 추정: {request.camera_clock_offset_seconds:+.1f}초 "
            f"± {request.uncertainty_seconds:.1f}초 (근거: {evidence})"
        ),
        "  ※ 시각 오차 추정이 틀린 경우까지 포함하도록 표준시 기준 범위와 CCTV 시계 기준 범위를",
        "    모두 덮는 범위로 산정했습니다.",
        f"  시간 범위 신뢰도: {confidence}",
        "  참고 사항:",
        *reason_lines,
    ]
    if request.provider_deletion_estimate:
        lines.append(
            "  제공처 보관 기간 기준 예상 삭제 시점: "
            f"{fmt_local(request.provider_deletion_estimate, tz)} (추정 — 제공처에 확인 필요)"
        )
    lines += [
        "",
        "■ 사건 개요",
        (
            f"  사고 시점: {fmt_local(incident.occurred_at, tz)} "
            f"(±{incident.occurred_at_uncertainty_seconds:.0f}초)"
        ),
        f"  사유: {incident.reason}",
        "",
        "■ 요청인 정보",
        "  성명·연락처: (직접 기재)",
        "",
        "■ 고지",
        f"  {DISCLAIMER}",
    ]
    return "\n".join(lines) + "\n"
