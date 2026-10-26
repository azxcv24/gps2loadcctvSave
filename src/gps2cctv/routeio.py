"""GPS 경로 파일(CSV, GPX) 읽기.

CSV 형식(머리글 필수, UTF-8):

    recorded_at,latitude,longitude,accuracy_m,speed_mps
    2026-10-26T14:20:00+09:00,37.000000,127.000000,5.0,15.0

``accuracy_m`` 와 ``speed_mps`` 는 선택이며 비워 둘 수 있다. 정확도가 없는 지점은
후보 계산에서 보수적인 기본 정확도를 사용하고 신뢰도를 낮춘다.
"""

from __future__ import annotations

import csv
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import Gps2CctvError
from .geo import CoordinateError, validate_lat_lon
from .timeutil import TimeInputError, parse_timestamp

# GPX에는 미터 단위 정확도가 표준으로 없어 HDOP에 일반적인 거리 오차 계수를 곱해 근사한다.
GPX_HDOP_TO_METERS = 5.0
MAX_ROUTE_POINTS = 500_000


class RouteFileError(Gps2CctvError):
    """경로 파일 형식 오류."""


@dataclass(frozen=True)
class RoutePoint:
    recorded_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    speed_mps: float | None = None


def _optional_float(raw: str | None, field: str, where: str) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise RouteFileError(f"{where}: {field} 값이 숫자가 아닙니다: {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise RouteFileError(f"{where}: {field} 값은 0 이상의 숫자여야 합니다: {raw!r}")
    return value


def _required_float(raw: str | None, field: str, where: str) -> float:
    if raw is None or raw.strip() == "":
        raise RouteFileError(f"{where}: {field} 값이 비어 있습니다.")
    try:
        return float(raw)
    except ValueError as exc:
        raise RouteFileError(f"{where}: {field} 값이 숫자가 아닙니다: {raw!r}") from exc


def _make_point(
    where: str,
    time_raw: str | None,
    lat: float,
    lon: float,
    accuracy: float | None,
    speed: float | None,
    assume_tz: str | None,
) -> RoutePoint:
    if time_raw is None:
        raise RouteFileError(f"{where}: 기록 시각이 없습니다.")
    try:
        recorded_at = parse_timestamp(time_raw, assume_tz)
        validate_lat_lon(lat, lon)
    except (TimeInputError, CoordinateError) as exc:
        raise RouteFileError(f"{where}: {exc}") from exc
    return RoutePoint(recorded_at, lat, lon, accuracy, speed)


def read_csv(path: Path, assume_tz: str | None = None) -> list[RoutePoint]:
    points: list[RoutePoint] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or [])
        missing = {"recorded_at", "latitude", "longitude"} - header
        if missing:
            raise RouteFileError(
                f"{path.name}: 필수 열이 없습니다: {', '.join(sorted(missing))} "
                "(필수: recorded_at, latitude, longitude)"
            )
        for line_no, row in enumerate(reader, start=2):
            where = f"{path.name}:{line_no}"
            points.append(
                _make_point(
                    where,
                    row.get("recorded_at"),
                    _required_float(row.get("latitude"), "latitude", where),
                    _required_float(row.get("longitude"), "longitude", where),
                    _optional_float(row.get("accuracy_m"), "accuracy_m", where),
                    _optional_float(row.get("speed_mps"), "speed_mps", where),
                    assume_tz,
                )
            )
            if len(points) > MAX_ROUTE_POINTS:
                raise RouteFileError(f"{path.name}: 지점 수가 상한({MAX_ROUTE_POINTS})을 넘습니다.")
    return points


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_gpx(path: Path, assume_tz: str | None = None) -> list[RoutePoint]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RouteFileError(f"{path.name}: GPX(XML) 형식 오류: {exc}") from exc
    points: list[RoutePoint] = []
    for element in root.iter():
        if _local_name(element.tag) != "trkpt":
            continue
        where = f"{path.name}:trkpt#{len(points) + 1}"
        children = {_local_name(child.tag): (child.text or "").strip() for child in element}
        hdop = _optional_float(children.get("hdop"), "hdop", where)
        points.append(
            _make_point(
                where,
                children.get("time"),
                _required_float(element.get("lat"), "lat", where),
                _required_float(element.get("lon"), "lon", where),
                hdop * GPX_HDOP_TO_METERS if hdop is not None else None,
                _optional_float(children.get("speed"), "speed", where),
                assume_tz,
            )
        )
        if len(points) > MAX_ROUTE_POINTS:
            raise RouteFileError(f"{path.name}: 지점 수가 상한({MAX_ROUTE_POINTS})을 넘습니다.")
    return points


def read_route(path: Path, assume_tz: str | None = None) -> list[RoutePoint]:
    suffix = path.suffix.lower()
    if not path.is_file():
        raise RouteFileError(f"경로 파일이 없습니다: {path}")
    if suffix == ".csv":
        points = read_csv(path, assume_tz)
    elif suffix == ".gpx":
        points = read_gpx(path, assume_tz)
    else:
        raise RouteFileError(f"지원하지 않는 경로 파일 형식입니다: {suffix} (.csv, .gpx 지원)")
    if not points:
        raise RouteFileError(f"{path.name}: 경로 지점이 없습니다.")
    return sorted(points, key=lambda p: p.recorded_at)
