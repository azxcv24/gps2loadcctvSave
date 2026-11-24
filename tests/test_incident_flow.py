"""합성 시나리오 전체 흐름: 사건 생성 → 경로 입력 → 후보 계산."""

from datetime import timedelta

import pytest

from gps2cctv import catalog, incidents
from gps2cctv.candidates import find_candidates, load_candidates
from gps2cctv.incidents import IncidentError
from gps2cctv.routeio import read_route
from gps2cctv.timeutil import parse_timestamp

from .conftest import ACCIDENT, FIXTURES, NOW


@pytest.fixture
def incident(conn, settings, city):
    inc = incidents.create_incident(
        conn,
        occurred_at=ACCIDENT,
        reason="합성 테스트 사건",
        now=NOW,
        settings=settings,
    )
    incidents.import_route(conn, inc.id, read_route(FIXTURES / "route.csv"), NOW, settings)
    return inc


def by_camera(candidates):
    return {c.provider_camera_id: c for c in candidates}


class TestIncidentCreation:
    def test_window_and_retention(self, conn, settings):
        inc = incidents.create_incident(
            conn,
            occurred_at=ACCIDENT,
            reason="r",
            now=NOW,
            settings=settings,
            before_seconds=600,
            after_seconds=300,
            time_uncertainty_seconds=60,
        )
        assert inc.started_at == ACCIDENT - timedelta(seconds=660)
        assert inc.ended_at == ACCIDENT + timedelta(seconds=360)
        assert inc.retention_until == NOW + timedelta(days=settings.default_incident_retention_days)

    def test_window_upper_bound(self, conn, settings):
        with pytest.raises(IncidentError, match="상한"):
            incidents.create_incident(
                conn,
                occurred_at=ACCIDENT,
                reason="r",
                now=NOW,
                settings=settings,
                before_seconds=6000,
                after_seconds=3000,
            )

    def test_future_incident_is_rejected(self, conn, settings):
        with pytest.raises(IncidentError):
            incidents.create_incident(
                conn, occurred_at=NOW + timedelta(hours=1), reason="r", now=NOW, settings=settings
            )

    def test_location_is_rounded(self, conn, settings):
        inc = incidents.create_incident(
            conn,
            occurred_at=ACCIDENT,
            reason="r",
            now=NOW,
            settings=settings,
            latitude=37.123456789,
            longitude=127.987654321,
        )
        assert (inc.latitude, inc.longitude) == (37.1235, 127.9877)

    def test_retention_bounds(self, conn, settings):
        with pytest.raises(IncidentError):
            incidents.create_incident(
                conn,
                occurred_at=ACCIDENT,
                reason="r",
                now=NOW,
                settings=settings,
                retention_days=settings.max_incident_retention_days + 1,
            )


class TestRouteImport:
    def test_only_points_inside_window_are_stored_and_rounded(self, conn, settings, city):
        inc = incidents.create_incident(
            conn, occurred_at=ACCIDENT, reason="r", now=NOW, settings=settings
        )
        points = read_route(FIXTURES / "route.csv")
        result = incidents.import_route(conn, inc.id, points, NOW, settings)
        assert result.stored + result.outside_window == len(points)
        assert result.outside_window > 0
        stored = incidents.load_route(conn, inc.id)
        # 범위 밖 지점은 경계 바로 밖 한 점씩(5분 이내)만 남긴다
        outside = [p for p in stored if not inc.started_at <= p.recorded_at <= inc.ended_at]
        assert len(outside) <= 2
        assert all(
            min(abs((p.recorded_at - inc.started_at).total_seconds()),
                abs((p.recorded_at - inc.ended_at).total_seconds())) <= 300
            for p in outside
        )  # fmt: skip
        assert all(round(p.latitude, 4) == p.latitude for p in stored)

    def test_reimport_ignores_duplicates(self, conn, settings, incident):
        points = read_route(FIXTURES / "route.csv")
        result = incidents.import_route(conn, incident.id, points, NOW, settings)
        assert result.stored == 0 and result.duplicates > 0


class TestCandidates:
    def test_expected_cameras(self, conn, settings, incident):
        run = find_candidates(conn, incident.id, NOW, settings)
        cams = by_camera(run.candidates)
        assert set(cams) == {"CAM-A-001", "CAM-A-002", "CAM-A-003", "CAM-A-005", "CAM-A-006"}
        # 사건 확인 범위 이전에만 지나간 카메라, 멀리 떨어진 카메라는 후보가 아님
        assert "CAM-A-008" not in cams and "CAM-A-004" not in cams

    def test_passage_times_match_scenario(self, conn, settings, incident):
        cams = by_camera(find_candidates(conn, incident.id, NOW, settings).candidates)
        expected = {
            "CAM-A-006": "2026-10-26T14:30:00+09:00",
            "CAM-A-005": "2026-10-26T14:28:20+09:00",
            "CAM-A-003": "2026-10-26T14:26:40+09:00",
        }
        for camera_id, when in expected.items():
            c = cams[camera_id]
            truth = parse_timestamp(when)
            assert c.review_window_start <= truth <= c.review_window_end, camera_id
            assert abs((c.estimated_passage_at - truth).total_seconds()) <= 3, camera_id

    def test_confidence_and_reasons(self, conn, settings, incident):
        cams = by_camera(find_candidates(conn, incident.id, NOW, settings).candidates)
        assert cams["CAM-A-003"].confidence == "low" and "gps_gap" in cams["CAM-A-003"].reasons
        assert "camera_status_inactive" in cams["CAM-A-005"].reasons
        assert "clock_unknown" in cams["CAM-A-006"].reasons
        assert cams["CAM-A-006"].uncertainty_seconds == settings.unknown_clock_uncertainty_seconds
        assert "route_ends_in_range" in cams["CAM-A-002"].reasons
        assert all(c.needs_manual_review for c in cams.values() if c.confidence == "low")

    def test_manual_clock_observation_is_applied(self, conn, settings, incident):
        catalog.add_time_observation(
            conn,
            "synthetic-city:CAM-A-001",
            reference_time=parse_timestamp("2026-10-24T12:00:00+09:00"),
            camera_time=parse_timestamp("2026-10-24T11:59:42+09:00"),
            uncertainty_seconds=1.0,
            drift_seconds_per_day=None,
            note=None,
            now=NOW,
            settings=settings,
        )
        c = by_camera(find_candidates(conn, incident.id, NOW, settings).candidates)["CAM-A-001"]
        assert c.camera_clock_offset_seconds == pytest.approx(-18.0)
        assert c.confidence == "high" and not c.needs_manual_review
        truth = parse_timestamp("2026-10-26T14:24:00+09:00")
        assert c.request_window_start <= truth + timedelta(seconds=-18) <= c.request_window_end
        assert c.request_window_start <= truth <= c.request_window_end
        assert c.calculation_details["clock"]["evidence"] == "manual_observation"

    def test_provider_deletion_estimate(self, conn, settings, incident):
        for c in find_candidates(conn, incident.id, NOW, settings).candidates:
            assert c.provider_deletion_estimate == c.request_window_start + timedelta(days=30)

    def test_policy_denied_source_is_excluded(self, conn, settings, incident):
        catalog.review_source(
            conn,
            "synthetic-highway",
            actions=["catalog_sync"],
            terms_ref="v1",
            valid_days=30,
            reviewer="tester",
            notes=None,
            now=NOW,
            settings=settings,
        )
        catalog.sync_cameras(conn, "synthetic-highway", NOW, settings)
        run = find_candidates(conn, incident.id, NOW, settings)
        assert ("synthetic-highway", "action_not_allowed") in run.excluded_sources
        assert all(c.source_id == "synthetic-city" for c in run.candidates)

    def test_disabling_source_stops_candidates(self, conn, settings, incident):
        catalog.set_source_enabled(conn, "synthetic-city", False, NOW, settings)
        run = find_candidates(conn, incident.id, NOW, settings)
        assert run.candidates == []
        assert ("synthetic-city", "source_disabled") in run.excluded_sources

    def test_recalculation_replaces_previous_candidates(self, conn, settings, incident):
        first = find_candidates(conn, incident.id, NOW, settings).candidates
        second = find_candidates(conn, incident.id, NOW, settings).candidates
        assert len(first) == len(second) == len(load_candidates(conn, incident.id))
        assert {c.id for c in first}.isdisjoint({c.id for c in second})

    def test_missing_camera_after_resync_is_flagged(self, conn, settings, incident, tmp_path):
        import json

        data = json.loads((FIXTURES / "cameras_city.json").read_text(encoding="utf-8"))
        data["cameras"] = [c for c in data["cameras"] if c["provider_camera_id"] != "CAM-A-002"]
        path = tmp_path / "cams.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        adapter = catalog.JsonFileCatalogAdapter(path, "synthetic-city", settings)
        result = catalog.sync_cameras(conn, "synthetic-city", NOW, settings, adapter=adapter)
        assert result.marked_missing == 1
        cams = by_camera(find_candidates(conn, incident.id, NOW, settings).candidates)
        assert "camera_missing_from_catalog" in cams["CAM-A-002"].reasons


class TestLocationOnly:
    def test_location_only_candidate(self, conn, settings, city):
        # 경로 없이 사고 위치만 알 때: 사고 지점 근처 카메라(CAM-A-002)만 후보
        from gps2cctv.geo import from_local_xy

        lat, lon = from_local_xy(6000, 4860, 37.0, 127.0)
        inc = incidents.create_incident(
            conn,
            occurred_at=ACCIDENT,
            reason="r",
            now=NOW,
            settings=settings,
            latitude=lat,
            longitude=lon,
            location_uncertainty_m=30,
            time_uncertainty_seconds=60,
        )
        run = find_candidates(conn, inc.id, NOW, settings)
        assert run.mode == "location_only_v1"
        cams = by_camera(run.candidates)
        assert set(cams) == {"CAM-A-002"}
        c = cams["CAM-A-002"]
        assert "location_only" in c.reasons and c.confidence == "low" and c.needs_manual_review
        pad = 60 + settings.location_only_padding_seconds
        assert c.review_window_start == ACCIDENT - timedelta(seconds=pad)
        assert c.review_window_end == ACCIDENT + timedelta(seconds=pad)

    def test_no_route_and_no_location_is_rejected(self, conn, settings, city):
        inc = incidents.create_incident(
            conn, occurred_at=ACCIDENT, reason="r", now=NOW, settings=settings
        )
        with pytest.raises(IncidentError):
            find_candidates(conn, inc.id, NOW, settings)
