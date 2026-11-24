"""사건 생성과 GPS 경로 입력.

- 사건 확인 범위(사고 전·후)는 설정 상한을 넘을 수 없다(최소 수집).
- 경로 지점은 사건 확인 범위 안의 것만 저장하고, 좌표는 설정된 정밀도로 낮춰 저장한다.
- 모든 사건에는 보존 종료 시각이 있다.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import audit
from .config import Settings
from .errors import Gps2CctvError, NotFoundError
from .geo import round_coord, validate_lat_lon
from .routeio import RoutePoint
from .timeutil import from_db, shift, to_db

LOCAL_OWNER = "local-user"
MAX_REASON_LENGTH = 500


class IncidentError(Gps2CctvError):
    """사건 입력 오류."""


@dataclass(frozen=True)
class Incident:
    id: str
    occurred_at: datetime
    occurred_at_uncertainty_seconds: float
    window_before_seconds: float
    window_after_seconds: float
    started_at: datetime
    ended_at: datetime
    latitude: float | None
    longitude: float | None
    location_uncertainty_m: float | None
    precision_decimals: int
    reason: str
    status: str
    retention_until: datetime
    created_at: datetime

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _from_row(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        occurred_at=from_db(row["occurred_at"]),
        occurred_at_uncertainty_seconds=row["occurred_at_uncertainty_seconds"],
        window_before_seconds=row["window_before_seconds"],
        window_after_seconds=row["window_after_seconds"],
        started_at=from_db(row["started_at"]),
        ended_at=from_db(row["ended_at"]),
        latitude=row["latitude_rounded"],
        longitude=row["longitude_rounded"],
        location_uncertainty_m=row["location_uncertainty_m"],
        precision_decimals=row["location_precision_decimals"],
        reason=row["reason"],
        status=row["status"],
        retention_until=from_db(row["retention_until"]),
        created_at=from_db(row["created_at"]),
    )


def create_incident(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    reason: str,
    now: datetime,
    settings: Settings,
    time_uncertainty_seconds: float | None = None,
    before_seconds: float | None = None,
    after_seconds: float | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_uncertainty_m: float | None = None,
    retention_days: int | None = None,
) -> Incident:
    reason = reason.strip()
    if not reason:
        raise IncidentError("사건 사유(--reason)를 입력하세요.")
    if len(reason) > MAX_REASON_LENGTH:
        raise IncidentError(f"사건 사유는 {MAX_REASON_LENGTH}자 이하로 입력하세요.")
    if occurred_at > now + timedelta(minutes=5):
        raise IncidentError("사고 시점이 현재보다 미래입니다.")

    unc = (
        settings.default_time_uncertainty_seconds
        if time_uncertainty_seconds is None
        else (time_uncertainty_seconds)
    )
    before = settings.default_before_seconds if before_seconds is None else before_seconds
    after = settings.default_after_seconds if after_seconds is None else after_seconds
    if not all(math.isfinite(v) for v in (unc, before, after)):
        raise IncidentError("시간 범위와 불확실성은 유한한 숫자여야 합니다.")
    if unc < 0 or before < 0 or after < 0:
        raise IncidentError("시간 범위와 불확실성은 음수일 수 없습니다.")
    total = before + after + 2 * unc
    if total > settings.max_incident_window_seconds:
        raise IncidentError(
            f"사건 확인 범위(사고 전 {before:.0f}초 + 후 {after:.0f}초 + 시각 불확실성 ±{unc:.0f}초 "
            f"= {total:.0f}초)가 상한 {settings.max_incident_window_seconds}초를 넘습니다. "
            "최소 범위만 다루도록 줄여 주세요."
        )

    if (latitude is None) != (longitude is None):
        raise IncidentError("위도와 경도는 함께 입력해야 합니다.")
    lat_r = lon_r = loc_unc = None
    if latitude is not None and longitude is not None:
        validate_lat_lon(latitude, longitude)
        lat_r = round_coord(latitude, settings.route_precision_decimals)
        lon_r = round_coord(longitude, settings.route_precision_decimals)
        loc_unc = (
            settings.default_location_uncertainty_m
            if location_uncertainty_m is None
            else location_uncertainty_m
        )
        if not math.isfinite(loc_unc) or loc_unc < 0 or loc_unc > 100_000:
            raise IncidentError("위치 불확실성은 0~100000m 사이의 숫자여야 합니다.")

    days = settings.default_incident_retention_days if retention_days is None else retention_days
    if not 1 <= days <= settings.max_incident_retention_days:
        raise IncidentError(
            f"보존 기간은 1~{settings.max_incident_retention_days}일 사이여야 합니다."
        )

    incident_id = f"inc_{uuid.uuid4().hex[:12]}"
    started = shift(occurred_at, -(before + unc))
    ended = shift(occurred_at, after + unc)
    with conn:
        conn.execute(
            """
            INSERT INTO incident(id, owner_id, occurred_at, occurred_at_uncertainty_seconds,
                window_before_seconds, window_after_seconds, started_at, ended_at,
                latitude_rounded, longitude_rounded, location_uncertainty_m,
                location_precision_decimals, reason, status, retention_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                incident_id,
                LOCAL_OWNER,
                to_db(occurred_at),
                unc,
                before,
                after,
                to_db(started),
                to_db(ended),
                lat_r,
                lon_r,
                loc_unc,
                settings.route_precision_decimals,
                reason,
                to_db(now + timedelta(days=days)),
                to_db(now),
            ),
        )
    audit.record(
        conn,
        action="incident.create",
        resource_type="incident",
        resource_id=incident_id,
        now=now,
        settings=settings,
        detail={"has_location": lat_r is not None, "retention_days": days},
    )
    return get_incident(conn, incident_id)


def get_incident(conn: sqlite3.Connection, incident_id: str) -> Incident:
    row = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"사건을 찾을 수 없습니다: {incident_id}")
    return _from_row(row)


def list_incidents(conn: sqlite3.Connection) -> list[Incident]:
    rows = conn.execute("SELECT * FROM incident ORDER BY occurred_at DESC").fetchall()
    return [_from_row(row) for row in rows]


@dataclass(frozen=True)
class RouteImportResult:
    stored: int
    outside_window: int
    duplicates: int


# 사건 확인 범위 바로 밖의 지점도 이 시간 안이면 하나씩 저장한다. 범위 경계를 가로지르는
# 구간(기록 간격이 길거나 GPS가 끊긴 경우)의 통과를 놓치지 않기 위함.
BOUNDARY_POINT_MAX_SECONDS = 300


def _select_points(points: list[RoutePoint], start: datetime, end: datetime) -> list[RoutePoint]:
    ordered = sorted(points, key=lambda p: p.recorded_at)
    inside = [p for p in ordered if start <= p.recorded_at <= end]
    before = [p for p in ordered if p.recorded_at < start]
    after = [p for p in ordered if p.recorded_at > end]
    limit = timedelta(seconds=BOUNDARY_POINT_MAX_SECONDS)
    selected = list(inside)
    if before and start - before[-1].recorded_at <= limit:
        selected.insert(0, before[-1])
    if after and after[0].recorded_at - end <= limit:
        selected.append(after[0])
    return selected


def import_route(
    conn: sqlite3.Connection,
    incident_id: str,
    points: list[RoutePoint],
    now: datetime,
    settings: Settings,
    *,
    replace: bool = False,
) -> RouteImportResult:
    """사건 확인 범위 안(+경계 바로 밖 한 점씩)의 경로 지점만 정밀도를 낮춰 저장한다.

    ``replace`` 가 참이면 기존 경로를 지우고 새로 저장한다(잘못 넣은 경로 교체).
    """
    incident = get_incident(conn, incident_id)
    decimals = incident.precision_decimals
    selected = _select_points(points, incident.started_at, incident.ended_at)
    stored = duplicates = 0
    outside = len(points) - len(selected)
    with conn:
        if replace:
            conn.execute("DELETE FROM route_point WHERE incident_id = ?", (incident_id,))
        for point in selected:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO route_point(incident_id, recorded_at, latitude_rounded,
                    longitude_rounded, precision_decimals, accuracy_m, speed_mps)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    to_db(point.recorded_at),
                    round_coord(point.latitude, decimals),
                    round_coord(point.longitude, decimals),
                    decimals,
                    point.accuracy_m,
                    point.speed_mps,
                ),
            )
            if cursor.rowcount:
                stored += 1
            else:
                duplicates += 1
    audit.record(
        conn,
        action="incident.route_import",
        resource_type="incident",
        resource_id=incident_id,
        now=now,
        settings=settings,
        detail={
            "stored": stored,
            "outside_window": outside,
            "duplicates": duplicates,
            "replaced": replace,
        },
    )
    return RouteImportResult(stored, outside, duplicates)


@dataclass(frozen=True)
class StoredPoint:
    recorded_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None
    speed_mps: float | None


def load_route(conn: sqlite3.Connection, incident_id: str) -> list[StoredPoint]:
    rows = conn.execute(
        """
        SELECT recorded_at, latitude_rounded, longitude_rounded, accuracy_m, speed_mps
        FROM route_point WHERE incident_id = ? ORDER BY recorded_at
        """,
        (incident_id,),
    ).fetchall()
    return [
        StoredPoint(
            from_db(r["recorded_at"]),
            r["latitude_rounded"],
            r["longitude_rounded"],
            r["accuracy_m"],
            r["speed_mps"],
        )
        for r in rows
    ]
