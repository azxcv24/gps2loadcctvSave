"""공통 테스트 fixture. 모든 데이터는 합성 데이터다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps2cctv import catalog, db
from gps2cctv.candidates import TrackPoint
from gps2cctv.config import Settings
from gps2cctv.geo import from_local_xy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LAT0, LON0 = 37.0, 127.0
# 합성 시나리오 기준 "현재": 사고(2026-10-26 05:32:10Z) 다음 날
NOW = datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC)
ACCIDENT = datetime(2026, 10, 26, 5, 32, 10, tzinfo=UTC)
ALL_ACTIONS = ["catalog_sync", "catalog_lookup", "preservation_request_prepare"]


@pytest.fixture
def settings() -> Settings:
    return Settings().validate()


@pytest.fixture
def conn():
    connection = db.connect_memory()
    yield connection
    connection.close()


@pytest.fixture
def city(conn, settings):
    """정책 검토를 마치고 카메라를 동기화한 합성 도시 소스."""
    catalog.register_sources(conn, FIXTURES / "sources.json", NOW, settings)
    catalog.review_source(
        conn,
        "synthetic-city",
        actions=ALL_ACTIONS,
        terms_ref="synthetic-v1",
        valid_days=30,
        reviewer="tester",
        notes=None,
        now=NOW,
        settings=settings,
    )
    catalog.sync_cameras(conn, "synthetic-city", NOW, settings)
    return "synthetic-city"


def straight_track(
    start: datetime,
    *,
    speed_mps: float = 15.0,
    duration_s: int = 120,
    step_s: int = 1,
    start_x: float = -900.0,
    y: float = 0.0,
    accuracy_m: float | None = 5.0,
) -> list[TrackPoint]:
    """동쪽으로 일정 속도로 달리는 합성 경로(기준점 기준 국소 좌표)."""
    points = []
    for k in range(0, duration_s + 1, step_s):
        lat, lon = from_local_xy(start_x + speed_mps * k, y, LAT0, LON0)
        points.append(TrackPoint(start + timedelta(seconds=k), lat, lon, accuracy_m, speed_mps))
    return points
