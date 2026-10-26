from datetime import UTC, datetime

import pytest

from gps2cctv import geo
from gps2cctv.config import ConfigError, load_settings
from gps2cctv.timeutil import (
    TimeInputError,
    ceil_to,
    floor_to,
    fmt_local,
    from_db,
    parse_duration,
    parse_timestamp,
    to_db,
)


class TestTime:
    def test_naive_timestamp_is_rejected_without_timezone(self):
        with pytest.raises(TimeInputError):
            parse_timestamp("2026-10-26T14:32:10")

    def test_naive_timestamp_with_assumed_timezone(self):
        value = parse_timestamp("2026-10-26T14:32:10", "Asia/Seoul")
        assert value == datetime(2026, 10, 26, 5, 32, 10, tzinfo=UTC)

    def test_z_suffix_and_offset(self):
        assert parse_timestamp("2026-10-26T05:32:10Z") == parse_timestamp(
            "2026-10-26T14:32:10+09:00"
        )

    def test_db_round_trip_keeps_milliseconds_and_sorts(self):
        a = datetime(2026, 10, 26, 5, 32, 10, 123000, tzinfo=UTC)
        b = datetime(2026, 10, 26, 5, 32, 9, 999000, tzinfo=UTC)
        assert from_db(to_db(a)) == a
        assert to_db(b) < to_db(a)

    def test_rounding_outward(self):
        t = datetime(2026, 10, 26, 5, 31, 22, 400000, tzinfo=UTC)
        assert floor_to(t, 10) == datetime(2026, 10, 26, 5, 31, 20, tzinfo=UTC)
        assert ceil_to(t, 10) == datetime(2026, 10, 26, 5, 31, 30, tzinfo=UTC)

    def test_duration(self):
        assert parse_duration("90") == 90
        assert parse_duration("10m") == 600
        assert parse_duration("1.5h") == 5400
        with pytest.raises(TimeInputError):
            parse_duration("10 minutes")

    def test_local_format(self):
        t = datetime(2026, 10, 26, 5, 32, 10, tzinfo=UTC)
        assert fmt_local(t, "Asia/Seoul") == "2026-10-26 14:32:10 KST"


class TestGeo:
    def test_haversine_one_degree_latitude(self):
        assert geo.haversine_m(37.0, 127.0, 38.0, 127.0) == pytest.approx(111_195, rel=1e-3)

    def test_local_projection_round_trip(self):
        lat, lon = geo.from_local_xy(1234.0, -567.0, 37.0, 127.0)
        x, y = geo.to_local_xy(lat, lon, 37.0, 127.0)
        assert (x, y) == pytest.approx((1234.0, -567.0), abs=1e-6)
        assert geo.haversine_m(37.0, 127.0, lat, lon) == pytest.approx(
            (1234.0**2 + 567.0**2) ** 0.5, rel=1e-3
        )

    def test_rounding_error_four_decimals(self):
        # 소수점 4자리 격자의 반대각선 ≈ 7.3 m (위도 37도)
        assert geo.rounding_error_m(37.0, 4) == pytest.approx(7.3, abs=0.2)

    def test_segment_through_circle(self):
        assert geo.segment_circle_interval(-10, 0, 10, 0, 5) == pytest.approx((0.25, 0.75))

    def test_segment_missing_circle(self):
        assert geo.segment_circle_interval(-10, 6, 10, 6, 5) is None

    def test_segment_starting_inside(self):
        s0, s1 = geo.segment_circle_interval(0, 0, 10, 0, 5)
        assert s0 == 0.0 and s1 == pytest.approx(0.5)

    def test_zero_length_segment(self):
        assert geo.segment_circle_interval(1, 1, 1, 1, 5) == (0.0, 1.0)
        assert geo.segment_circle_interval(9, 9, 9, 9, 5) is None

    def test_invalid_coordinates(self):
        with pytest.raises(geo.CoordinateError):
            geo.validate_lat_lon(91.0, 0.0)


class TestConfig:
    def test_defaults_are_valid(self):
        settings = load_settings(None)
        assert settings.route_precision_decimals == 4
        assert settings.allowed_hosts == ()

    def test_sections_and_hosts(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[policy]\nallowed_hosts = ["Data.Example.go.kr"]\n'
            "[candidates]\nmax_gap_seconds = 20\n",
            encoding="utf-8",
        )
        settings = load_settings(path)
        assert settings.allowed_hosts == ("data.example.go.kr",)
        assert settings.max_gap_seconds == 20.0

    def test_unknown_key_is_rejected(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[policy]\nallowed_host = []\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(path)

    def test_invalid_value_is_rejected(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[privacy]\nroute_precision_decimals = 9\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(path)
