"""후보 카메라와 확인·보존 요청 시간 범위 계산.

계산 방법(route_interpolation_v1):

1. 사건 확인 범위 안의 GPS 경로를 시간순 선분으로 나눈다.
2. 카메라마다 "유효 반경"을 정한다.
   유효 반경 = 촬영 반경 + GPS 정확도 + 좌표 반올림 오차 + 카메라 위치 오차
3. 일반 선분(간격 ≤ max_gap_seconds): 선분이 유효 반경 원과 만나면, 그 선분의 양 끝 GPS 기록
   시각을 확인 범위로 잡는다(두 기록 사이 실제 경로는 직선이 아닐 수 있으므로 보수적으로).
4. GPS 공백 선분(간격 > max_gap_seconds): 직선 보간 대신 "도달 가능 범위(시공간 프리즘)"로
   카메라 반경에 들어갈 수 있었던 가장 이른·늦은 시각을 계산한다. 신뢰도는 낮음으로 표시한다.
5. 겹치는 범위를 합치고, GPS 시각 오차 여유를 앞뒤로 더해 "표준시 기준 확인 범위"를 만든다.
6. CCTV 시각 오차(offset ± 불확실성)를 적용해 "CCTV 시계 기준 범위"를 만든다.
7. 두 범위를 모두 포함하도록 합치고 바깥쪽으로 반올림해 "보존 요청 권장 범위"를 만든다.
   (시각 오차 추정이 틀렸더라도 CCTV 시계가 정확한 경우까지 포함하기 위함)

이 계산은 "어느 시간대 자료를 보존 요청하거나 직접 확인할지"를 정할 뿐이며,
영상 속 사람·차량이 사용자인지 판단하지 않는다.
"""

from __future__ import annotations

import itertools
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import audit
from .config import Settings
from .geo import (
    closest_parameter,
    degree_margins,
    rounding_error_m,
    segment_circle_interval,
    to_local_xy,
)
from .incidents import IncidentError, get_incident, load_route
from .policy import check_policy
from .timeutil import ceil_to, floor_to, from_db, seconds_between, shift, to_db

METHOD_ROUTE = "route_interpolation_v1"
METHOD_LOCATION_ONLY = "location_only_v1"

REASON_TEXT: dict[str, str] = {
    "clock_unknown": "CCTV 시각 정보 없음 — 보수적인 기본 오차 적용",
    "clock_uncertainty_high": "CCTV 시각 오차의 불확실성이 큼",
    "clock_model_stale": "CCTV 시각 오차 근거가 오래됨",
    "gps_gap": "GPS 기록 공백 구간 — 도달 가능 범위로 추정",
    "gps_accuracy_unknown": "GPS 정확도 정보 없음 — 기본값 적용",
    "route_starts_in_range": "경로 기록 시작 시점에 이미 촬영 반경 안",
    "route_ends_in_range": "경로 기록 종료 시점에도 촬영 반경 안",
    "location_only": "경로 없이 사고 위치·시각만으로 추정",
    "camera_status_inactive": "카탈로그상 비활성 카메라",
    "camera_status_unknown": "카탈로그상 상태 미상 카메라",
    "camera_missing_from_catalog": "최근 카탈로그에서 사라진 카메라",
    "window_too_wide": "요청 범위가 너무 넓음 — 사용자 검토 또는 제공처 문의 필요",
}
LOW_CONFIDENCE_REASONS = frozenset(
    {
        "clock_unknown",
        "gps_gap",
        "location_only",
        "camera_status_inactive",
        "camera_missing_from_catalog",
        "window_too_wide",
    }
)
MEDIUM_CONFIDENCE_REASONS = frozenset(
    {
        "clock_uncertainty_high",
        "clock_model_stale",
        "gps_accuracy_unknown",
        "route_starts_in_range",
        "route_ends_in_range",
        "camera_status_unknown",
    }
)
CONFIDENCE_TEXT = {"high": "높음", "medium": "보통", "low": "낮음"}


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackPoint:
    t: datetime
    lat: float
    lon: float
    accuracy_m: float | None = None
    speed_mps: float | None = None


@dataclass(frozen=True)
class CameraInfo:
    id: str
    source_id: str
    provider_camera_id: str
    latitude: float
    longitude: float
    location_uncertainty_m: float = 10.0
    coverage_radius_m: float | None = None
    status: str = "active"
    name: str | None = None
    road_name: str | None = None
    road_direction: str | None = None


@dataclass
class _Hit:
    window_start: datetime
    window_end: datetime
    entry_at: datetime
    exit_at: datetime
    closest_at: datetime | None
    closest_distance_m: float | None
    effective_radius_m: float
    accuracy_m: float
    segment_seconds: float
    flags: set[str] = field(default_factory=set)
    gap_speed_mps: float | None = None

    def absorb(self, other: _Hit) -> None:
        self.window_start = min(self.window_start, other.window_start)
        self.window_end = max(self.window_end, other.window_end)
        self.entry_at = min(self.entry_at, other.entry_at)
        self.exit_at = max(self.exit_at, other.exit_at)
        if other.closest_distance_m is not None and (
            self.closest_distance_m is None or other.closest_distance_m < self.closest_distance_m
        ):
            self.closest_distance_m = other.closest_distance_m
            self.closest_at = other.closest_at
        self.effective_radius_m = max(self.effective_radius_m, other.effective_radius_m)
        self.accuracy_m = max(self.accuracy_m, other.accuracy_m)
        self.segment_seconds = max(self.segment_seconds, other.segment_seconds)
        self.flags |= other.flags
        if other.gap_speed_mps is not None:
            self.gap_speed_mps = max(self.gap_speed_mps or 0.0, other.gap_speed_mps)


@dataclass(frozen=True)
class Passage:
    """카메라 유효 반경을 지나간 한 번의 통과(표준시 기준)."""

    estimated_passage_at: datetime
    closest_distance_m: float | None
    entry_at: datetime
    exit_at: datetime
    window_start: datetime
    window_end: datetime
    flags: frozenset[str]
    details: dict[str, object]


def _accuracy(point: TrackPoint, settings: Settings) -> tuple[float, bool]:
    if point.accuracy_m is None:
        return settings.default_gps_accuracy_m, True
    return point.accuracy_m, False


def _segment_speeds(track: list[TrackPoint], xy: list[tuple[float, float]]) -> list[float | None]:
    speeds: list[float | None] = []
    for i in range(len(track) - 1):
        dt = seconds_between(track[i].t, track[i + 1].t)
        if dt <= 0:
            speeds.append(None)
            continue
        dist = math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])
        speeds.append(dist / dt)
    return speeds


def gap_speed_limit(
    track: list[TrackPoint],
    index: int,
    chord_m: float,
    gap_seconds: float,
    segment_speeds: list[float | None],
    settings: Settings,
) -> float:
    """GPS 공백 구간에서 가정하는 최대 이동 속도(m/s).

    공백 앞뒤의 관측 속도·직선 환산 속도 중 큰 값에 여유 계수를 곱하고, 설정된 하한·상한으로
    제한한다. 단, 두 지점 사이를 실제로 이동한 것이므로 직선 환산 속도보다 작을 수는 없다.
    """
    implied = chord_m / gap_seconds
    observed = [implied]
    for point in (track[index], track[index + 1]):
        if point.speed_mps is not None:
            observed.append(point.speed_mps)
    for j in (index - 1, index + 1):
        if 0 <= j < len(segment_speeds):
            dt = seconds_between(track[j].t, track[j + 1].t)
            speed = segment_speeds[j]
            if speed is not None and dt <= settings.max_gap_seconds:
                observed.append(speed)
    speed = max(observed) * settings.gap_speed_factor
    speed = min(max(speed, settings.gap_min_speed_mps), settings.gap_max_speed_mps)
    return max(speed, implied)


def compute_passages(
    track: list[TrackPoint],
    camera: CameraInfo,
    settings: Settings,
    precision_decimals: int | None,
) -> list[Passage]:
    """카메라 유효 반경을 지나간 통과 구간을 계산한다(표준시 기준)."""
    if not track:
        return []
    coverage = camera.coverage_radius_m or settings.default_coverage_radius_m
    rounding = (
        rounding_error_m(camera.latitude, precision_decimals)
        if precision_decimals is not None
        else 0.0
    )
    base_radius = coverage + rounding + camera.location_uncertainty_m
    xy = [to_local_xy(p.lat, p.lon, camera.latitude, camera.longitude) for p in track]
    hits: list[_Hit] = []

    if len(track) == 1:
        point = track[0]
        acc, unknown = _accuracy(point, settings)
        radius = base_radius + acc
        distance = math.hypot(*xy[0])
        if distance <= radius:
            flags = {"route_starts_in_range", "route_ends_in_range"}
            if unknown:
                flags.add("gps_accuracy_unknown")
            hits.append(
                _Hit(point.t, point.t, point.t, point.t, point.t, distance, radius, acc, 0.0, flags)
            )
    else:
        speeds = _segment_speeds(track, xy)
        last = len(track) - 2
        for i in range(len(track) - 1):
            a, b = track[i], track[i + 1]
            dt = seconds_between(a.t, b.t)
            if dt <= 0:
                continue
            acc_a, unknown_a = _accuracy(a, settings)
            acc_b, unknown_b = _accuracy(b, settings)
            acc = max(acc_a, acc_b)
            radius = base_radius + acc
            (ax, ay), (bx, by) = xy[i], xy[i + 1]
            flags: set[str] = set()
            if unknown_a or unknown_b:
                flags.add("gps_accuracy_unknown")

            if dt > settings.max_gap_seconds:
                chord = math.hypot(bx - ax, by - ay)
                speed = gap_speed_limit(track, i, chord, dt, speeds, settings)
                earliest = max(0.0, math.hypot(ax, ay) - radius) / speed
                latest = dt - max(0.0, math.hypot(bx, by) - radius) / speed
                if earliest > latest:
                    continue
                flags.add("gps_gap")
                start, end = shift(a.t, earliest), shift(a.t, latest)
                hits.append(_Hit(start, end, start, end, None, None, radius, acc, dt, flags, speed))
                continue

            interval = segment_circle_interval(ax, ay, bx, by, radius)
            if interval is None:
                continue
            s0, s1 = interval
            sc = closest_parameter(ax, ay, bx, by)
            if i == 0 and s0 <= 0.0:
                flags.add("route_starts_in_range")
            if i == last and s1 >= 1.0:
                flags.add("route_ends_in_range")
            hits.append(
                _Hit(
                    window_start=a.t,
                    window_end=b.t,
                    entry_at=shift(a.t, s0 * dt),
                    exit_at=shift(a.t, s1 * dt),
                    closest_at=shift(a.t, sc * dt),
                    closest_distance_m=math.hypot(ax + sc * (bx - ax), ay + sc * (by - ay)),
                    effective_radius_m=radius,
                    accuracy_m=acc,
                    segment_seconds=dt,
                    flags=flags,
                )
            )

    hits.sort(key=lambda h: h.window_start)
    merged: list[_Hit] = []
    for hit in hits:
        if merged and hit.window_start <= merged[-1].window_end:
            merged[-1].absorb(hit)
        else:
            merged.append(hit)

    margin = settings.gps_time_margin_seconds
    passages = []
    for hit in merged:
        if hit.closest_at is not None:
            estimated = hit.closest_at
        else:
            estimated = shift(
                hit.window_start, seconds_between(hit.window_start, hit.window_end) / 2
            )
        details: dict[str, object] = {
            "coverage_radius_m": round(coverage, 1),
            "gps_accuracy_m_max": round(hit.accuracy_m, 1),
            "rounding_error_m": round(rounding, 1),
            "camera_location_uncertainty_m": round(camera.location_uncertainty_m, 1),
            "effective_radius_m": round(hit.effective_radius_m, 1),
            "entry_at": to_db(hit.entry_at),
            "exit_at": to_db(hit.exit_at),
            "max_segment_seconds": round(hit.segment_seconds, 1),
            "gps_time_margin_seconds": margin,
        }
        if hit.gap_speed_mps is not None:
            details["gap_speed_limit_mps"] = round(hit.gap_speed_mps, 1)
        passages.append(
            Passage(
                estimated_passage_at=estimated,
                closest_distance_m=hit.closest_distance_m,
                entry_at=hit.entry_at,
                exit_at=hit.exit_at,
                window_start=shift(hit.window_start, -margin),
                window_end=shift(hit.window_end, margin),
                flags=frozenset(hit.flags),
                details=details,
            )
        )
    return passages


@dataclass(frozen=True)
class ClockModel:
    id: str
    reference_time: datetime
    offset_seconds: float
    uncertainty_seconds: float
    drift_seconds_per_day: float
    evidence: str
    note: str | None = None


@dataclass(frozen=True)
class ClockEstimate:
    """특정 시각의 CCTV 시계 오차 추정. offset = CCTV 표시 시각 - 표준 시각."""

    offset_seconds: float
    uncertainty_seconds: float
    evidence: str
    model_id: str | None
    reference_time: datetime | None
    base_uncertainty_seconds: float
    drift_seconds_per_day: float
    age_days: float | None
    reasons: frozenset[str]

    def as_details(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "model_id": self.model_id,
            "reference_time": to_db(self.reference_time) if self.reference_time else None,
            "offset_seconds": round(self.offset_seconds, 3),
            "base_uncertainty_seconds": round(self.base_uncertainty_seconds, 3),
            "drift_seconds_per_day": self.drift_seconds_per_day,
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
            "uncertainty_seconds": round(self.uncertainty_seconds, 3),
        }


def resolve_clock(models: list[ClockModel], at: datetime, settings: Settings) -> ClockEstimate:
    """``at`` 에 가장 가까운 시각 오차 근거를 고르고, 시간이 지난 만큼 불확실성을 늘린다."""
    if not models:
        unc = settings.unknown_clock_uncertainty_seconds
        return ClockEstimate(
            0.0, unc, "unknown", None, None, unc, 0.0, None, frozenset({"clock_unknown"})
        )
    best = min(models, key=lambda m: (abs(seconds_between(m.reference_time, at)), m.evidence))
    age_days = abs(seconds_between(best.reference_time, at)) / 86400.0
    uncertainty = best.uncertainty_seconds + best.drift_seconds_per_day * age_days
    reasons = set()
    if uncertainty > settings.high_confidence_max_clock_uncertainty_seconds:
        reasons.add("clock_uncertainty_high")
    if age_days > settings.stale_clock_model_days:
        reasons.add("clock_model_stale")
    return ClockEstimate(
        offset_seconds=best.offset_seconds,
        uncertainty_seconds=uncertainty,
        evidence=best.evidence,
        model_id=best.id,
        reference_time=best.reference_time,
        base_uncertainty_seconds=best.uncertainty_seconds,
        drift_seconds_per_day=best.drift_seconds_per_day,
        age_days=age_days,
        reasons=frozenset(reasons),
    )


@dataclass(frozen=True)
class Windows:
    review_start: datetime
    review_end: datetime
    camera_clock_start: datetime
    camera_clock_end: datetime
    request_start: datetime
    request_end: datetime

    @property
    def request_width_seconds(self) -> float:
        return seconds_between(self.request_start, self.request_end)


def build_windows(
    review_start: datetime,
    review_end: datetime,
    offset_seconds: float,
    uncertainty_seconds: float,
    rounding_seconds: int,
) -> Windows:
    """표준시 기준 범위에 CCTV 시각 오차를 적용하고, 둘을 모두 포함하는 요청 범위를 만든다."""
    cam_start = shift(review_start, offset_seconds - uncertainty_seconds)
    cam_end = shift(review_end, offset_seconds + uncertainty_seconds)
    return Windows(
        review_start=review_start,
        review_end=review_end,
        camera_clock_start=cam_start,
        camera_clock_end=cam_end,
        request_start=floor_to(min(review_start, cam_start), rounding_seconds),
        request_end=ceil_to(max(review_end, cam_end), rounding_seconds),
    )


def assess(
    flags: frozenset[str] | set[str],
    clock: ClockEstimate,
    windows: Windows,
    camera_status: str,
    settings: Settings,
) -> tuple[str, bool, list[str]]:
    """시간 범위 신뢰도(식별 신뢰도가 아님)와 사용자 검토 필요 여부."""
    reasons = set(flags) | set(clock.reasons)
    if camera_status == "inactive":
        reasons.add("camera_status_inactive")
    elif camera_status == "unknown":
        reasons.add("camera_status_unknown")
    elif camera_status == "missing_from_catalog":
        reasons.add("camera_missing_from_catalog")
    if windows.request_width_seconds > settings.max_review_window_seconds:
        reasons.add("window_too_wide")
    if reasons & LOW_CONFIDENCE_REASONS:
        confidence = "low"
    elif reasons & MEDIUM_CONFIDENCE_REASONS:
        confidence = "medium"
    else:
        confidence = "high"
    needs_review = confidence == "low" or "window_too_wide" in reasons
    return confidence, needs_review, sorted(reasons)


def deletion_status(estimate: datetime | None, now: datetime, settings: Settings) -> str:
    """제공처 보관 기간 기준 예상 삭제 시점 대비 현재 상태."""
    if estimate is None:
        return "unknown"
    if estimate <= now:
        return "passed"
    if estimate - now <= timedelta(days=settings.deletion_warning_days):
        return "imminent"
    return "ok"


# ---------------------------------------------------------------------------
# 저장소 연동
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    id: str
    incident_id: str
    camera_id: str
    source_id: str
    provider_camera_id: str
    pass_index: int
    estimated_passage_at: datetime
    closest_distance_m: float | None
    review_window_start: datetime
    review_window_end: datetime
    camera_clock_offset_seconds: float
    uncertainty_seconds: float
    camera_clock_window_start: datetime
    camera_clock_window_end: datetime
    request_window_start: datetime
    request_window_end: datetime
    calculation_method: str
    calculation_details: dict[str, object]
    confidence: str
    needs_manual_review: bool
    reasons: tuple[str, ...]
    provider_deletion_estimate: datetime | None
    policy_snapshot_id: str
    created_at: datetime


def _candidate_from_row(row: sqlite3.Row) -> Candidate:
    deletion = row["provider_deletion_estimate"]
    return Candidate(
        id=row["id"],
        incident_id=row["incident_id"],
        camera_id=row["camera_id"],
        source_id=row["source_id"],
        provider_camera_id=row["provider_camera_id"],
        pass_index=row["pass_index"],
        estimated_passage_at=from_db(row["estimated_passage_at"]),
        closest_distance_m=row["closest_distance_m"],
        review_window_start=from_db(row["review_window_start"]),
        review_window_end=from_db(row["review_window_end"]),
        camera_clock_offset_seconds=row["camera_clock_offset_seconds"],
        uncertainty_seconds=row["uncertainty_seconds"],
        camera_clock_window_start=from_db(row["camera_clock_window_start"]),
        camera_clock_window_end=from_db(row["camera_clock_window_end"]),
        request_window_start=from_db(row["request_window_start"]),
        request_window_end=from_db(row["request_window_end"]),
        calculation_method=row["calculation_method"],
        calculation_details=json.loads(row["calculation_details"]),
        confidence=row["confidence"],
        needs_manual_review=bool(row["needs_manual_review"]),
        reasons=tuple(json.loads(row["reasons"])),
        provider_deletion_estimate=from_db(deletion) if deletion else None,
        policy_snapshot_id=row["policy_snapshot_id"],
        created_at=from_db(row["created_at"]),
    )


def load_candidates(conn: sqlite3.Connection, incident_id: str) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT * FROM camera_candidate WHERE incident_id = ?
        ORDER BY estimated_passage_at, camera_id, pass_index
        """,
        (incident_id,),
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> Candidate | None:
    row = conn.execute("SELECT * FROM camera_candidate WHERE id = ?", (candidate_id,)).fetchone()
    return _candidate_from_row(row) if row else None


def load_clock_models(conn: sqlite3.Connection, camera_id: str) -> list[ClockModel]:
    rows = conn.execute(
        "SELECT * FROM camera_time_model WHERE camera_id = ?", (camera_id,)
    ).fetchall()
    return [
        ClockModel(
            id=r["id"],
            reference_time=from_db(r["reference_time"]),
            offset_seconds=r["estimated_offset_seconds"],
            uncertainty_seconds=r["uncertainty_seconds"],
            drift_seconds_per_day=r["drift_seconds_per_day"],
            evidence=r["evidence"],
            note=r["evidence_note"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class CandidateRun:
    incident_id: str
    mode: str
    route_points: int
    cameras_considered: int
    candidates: list[Candidate]
    excluded_sources: list[tuple[str, str]]


def _search_margin_m(
    track: list[TrackPoint], conn: sqlite3.Connection, settings: Settings
) -> float:
    row = conn.execute(
        "SELECT MAX(coverage_radius_m), MAX(location_uncertainty_m) FROM camera"
    ).fetchone()
    max_coverage = max(row[0] or 0.0, settings.default_coverage_radius_m)
    max_location = row[1] or 0.0
    max_accuracy = max(
        (p.accuracy_m if p.accuracy_m is not None else settings.default_gps_accuracy_m)
        for p in track
    )
    max_gap_reach = 0.0
    for a, b in itertools.pairwise(track):
        dt = seconds_between(a.t, b.t)
        if dt > settings.max_gap_seconds:
            max_gap_reach = max(max_gap_reach, settings.gap_max_speed_mps * dt)
    return max_coverage + max_location + max_accuracy + max_gap_reach + 50.0


def find_candidates(
    conn: sqlite3.Connection, incident_id: str, now: datetime, settings: Settings
) -> CandidateRun:
    """사건 경로 주변 공식 카메라와 시간 범위를 계산해 저장한다(이전 계산 결과는 교체)."""
    incident = get_incident(conn, incident_id)
    route = load_route(conn, incident_id)
    precision = incident.precision_decimals
    if route:
        mode = METHOD_ROUTE
        track = [
            TrackPoint(p.recorded_at, p.latitude, p.longitude, p.accuracy_m, p.speed_mps)
            for p in route
        ]
    elif incident.has_location:
        mode = METHOD_LOCATION_ONLY
        track = [
            TrackPoint(
                incident.occurred_at,
                incident.latitude,
                incident.longitude,
                incident.location_uncertainty_m,
            )
        ]
    else:
        raise IncidentError(
            "경로나 사고 위치가 없어 후보를 계산할 수 없습니다. "
            "경로를 입력(incident import-route)하거나 사고 위치를 지정해 사건을 만드세요."
        )

    margin_m = _search_margin_m(track, conn, settings)
    lat_values = [p.lat for p in track]
    lon_values = [p.lon for p in track]
    dlat, dlon = degree_margins(max(abs(v) for v in lat_values), margin_m)
    rows = conn.execute(
        """
        SELECT * FROM camera
        WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
        ORDER BY id
        """,
        (
            min(lat_values) - dlat,
            max(lat_values) + dlat,
            min(lon_values) - dlon,
            max(lon_values) + dlon,
        ),
    ).fetchall()

    decisions = {}
    for source_id in sorted({row["source_id"] for row in rows}):
        decisions[source_id] = check_policy(conn, source_id, "catalog_lookup", now, settings)
    excluded = [(sid, d.reason_code) for sid, d in decisions.items() if not d.allowed]
    retention_days = {
        r["id"]: r["provider_retention_days"]
        for r in conn.execute("SELECT id, provider_retention_days FROM cctv_source")
    }

    records: list[tuple] = []
    considered = 0
    for row in rows:
        decision = decisions[row["source_id"]]
        if not decision.allowed:
            continue
        considered += 1
        camera = CameraInfo(
            id=row["id"],
            source_id=row["source_id"],
            provider_camera_id=row["provider_camera_id"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            location_uncertainty_m=row["location_uncertainty_m"],
            coverage_radius_m=row["coverage_radius_m"],
            status=row["status"],
        )
        passages = compute_passages(track, camera, settings, precision)
        if not passages:
            continue
        models = load_clock_models(conn, camera.id)
        for index, passage in enumerate(passages):
            flags = set(passage.flags)
            review_start, review_end = passage.window_start, passage.window_end
            if mode == METHOD_LOCATION_ONLY:
                flags -= {"route_starts_in_range", "route_ends_in_range"}
                flags.add("location_only")
                pad = (
                    incident.occurred_at_uncertainty_seconds
                    + settings.location_only_padding_seconds
                )
                review_start = shift(incident.occurred_at, -pad)
                review_end = shift(incident.occurred_at, pad)
            clock = resolve_clock(models, passage.estimated_passage_at, settings)
            windows = build_windows(
                review_start,
                review_end,
                clock.offset_seconds,
                clock.uncertainty_seconds,
                settings.request_rounding_seconds,
            )
            confidence, needs_review, reasons = assess(
                frozenset(flags), clock, windows, camera.status, settings
            )
            days = retention_days.get(camera.source_id)
            deletion = shift(windows.request_start, days * 86400) if days else None
            details = dict(passage.details)
            details["clock"] = clock.as_details()
            details["request_rounding_seconds"] = settings.request_rounding_seconds
            details["provider_retention_days"] = days
            records.append(
                (
                    f"cand_{uuid.uuid4().hex[:12]}",
                    incident_id,
                    camera.id,
                    camera.source_id,
                    camera.provider_camera_id,
                    index,
                    to_db(passage.estimated_passage_at),
                    passage.closest_distance_m,
                    to_db(windows.review_start),
                    to_db(windows.review_end),
                    clock.offset_seconds,
                    clock.uncertainty_seconds,
                    to_db(windows.camera_clock_start),
                    to_db(windows.camera_clock_end),
                    to_db(windows.request_start),
                    to_db(windows.request_end),
                    mode,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    confidence,
                    1 if needs_review else 0,
                    json.dumps(reasons),
                    to_db(deletion) if deletion else None,
                    decision.snapshot_id,
                    to_db(now),
                )
            )

    with conn:
        conn.execute("DELETE FROM camera_candidate WHERE incident_id = ?", (incident_id,))
        conn.executemany(
            """
            INSERT INTO camera_candidate(id, incident_id, camera_id, source_id, provider_camera_id,
                pass_index, estimated_passage_at, closest_distance_m, review_window_start,
                review_window_end, camera_clock_offset_seconds, uncertainty_seconds,
                camera_clock_window_start, camera_clock_window_end, request_window_start,
                request_window_end, calculation_method, calculation_details, confidence,
                needs_manual_review, reasons, provider_deletion_estimate, policy_snapshot_id,
                created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
    candidates = load_candidates(conn, incident_id)
    audit.record(
        conn,
        action="incident.candidates",
        resource_type="incident",
        resource_id=incident_id,
        now=now,
        settings=settings,
        detail={
            "candidates": len(candidates),
            "cameras_considered": considered,
            "excluded_sources": len(excluded),
            "needs_manual_review": sum(1 for c in candidates if c.needs_manual_review),
            "location_only": mode == METHOD_LOCATION_ONLY,
        },
    )
    return CandidateRun(incident_id, mode, len(route), considered, candidates, excluded)
