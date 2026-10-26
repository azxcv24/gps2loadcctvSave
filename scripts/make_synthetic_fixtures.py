"""합성 GPS 경로·CCTV 카탈로그 fixture 생성기.

실제 사람·차량·카메라와 무관한 가상의 데이터다. 좌표는 임의의 기준점 주변에 만든
가상 도로이며, 제공처 주소는 실제 요청이 불가능한 예약 도메인(.invalid)을 쓴다.

    python scripts/make_synthetic_fixtures.py

시나리오(표준시, KST):
- 14:20:00 동쪽으로 15 m/s 주행 시작
- 14:26:00 ~ 14:27:30 GPS 공백(지하차도 가정) — 이 사이에 북쪽으로 방향 전환
- 14:31:58 감속 시작, 14:32:10 사고 지점에 정차, 14:40:00 까지 정차
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gps2cctv.geo import from_local_xy

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LAT0, LON0 = 37.0000, 127.0000
START = datetime(2026, 10, 26, 5, 20, 0, tzinfo=UTC)  # 14:20:00 KST
ACCIDENT = datetime(2026, 10, 26, 5, 32, 10, tzinfo=UTC)  # 14:32:10 KST
END = datetime(2026, 10, 26, 5, 40, 0, tzinfo=UTC)
GAP = (datetime(2026, 10, 26, 5, 26, 0, tzinfo=UTC), datetime(2026, 10, 26, 5, 27, 30, tzinfo=UTC))
SPEED = 15.0
EAST_LEG = 6000.0
DECEL_START = ACCIDENT - timedelta(seconds=12)
SAMPLE_SECONDS = 2


def distance_at(t: datetime) -> float:
    """출발 후 경로를 따라 이동한 거리(m)."""
    elapsed = (min(t, END) - START).total_seconds()
    decel_elapsed = (DECEL_START - START).total_seconds()
    if elapsed <= decel_elapsed:
        return SPEED * elapsed
    tau = min(elapsed - decel_elapsed, 12.0)
    # 12초 동안 15 m/s → 0 등감속
    return SPEED * decel_elapsed + SPEED * tau - 0.5 * (SPEED / 12.0) * tau * tau


def speed_at(t: datetime) -> float:
    elapsed = (t - START).total_seconds()
    decel_elapsed = (DECEL_START - START).total_seconds()
    if elapsed <= decel_elapsed:
        return SPEED
    return max(0.0, SPEED - (SPEED / 12.0) * (elapsed - decel_elapsed))


def xy_at_distance(d: float) -> tuple[float, float]:
    if d <= EAST_LEG:
        return d, 0.0
    return EAST_LEG, d - EAST_LEG


def to_iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_route(rng: random.Random) -> list[dict[str, object]]:
    rows = []
    t = START
    while t <= END:
        if not (GAP[0] < t < GAP[1]):
            x, y = xy_at_distance(distance_at(t))
            accuracy = round(rng.uniform(3.0, 8.0), 1)
            noise = accuracy / 3.0
            lat, lon = from_local_xy(x + rng.gauss(0, noise), y + rng.gauss(0, noise), LAT0, LON0)
            rows.append(
                {
                    "recorded_at": to_iso(t),
                    "latitude": f"{lat:.6f}",
                    "longitude": f"{lon:.6f}",
                    "accuracy_m": accuracy,
                    "speed_mps": round(speed_at(t), 1),
                }
            )
        t += timedelta(seconds=SAMPLE_SECONDS)
    return rows


def camera_at(distance_m: float, lateral_m: float) -> tuple[float, float]:
    """경로상 거리 지점에서 진행 방향 오른쪽(+)/왼쪽(-)으로 떨어진 가상 카메라 좌표."""
    x, y = xy_at_distance(distance_m)
    if distance_m <= EAST_LEG:
        y -= lateral_m
    else:
        x += lateral_m
    return from_local_xy(x, y, LAT0, LON0)


def camera(
    pid: str,
    name: str,
    lat: float,
    lon: float,
    road: str,
    direction: str,
    status: str = "active",
    clock: dict | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "provider_camera_id": pid,
        "name": name,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "location_uncertainty_m": 10,
        "coverage_radius_m": 100,
        "road_name": road,
        "road_direction": direction,
        "status": status,
        "observed_at": "2026-10-25T15:00:00Z",
    }
    if clock:
        item["clock"] = clock
    return item


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261026)

    route = make_route(rng)
    with (OUT / "route.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(route[0]))
        writer.writeheader()
        writer.writerows(route)

    ntp_clock = {
        "reference_time": "2026-10-25T15:00:00Z",
        "offset_seconds": 0,
        "uncertainty_seconds": 5,
        "drift_seconds_per_day": 1.0,
        "note": "합성 예시: 제공처가 NTP 동기화를 고지했다고 가정",
    }
    accident_d = distance_at(ACCIDENT)
    city = [
        # 사건 확인 범위(14:21:10~) 이전에만 지나간 카메라 → 후보가 아니어야 함
        camera("CAM-A-008", "합성 1교차로", *camera_at(SPEED * 50, 30), "합성대로", "동행"),
        # 시각 오차를 사용자가 직접 관측하는 카메라(빠른 시작 안내에서 time-observe)
        camera("CAM-A-001", "합성 2교차로", *camera_at(SPEED * 240, -25), "합성대로", "동행"),
        # GPS 공백 구간의 방향 전환 지점 → 도달 가능 범위로만 추정
        camera(
            "CAM-A-003",
            "합성 지하차도 출구",
            *camera_at(EAST_LEG, 20),
            "합성대로",
            "북행",
            clock=ntp_clock,
        ),
        # 카탈로그상 비활성
        camera(
            "CAM-A-005",
            "합성 3교차로",
            *camera_at(SPEED * 500, 30),
            "합성로",
            "북행",
            status="inactive",
            clock=ntp_clock,
        ),
        # 시각 정보 없음
        camera("CAM-A-006", "합성 4교차로", *camera_at(SPEED * 600, -30), "합성로", "북행"),
        # 사고 지점 근처, 정차 후에도 촬영 반경 안
        camera(
            "CAM-A-002",
            "합성 사고지점 교차로",
            *camera_at(accident_d - 60, 40),
            "합성로",
            "북행",
            clock=ntp_clock,
        ),
        # 경로에서 멀리 떨어진 카메라 → 후보가 아니어야 함
        camera(
            "CAM-A-004",
            "합성 외곽 공원",
            *from_local_xy(3000, 2000, LAT0, LON0),
            "합성공원로",
            "양방향",
        ),
    ]
    highway = [
        # 정책 검토 전 소스 → 계산에서 제외되어야 함
        camera(
            "HWY-B-001", "합성 고속도로 1지점", *camera_at(SPEED * 150, 30), "합성고속도로", "동행"
        ),
    ]
    for filename, source_id, cams in (
        ("cameras_city.json", "synthetic-city", city),
        ("cameras_highway.json", "synthetic-highway", highway),
    ):
        payload = {
            "source_id": source_id,
            "note": "합성 테스트 데이터 — 실제 카메라가 아닙니다.",
            "cameras": cams,
        }
        (OUT / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    sources = {
        "note": "합성 테스트 소스 — 실제 기관·주소·절차가 아닙니다.",
        "sources": [
            {
                "id": "synthetic-city",
                "provider_name": "합성 도시 교통 CCTV (테스트용)",
                "environment": "synthetic",
                "capabilities": ["catalog", "preservation_request"],
                "official_endpoint": "https://city-cctv.example.invalid/api",
                "terms_url": "https://city-cctv.example.invalid/terms",
                "preservation_procedure": (
                    "합성 예시: 제공처 민원 창구에 영상정보 보존·열람 신청서를 제출 (실제 절차 아님)"
                ),
                "provider_retention_days": 30,
                "adapter": {"type": "json_file", "path": "cameras_city.json"},
            },
            {
                "id": "synthetic-highway",
                "provider_name": "합성 고속도로 CCTV (테스트용)",
                "environment": "synthetic",
                "capabilities": ["catalog", "preservation_request"],
                "official_endpoint": "https://highway-cctv.example.invalid/api",
                "terms_url": "https://highway-cctv.example.invalid/terms",
                "preservation_procedure": "합성 예시: 고객센터 보존 요청 (실제 절차 아님)",
                "provider_retention_days": 7,
                "adapter": {"type": "json_file", "path": "cameras_highway.json"},
            },
        ],
    }
    (OUT / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"생성 완료: {OUT} (경로 지점 {len(route)}개, 사고 지점 거리 {accident_d:.0f}m)")


if __name__ == "__main__":
    main()
