"""사건 보고서(텍스트·JSON).

보고서에는 사용자 경로 좌표를 넣지 않는다. 카메라 정보, 시간 범위, 계산 근거,
보존 요청 상태만 보여 준다. 모든 보고서 맨 앞에 오해 방지 고지를 붙인다.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import DISCLAIMER, __version__, audit
from .candidates import (
    CONFIDENCE_TEXT,
    REASON_TEXT,
    Candidate,
    deletion_status,
    load_candidates,
)
from .config import Settings
from .incidents import Incident, get_incident
from .policy import REASON_TEXT as POLICY_REASON_TEXT
from .preservation import STATUS_TEXT, list_requests
from .timeutil import fmt_local, from_db, to_db

INCIDENT_STATUS_TEXT = {
    "draft": "초안",
    "preservation_requested": "보존 요청 진행 중",
    "reviewing": "검토 중",
    "closed": "종료",
}
EVIDENCE_TEXT = {
    "manual_observation": "사용자 직접 관측",
    "provider_declared": "제공처 고지",
    "unknown": "정보 없음",
}
DELETION_TEXT = {
    "unknown": "제공처 보관 기간 정보 없음 — 제공처에 확인 필요",
    "ok": "여유 있음",
    "imminent": "임박 — 서둘러 보존 요청 필요",
    "passed": "보관 기간이 지났을 가능성 — 제공처에 즉시 확인 필요",
}


def _camera_meta(conn: sqlite3.Connection, camera_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT name, road_name, road_direction, status FROM camera WHERE id = ?", (camera_id,)
    ).fetchone()
    if row is None:
        return {"name": None, "road_name": None, "road_direction": None, "status": "unknown"}
    return dict(row)


def _source_name(conn: sqlite3.Connection, source_id: str) -> str:
    row = conn.execute(
        "SELECT provider_name FROM cctv_source WHERE id = ?", (source_id,)
    ).fetchone()
    return row["provider_name"] if row else source_id


def _route_count(conn: sqlite3.Connection, incident_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM route_point WHERE incident_id = ?", (incident_id,)
    ).fetchone()[0]


def _candidate_dict(
    conn: sqlite3.Connection, c: Candidate, now: datetime, settings: Settings
) -> dict[str, Any]:
    meta = _camera_meta(conn, c.camera_id)
    return {
        "candidate_id": c.id,
        "camera_id": c.camera_id,
        "source_id": c.source_id,
        "provider_name": _source_name(conn, c.source_id),
        "provider_camera_id": c.provider_camera_id,
        "camera_name": meta["name"],
        "road_name": meta["road_name"],
        "road_direction": meta["road_direction"],
        "camera_status": meta["status"],
        "pass_index": c.pass_index,
        "estimated_passage_at": to_db(c.estimated_passage_at),
        "closest_distance_m": (
            round(c.closest_distance_m, 1) if c.closest_distance_m is not None else None
        ),
        "review_window": {
            "time_basis": "standard_time",
            "start": to_db(c.review_window_start),
            "end": to_db(c.review_window_end),
        },
        "camera_clock": {
            "offset_seconds": round(c.camera_clock_offset_seconds, 3),
            "uncertainty_seconds": round(c.uncertainty_seconds, 3),
            "window_start": to_db(c.camera_clock_window_start),
            "window_end": to_db(c.camera_clock_window_end),
        },
        "request_window": {
            "time_basis": "cctv_display_time",
            "start": to_db(c.request_window_start),
            "end": to_db(c.request_window_end),
        },
        "time_window_confidence": c.confidence,
        "needs_manual_review": c.needs_manual_review,
        "reasons": list(c.reasons),
        "provider_deletion_estimate": (
            to_db(c.provider_deletion_estimate) if c.provider_deletion_estimate else None
        ),
        "provider_deletion_status": deletion_status(c.provider_deletion_estimate, now, settings),
        "calculation_method": c.calculation_method,
        "calculation_details": c.calculation_details,
        "policy_snapshot_id": c.policy_snapshot_id,
    }


def build_report(
    conn: sqlite3.Connection,
    incident_id: str,
    now: datetime,
    settings: Settings,
    *,
    excluded_sources: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    incident = get_incident(conn, incident_id)
    candidates = load_candidates(conn, incident_id)
    requests = list_requests(conn, incident_id)
    report: dict[str, Any] = {
        "disclaimer": DISCLAIMER,
        "generated_at": to_db(now),
        "generator": f"gps2cctv {__version__}",
        "incident": {
            "id": incident.id,
            "status": incident.status,
            "occurred_at": to_db(incident.occurred_at),
            "occurred_at_uncertainty_seconds": incident.occurred_at_uncertainty_seconds,
            "window_before_seconds": incident.window_before_seconds,
            "window_after_seconds": incident.window_after_seconds,
            "started_at": to_db(incident.started_at),
            "ended_at": to_db(incident.ended_at),
            "has_location": incident.has_location,
            "location_precision_decimals": incident.precision_decimals,
            "route_points": _route_count(conn, incident_id),
            "reason": incident.reason,
            "retention_until": to_db(incident.retention_until),
        },
        "candidates": [_candidate_dict(conn, c, now, settings) for c in candidates],
        "preservation_requests": [
            {
                "request_id": r.id,
                "candidate_id": r.candidate_id,
                "camera_id": r.camera_id,
                "provider_camera_id": r.provider_camera_id,
                "status": r.status,
                "requested_window": {
                    "time_basis": "cctv_display_time",
                    "start": to_db(r.requested_start),
                    "end": to_db(r.requested_end),
                },
                "provider_request_id": r.provider_request_id,
                "submitted_at": to_db(r.submitted_at) if r.submitted_at else None,
                "responded_at": to_db(r.responded_at) if r.responded_at else None,
                "response_reference": r.response_reference,
                "provider_deletion_estimate": (
                    to_db(r.provider_deletion_estimate) if r.provider_deletion_estimate else None
                ),
            }
            for r in requests
        ],
    }
    if excluded_sources is not None:
        report["excluded_sources"] = [
            {"source_id": sid, "reason_code": code} for sid, code in excluded_sources
        ]
    return report


def _incident_lines(incident: Incident, route_points: int, settings: Settings) -> list[str]:
    tz = settings.display_timezone
    location = (
        f"입력됨(소수점 {incident.precision_decimals}자리 정밀도, ±{incident.location_uncertainty_m:.0f}m)"
        if incident.has_location
        else "없음"
    )
    return [
        f"사건 {incident.id}  상태: {INCIDENT_STATUS_TEXT.get(incident.status, incident.status)}",
        (
            f"  사고 시점: {fmt_local(incident.occurred_at, tz)} "
            f"(±{incident.occurred_at_uncertainty_seconds:.0f}초)"
        ),
        (
            f"  확인 범위: {fmt_local(incident.started_at, tz)} ~ "
            f"{fmt_local(incident.ended_at, tz, with_date=False)} "
            f"(사고 전 {incident.window_before_seconds / 60:.0f}분, "
            f"후 {incident.window_after_seconds / 60:.0f}분)"
        ),
        f"  경로 지점: {route_points}개 / 사고 위치: {location}",
        f"  자료 보존 종료: {fmt_local(incident.retention_until, tz)}",
    ]


def _candidate_lines(item: dict[str, Any], number: int, settings: Settings) -> list[str]:
    tz = settings.display_timezone

    def t(value: str, with_date: bool = False, rounding: str = "nearest") -> str:
        return fmt_local(from_db(value), tz, with_date=with_date, rounding=rounding)

    name = item["camera_name"] or "-"
    road = " / ".join(x for x in (item["road_name"], item["road_direction"]) if x)
    confidence = CONFIDENCE_TEXT[item["time_window_confidence"]]
    review = "  ※ 사용자 검토 필요" if item["needs_manual_review"] else ""
    clock = item["calculation_details"].get("clock", {})
    evidence = EVIDENCE_TEXT.get(clock.get("evidence"), "정보 없음")
    distance = (
        f"{item['closest_distance_m']:.0f}m"
        if item["closest_distance_m"] is not None
        else "추정 불가(GPS 공백)"
    )
    lines = [
        (
            f"{number}. [{item['candidate_id']}] {name} — {item['provider_name']} "
            f"(카메라 ID {item['provider_camera_id']})"
        ),
        f"   {road}" if road else None,
        f"   시간 범위 신뢰도: {confidence}{review}",
        f"   예상 통과(표준시): {t(item['estimated_passage_at'])}   최근접 거리: {distance}",
        (
            f"   확인 범위(표준시): {t(item['review_window']['start'], rounding='floor')} ~ "
            f"{t(item['review_window']['end'], rounding='ceil')}"
        ),
        (
            f"   CCTV 시각 오차: {item['camera_clock']['offset_seconds']:+.1f}초 "
            f"± {item['camera_clock']['uncertainty_seconds']:.1f}초 (근거: {evidence})"
        ),
        (
            f"   확인 범위(CCTV 시계): {t(item['camera_clock']['window_start'], rounding='floor')} ~ "
            f"{t(item['camera_clock']['window_end'], rounding='ceil')}"
        ),
        (
            f"   ▶ 보존 요청 권장 범위(CCTV 표시 시각): {t(item['request_window']['start'], True)} ~ "
            f"{t(item['request_window']['end'])}"
        ),
    ]
    if item["provider_deletion_estimate"]:
        lines.append(
            f"   제공처 보관 기간 기준 예상 삭제: {t(item['provider_deletion_estimate'], True)} "
            f"({DELETION_TEXT[item['provider_deletion_status']]})"
        )
    else:
        lines.append(f"   제공처 보관 기간: {DELETION_TEXT['unknown']}")
    for code in item["reasons"]:
        lines.append(f"   - {REASON_TEXT.get(code, code)}")
    return [line for line in lines if line is not None]


def render_text(report: dict[str, Any], conn: sqlite3.Connection, settings: Settings) -> str:
    tz = settings.display_timezone
    incident = get_incident(conn, report["incident"]["id"])
    lines = [f"[안내] {report['disclaimer']}", ""]
    lines += _incident_lines(incident, report["incident"]["route_points"], settings)
    lines.append("")
    excluded = report.get("excluded_sources") or []
    candidates = report["candidates"]
    lines.append(f"후보 카메라 {len(candidates)}건")
    for item in excluded:
        reason = POLICY_REASON_TEXT.get(item["reason_code"], item["reason_code"])
        lines.append(f"  (제외된 소스: {item['source_id']} — {reason})")
    urgent = [c for c in candidates if c["provider_deletion_status"] in ("imminent", "passed")]
    if urgent:
        lines.append(
            f"  ⚠ 예상 삭제 시점이 임박했거나 지난 후보 {len(urgent)}건 — 우선 확인하세요."
        )
    for number, item in enumerate(candidates, start=1):
        lines.append("")
        lines += _candidate_lines(item, number, settings)
    requests = report["preservation_requests"]
    if requests:
        lines += ["", f"보존 요청 {len(requests)}건"]
        for r in requests:
            ref = f", 접수번호 {r['provider_request_id']}" if r["provider_request_id"] else ""
            lines.append(
                f"  [{r['request_id']}] 카메라 {r['provider_camera_id']} "
                f"{fmt_local(from_db(r['requested_window']['start']), tz)} ~ "
                f"{fmt_local(from_db(r['requested_window']['end']), tz, with_date=False)} "
                f"— {STATUS_TEXT[r['status']]}{ref}"
            )
    return "\n".join(lines) + "\n"


def record_view(
    conn: sqlite3.Connection, incident_id: str, now: datetime, settings: Settings, *, export: bool
) -> None:
    audit.record(
        conn,
        action="incident.export" if export else "incident.report_view",
        resource_type="incident",
        resource_id=incident_id,
        now=now,
        settings=settings,
    )
