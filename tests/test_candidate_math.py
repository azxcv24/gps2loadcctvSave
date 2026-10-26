"""후보 시간 범위 계산(순수 함수) 테스트. 합성 경로·카메라만 사용한다."""

from datetime import UTC, datetime, timedelta

import pytest

from gps2cctv.candidates import (
    CameraInfo,
    ClockModel,
    TrackPoint,
    assess,
    build_windows,
    compute_passages,
    deletion_status,
    resolve_clock,
)
from gps2cctv.geo import from_local_xy

from .conftest import LAT0, LON0, straight_track

T0 = datetime(2026, 10, 26, 5, 30, 0, tzinfo=UTC)


def cam(x: float = 0.0, y: float = 0.0, **kwargs) -> CameraInfo:
    lat, lon = from_local_xy(x, y, LAT0, LON0)
    return CameraInfo("src:CAM", "src", "CAM", lat, lon, **kwargs)


def kst(h: int, m: int, s: int) -> datetime:
    return datetime(2026, 10, 26, h - 9, m, s, tzinfo=UTC)


class TestReadmeExample:
    """README의 시간 범위 계산 예시를 그대로 재현한다."""

    def test_example_numbers(self):
        passage = kst(14, 32, 10)
        windows = build_windows(
            passage - timedelta(seconds=30),
            passage + timedelta(seconds=30),
            offset_seconds=-18,
            uncertainty_seconds=0,
            rounding_seconds=10,
        )
        assert windows.review_start == kst(14, 31, 40)
        assert windows.review_end == kst(14, 32, 40)
        assert windows.camera_clock_start == kst(14, 31, 22)
        assert windows.camera_clock_end == kst(14, 32, 22)
        # 요청 범위는 두 범위를 모두 덮고 10초 단위로 바깥쪽 반올림
        assert windows.request_start == kst(14, 31, 20)
        assert windows.request_end == kst(14, 32, 40)


class TestPassages:
    def test_passage_time_of_straight_route(self, settings):
        # x=-900m 에서 15 m/s → 원점(카메라)을 60초 뒤 통과
        track = straight_track(T0, duration_s=120, y=20.0)
        passages = compute_passages(track, cam(), settings, precision_decimals=None)
        assert len(passages) == 1
        p = passages[0]
        assert abs((p.estimated_passage_at - (T0 + timedelta(seconds=60))).total_seconds()) <= 1
        assert p.closest_distance_m == pytest.approx(20.0, abs=0.5)
        assert p.window_start < p.estimated_passage_at < p.window_end
        # 유효 반경 = 100 + 5 + 0 + 10 = 115 m → 반경 안 약 ±7.6초 + 기록 간격·시각 여유
        width = (p.window_end - p.window_start).total_seconds()
        assert 15 <= width <= 22
        assert not p.flags

    def test_far_camera_has_no_passage(self, settings):
        track = straight_track(T0, y=500.0)
        assert compute_passages(track, cam(), settings, None) == []

    def test_rounding_error_widens_effective_radius(self, settings):
        track = straight_track(T0)
        exact = compute_passages(track, cam(), settings, None)[0]
        rounded = compute_passages(track, cam(), settings, 4)[0]
        assert rounded.details["rounding_error_m"] > 0
        assert rounded.details["effective_radius_m"] > exact.details["effective_radius_m"]

    def test_missing_accuracy_uses_default_and_flags(self, settings):
        track = straight_track(T0, accuracy_m=None)
        p = compute_passages(track, cam(), settings, None)[0]
        assert "gps_accuracy_unknown" in p.flags
        assert p.details["gps_accuracy_m_max"] == settings.default_gps_accuracy_m

    def test_two_separate_passes(self, settings):
        # 동쪽으로 지나간 뒤 5분 뒤 서쪽으로 다시 지나감
        out = straight_track(T0, duration_s=120)
        back_start = T0 + timedelta(seconds=420)
        back = [
            TrackPoint(
                back_start + timedelta(seconds=k), *from_local_xy(900 - 15 * k, 0, LAT0, LON0), 5.0
            )
            for k in range(0, 121)
        ]
        # 두 구간 사이는 카메라에서 먼 곳에 머무름
        park = [
            TrackPoint(T0 + timedelta(seconds=s), *from_local_xy(900, 0, LAT0, LON0), 5.0)
            for s in range(121, 420, 10)
        ]
        passages = compute_passages(out + park + back, cam(), settings, None)
        assert len(passages) == 2
        assert passages[0].estimated_passage_at < passages[1].estimated_passage_at

    def test_route_starting_and_ending_in_range(self, settings):
        track = straight_track(T0, start_x=-30.0, duration_s=4)
        p = compute_passages(track, cam(), settings, None)[0]
        assert {"route_starts_in_range", "route_ends_in_range"} <= p.flags


class TestGap:
    def _gap_track(self, gap_s: int = 90):
        # 모서리(원점, 카메라 위치) 600m 전에서 GPS가 끊기고,
        # 모서리를 돌아 북쪽 750m 지점에서 다시 잡힘(15 m/s로 1350m = 90초)
        before = [
            TrackPoint(
                T0 + timedelta(seconds=k), *from_local_xy(-1200 + 15 * k, 0, LAT0, LON0), 5.0, 15.0
            )
            for k in range(0, 41)
        ]
        t_gap_end = T0 + timedelta(seconds=40 + gap_s)
        after = [
            TrackPoint(
                t_gap_end + timedelta(seconds=k),
                *from_local_xy(0, 750 + 15 * k, LAT0, LON0),
                5.0,
                15.0,
            )
            for k in range(0, 31)
        ]
        return before + after

    def test_corner_camera_in_gap_is_found_with_prism(self, settings):
        # 직선 보간이면 모서리(원점)에서 ~470m 떨어진 현을 지나 놓치는 카메라
        track = self._gap_track()
        passages = compute_passages(track, cam(), settings, None)
        assert len(passages) == 1
        p = passages[0]
        assert "gps_gap" in p.flags
        assert p.closest_distance_m is None
        # 실제 통과 시각(공백 시작 40초 뒤 = T0+80s)이 범위 안에 있어야 함
        true_passage = T0 + timedelta(seconds=80)
        assert p.window_start <= true_passage <= p.window_end
        assert p.details["gap_speed_limit_mps"] >= 15.0

    def test_straight_interpolation_would_miss_corner_camera(self, settings):
        # 공백을 직선으로 보간하면(공백 기준을 크게 잡으면) 같은 카메라를 놓친다 — 프리즘을 쓰는 이유
        from dataclasses import replace

        linear_only = replace(settings, max_gap_seconds=10_000)
        assert compute_passages(self._gap_track(), cam(), linear_only, None) == []

    def test_unreachable_camera_in_gap_is_excluded(self, settings):
        track = self._gap_track()
        assert compute_passages(track, cam(x=-3000, y=3000), settings, None) == []


class TestClock:
    def test_unknown_clock_is_conservative(self, settings):
        est = resolve_clock([], T0, settings)
        assert est.evidence == "unknown"
        assert est.uncertainty_seconds == settings.unknown_clock_uncertainty_seconds
        assert "clock_unknown" in est.reasons

    def test_uncertainty_grows_with_age(self, settings):
        model = ClockModel("m1", T0 - timedelta(days=3), -18.0, 1.0, 2.0, "manual_observation")
        est = resolve_clock([model], T0, settings)
        assert est.offset_seconds == -18.0
        assert est.uncertainty_seconds == pytest.approx(1.0 + 2.0 * 3)
        assert est.reasons == frozenset()

    def test_nearest_model_wins_and_stale_flag(self, settings):
        old = ClockModel("old", T0 - timedelta(days=60), -30.0, 1.0, 0.5, "manual_observation")
        new = ClockModel("new", T0 + timedelta(days=1), -5.0, 1.0, 0.5, "provider_declared")
        assert resolve_clock([old, new], T0, settings).model_id == "new"
        assert "clock_model_stale" in resolve_clock([old], T0, settings).reasons

    def test_camera_window_shift_direction(self):
        # 카메라 시계가 18초 느리면 CCTV 표시 시각 기준 범위는 18초 앞당겨진다
        w = build_windows(T0, T0 + timedelta(seconds=20), -18, 2, 10)
        assert w.camera_clock_start == T0 - timedelta(seconds=20)
        assert w.camera_clock_end == T0 + timedelta(seconds=4)
        assert w.request_start <= w.camera_clock_start and w.request_end >= w.review_end


class TestAssessment:
    def _windows(self, width_s: float):
        return build_windows(T0, T0 + timedelta(seconds=width_s), 0, 1, 10)

    def test_high_confidence(self, settings):
        clock = resolve_clock(
            [ClockModel("m", T0, 0.0, 1.0, 1.0, "provider_declared")], T0, settings
        )
        confidence, review, reasons = assess(
            frozenset(), clock, self._windows(30), "active", settings
        )
        assert (confidence, review, reasons) == ("high", False, [])

    def test_unknown_clock_needs_review(self, settings):
        clock = resolve_clock([], T0, settings)
        confidence, review, _ = assess(frozenset(), clock, self._windows(30), "active", settings)
        assert confidence == "low" and review

    def test_too_wide_window_needs_review(self, settings):
        clock = resolve_clock(
            [ClockModel("m", T0, 0.0, 1.0, 0.0, "provider_declared")], T0, settings
        )
        confidence, review, reasons = assess(
            frozenset(),
            clock,
            self._windows(settings.max_review_window_seconds + 60),
            "active",
            settings,
        )
        assert "window_too_wide" in reasons and review and confidence == "low"

    def test_inactive_camera_is_low(self, settings):
        clock = resolve_clock(
            [ClockModel("m", T0, 0.0, 1.0, 0.0, "provider_declared")], T0, settings
        )
        confidence, _, reasons = assess(frozenset(), clock, self._windows(30), "inactive", settings)
        assert confidence == "low" and "camera_status_inactive" in reasons


def test_deletion_status(settings):
    assert deletion_status(None, T0, settings) == "unknown"
    assert deletion_status(T0 - timedelta(hours=1), T0, settings) == "passed"
    assert deletion_status(T0 + timedelta(days=1), T0, settings) == "imminent"
    assert deletion_status(T0 + timedelta(days=20), T0, settings) == "ok"
