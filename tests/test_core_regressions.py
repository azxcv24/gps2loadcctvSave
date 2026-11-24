"""초기 모듈(수동 사건 처리) 검토에서 찾은 문제의 회귀 테스트."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gps2cctv import catalog, db, incidents, retention
from gps2cctv.candidates import TrackPoint, compute_passages, find_candidates
from gps2cctv.cli import main
from gps2cctv.config import ConfigError, load_settings
from gps2cctv.geo import from_local_xy
from gps2cctv.incidents import IncidentError
from gps2cctv.routeio import RoutePoint, read_route
from gps2cctv.timeutil import TimeInputError, fmt_local, parse_timestamp

from .conftest import ACCIDENT, ALL_ACTIONS, LAT0, LON0, NOW


def _source_with_camera(conn, settings, tmp_path, x: float, y: float) -> None:
    lat, lon = from_local_xy(x, y, LAT0, LON0)
    cams = tmp_path / "cams.json"
    cams.write_text(json.dumps({"cameras": [{
        "provider_camera_id": "C1", "latitude": lat, "longitude": lon, "coverage_radius_m": 100,
        "location_uncertainty_m": 10, "status": "active", "observed_at": "2026-10-26T00:00:00Z",
    }]}), encoding="utf-8")  # fmt: skip
    src = tmp_path / "sources.json"
    src.write_text(json.dumps({"sources": [{
        "id": "test-src", "provider_name": "t", "environment": "synthetic",
        "capabilities": ["catalog"], "official_endpoint": "https://t.example.invalid/api",
        "terms_url": "https://t.example.invalid/terms", "preservation_procedure": "x",
        "adapter": {"type": "json_file", "path": str(cams)},
    }]}), encoding="utf-8")  # fmt: skip
    catalog.register_sources(conn, src, NOW, settings)
    catalog.review_source(conn, "test-src", actions=ALL_ACTIONS[:2], terms_ref="v", valid_days=30,
                          reviewer="t", notes=None, now=NOW, settings=settings)  # fmt: skip
    catalog.sync_cameras(conn, "test-src", NOW, settings)


def _east_route(start: datetime, *, step: int, seconds: int, speed: float = 15.0, x0: float = -600):
    return [
        RoutePoint(start + timedelta(seconds=k), *from_local_xy(x0 + speed * k, 0, LAT0, LON0), 5.0)
        for k in range(0, seconds + 1, step)
    ]


class TestLiveEventRetention:
    def _event(self, conn, incident_id, at):
        with conn:
            conn.execute(
                """INSERT INTO live_event(id, device_id, kind, occurred_at, detail, latitude,
                   longitude, protect_from, protect_until, incident_id, processed, created_at)
                   VALUES (?, 'dev', 'hard_brake', ?, 'x', 37.1, 127.1, ?, ?, ?, 1, ?)""",
                (f"evt_{incident_id}", at, at, at, incident_id, at),
            )

    def test_deleting_incident_deletes_its_event(self, conn, settings):
        inc = incidents.create_incident(conn, occurred_at=ACCIDENT, reason="r", now=NOW,
                                        settings=settings)  # fmt: skip
        self._event(conn, inc.id, "2026-10-26T05:32:10.000Z")
        report = retention.delete_incident(conn, inc.id, NOW, settings)
        assert report.verified and report.live_events == 1
        assert conn.execute("SELECT COUNT(*) FROM live_event").fetchone()[0] == 0

    def test_old_events_are_purged(self, conn, settings):
        self._event(conn, "none", "2026-10-26T05:32:10.000Z")
        retention.purge_expired(conn, NOW + timedelta(days=30), settings)
        assert conn.execute("SELECT COUNT(*) FROM live_event").fetchone()[0] == 1
        report = retention.purge_expired(conn, NOW + timedelta(days=91), settings)
        assert report.live_events == 1
        assert conn.execute("SELECT COUNT(*) FROM live_event").fetchone()[0] == 0


def test_coarse_precision_does_not_hide_cameras(conn, settings, tmp_path):
    coarse = replace(settings, route_precision_decimals=3)
    _source_with_camera(conn, coarse, tmp_path, 0, 175)  # 반올림 오차 포함 유효 반경 안
    inc = incidents.create_incident(
        conn, occurred_at=ACCIDENT, reason="r", now=NOW, settings=coarse
    )
    route = _east_route(ACCIDENT - timedelta(seconds=40), step=1, seconds=80)
    incidents.import_route(conn, inc.id, route, NOW, coarse)
    assert len(find_candidates(conn, inc.id, NOW, coarse).candidates) == 1


def test_passage_across_window_edge_is_found(conn, settings, tmp_path):
    """20초 간격 기록에서 범위 시작 직후 통과 — 범위 밖 직전 지점이 있어야 찾는다."""
    _source_with_camera(conn, settings, tmp_path, 0, 20)
    inc = incidents.create_incident(conn, occurred_at=ACCIDENT, reason="r", now=NOW, settings=settings,
                                    before_seconds=600, time_uncertainty_seconds=0)  # fmt: skip
    start = inc.started_at
    # 카메라(원점)를 범위 시작 5초 뒤에 지나도록, 시작 15초 전부터 20초 간격으로 기록
    route = _east_route(start - timedelta(seconds=15), step=20, seconds=120, x0=-300)
    incidents.import_route(conn, inc.id, route, NOW, settings)
    assert len(find_candidates(conn, inc.id, NOW, settings).candidates) == 1


def test_poor_accuracy_lowers_confidence(settings):
    t0 = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
    track = [TrackPoint(t0 + timedelta(seconds=k), *from_local_xy(-900 + 30 * k, 900, LAT0, LON0),
                        1500.0) for k in range(61)]  # fmt: skip
    from gps2cctv.candidates import CameraInfo

    lat, lon = from_local_xy(0, 0, LAT0, LON0)
    (passage,) = compute_passages(track, CameraInfo("c", "s", "p", lat, lon), settings, None)
    assert "gps_accuracy_poor" in passage.flags


class TestInputValidation:
    def test_dst_ambiguous_and_missing_times_rejected(self):
        with pytest.raises(TimeInputError, match="서머타임"):
            parse_timestamp("2026-10-25T02:30:00", "Europe/Berlin")  # 두 번 있는 시각
        with pytest.raises(TimeInputError, match="서머타임"):
            parse_timestamp("2026-03-29T02:30:00", "Europe/Berlin")  # 없는 시각
        assert parse_timestamp("2026-10-25T04:00:00", "Europe/Berlin").hour == 3

    def test_non_finite_values_rejected(self, conn, settings, tmp_path):
        with pytest.raises(IncidentError):
            incidents.create_incident(conn, occurred_at=ACCIDENT, reason="r", now=NOW,
                                      settings=settings, latitude=37.0, longitude=127.0,
                                      location_uncertainty_m=float("nan"))  # fmt: skip
        cfg = tmp_path / "c.toml"
        cfg.write_text("[candidates]\ngps_time_margin_seconds = nan\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(cfg)

    def test_cp949_csv_is_read(self, tmp_path):
        path = tmp_path / "경로.csv"
        path.write_bytes(
            "recorded_at,latitude,longitude,메모\n2026-10-26T05:00:00Z,37,127,출발\n".encode(
                "cp949"
            )
        )
        assert len(read_route(path)) == 1

    def test_cli_errors_are_messages_not_tracebacks(self, tmp_path, capsys):
        dbp = tmp_path / "t.sqlite3"
        assert main(["--db", str(dbp), "incident", "create", "--time", "2026-10-26T14:00:00+09:00",
                     "--reason", "r"]) == 0  # fmt: skip
        inc_id = capsys.readouterr().out.split(":")[1].split()[0].strip()
        out_dir = tmp_path / "a-directory"
        out_dir.mkdir()
        assert main(["--db", str(dbp), "incident", "report", inc_id, "--out", str(out_dir)]) == 2
        assert "저장할 수 없습니다" in capsys.readouterr().err
        conn = db.connect(dbp)
        with conn:
            conn.execute("UPDATE schema_meta SET value = '99'")
        conn.close()
        assert main(["--db", str(dbp), "incident", "list"]) == 2
        assert "스키마 버전" in capsys.readouterr().err


def test_window_display_rounds_outward():
    t = datetime(2026, 10, 26, 5, 23, 24, 799000, tzinfo=UTC)
    assert fmt_local(t, "Asia/Seoul", with_date=False, rounding="floor") == "14:23:24"
    assert fmt_local(t, "Asia/Seoul", with_date=False, rounding="ceil") == "14:23:25"
    whole = t.replace(microsecond=0)
    assert fmt_local(whole, "Asia/Seoul", with_date=False, rounding="ceil") == "14:23:24"


def test_reimport_with_replace(conn, settings):
    inc = incidents.create_incident(
        conn, occurred_at=ACCIDENT, reason="r", now=NOW, settings=settings
    )
    first = _east_route(ACCIDENT - timedelta(seconds=30), step=1, seconds=10)
    second = _east_route(ACCIDENT, step=2, seconds=10, x0=0)
    incidents.import_route(conn, inc.id, first, NOW, settings)
    incidents.import_route(conn, inc.id, second, NOW, settings, replace=True)
    stored = incidents.load_route(conn, inc.id)
    assert [p.recorded_at for p in stored] == [p.recorded_at for p in second]
