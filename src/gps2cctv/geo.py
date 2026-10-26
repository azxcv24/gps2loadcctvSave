"""거리·좌표 계산.

후보 검색은 카메라 주변 수 km 이내의 짧은 거리만 다루므로, 카메라를 원점으로 하는
등장방형(equirectangular) 국소 평면 근사를 사용한다. 수 km 범위에서 오차는 미터
이하로, GPS 정확도보다 훨씬 작다.
"""

from __future__ import annotations

import math

from .errors import Gps2CctvError

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE = EARTH_RADIUS_M * math.pi / 180.0


class CoordinateError(Gps2CctvError):
    """좌표 범위 오류."""


def validate_lat_lon(lat: float, lon: float) -> None:
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise CoordinateError("좌표가 숫자가 아닙니다.")
    if not -90.0 <= lat <= 90.0:
        raise CoordinateError(f"위도 범위(-90~90)를 벗어났습니다: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise CoordinateError(f"경도 범위(-180~180)를 벗어났습니다: {lon}")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def round_coord(value: float, decimals: int) -> float:
    return round(value, decimals)


def rounding_error_m(lat: float, decimals: int) -> float:
    """소수점 ``decimals`` 자리로 반올림했을 때 생길 수 있는 최대 위치 오차(격자 반대각선)."""
    half_step = 0.5 * 10.0 ** (-decimals)
    dy = half_step * METERS_PER_DEGREE
    dx = half_step * METERS_PER_DEGREE * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


def to_local_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """(lat0, lon0)를 원점으로 하는 동(x)·북(y) 방향 미터 좌표."""
    x = (lon - lon0) * METERS_PER_DEGREE * math.cos(math.radians(lat0))
    y = (lat - lat0) * METERS_PER_DEGREE
    return x, y


def from_local_xy(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + y / METERS_PER_DEGREE
    lon = lon0 + x / (METERS_PER_DEGREE * math.cos(math.radians(lat0)))
    return lat, lon


def degree_margins(lat: float, radius_m: float) -> tuple[float, float]:
    """반경 ``radius_m`` 를 덮는 위도·경도 여유(도)."""
    dlat = radius_m / METERS_PER_DEGREE
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_m / (METERS_PER_DEGREE * cos_lat)
    return dlat, dlon


def segment_circle_interval(
    ax: float, ay: float, bx: float, by: float, radius: float
) -> tuple[float, float] | None:
    """원점 중심 반지름 ``radius`` 원 안에 들어가는 선분 A→B의 매개변수 구간 [s0, s1].

    선분 위의 점은 A + s(B - A), s ∈ [0, 1]. 원과 만나지 않으면 None.
    """
    dx, dy = bx - ax, by - ay
    a = dx * dx + dy * dy
    c = ax * ax + ay * ay - radius * radius
    if a < 1e-12:
        return (0.0, 1.0) if c <= 0 else None
    b = 2.0 * (ax * dx + ay * dy)
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    s0 = (-b - root) / (2.0 * a)
    s1 = (-b + root) / (2.0 * a)
    lo, hi = max(s0, 0.0), min(s1, 1.0)
    if lo > hi:
        return None
    return lo, hi


def closest_parameter(ax: float, ay: float, bx: float, by: float) -> float:
    """원점에 가장 가까운 선분 A→B 위의 매개변수 s ∈ [0, 1]."""
    dx, dy = bx - ax, by - ay
    a = dx * dx + dy * dy
    if a < 1e-12:
        return 0.0
    return min(1.0, max(0.0, -(ax * dx + ay * dy) / a))
