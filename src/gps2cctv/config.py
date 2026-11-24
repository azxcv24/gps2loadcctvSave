"""설정 값과 보수적인 기본값.

설정 파일(TOML)은 섹션을 자유롭게 나눌 수 있지만 키 이름은 아래 ``Settings`` 필드와
같아야 한다. 알 수 없는 키는 오타로 인한 안전 설정 누락을 막기 위해 오류로 처리한다.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import Gps2CctvError
from .timeutil import get_zone

# 실시간 자동 녹화(serve) 전용 섹션 — gps2cctv.live.config 가 읽는다
LIVE_SECTIONS = frozenset({"server", "devices", "its", "camera_files", "recording"})


class ConfigError(Gps2CctvError):
    """설정 파일 오류."""


@dataclass(frozen=True)
class Settings:
    # --- 개인정보·보존 ---
    # 경로 좌표 저장 정밀도(소수점 자리). 4자리 ≈ 위도 11m. 반올림 오차는 계산에 반영된다.
    route_precision_decimals: int = 4
    default_incident_retention_days: int = 90
    max_incident_retention_days: int = 1095
    audit_retention_days: int = 365
    # 사건 확인 범위(사고 전 + 사고 후 + 시각 불확실성 양쪽)의 상한. 최소 수집 원칙.
    max_incident_window_seconds: int = 7200
    default_before_seconds: int = 600
    default_after_seconds: int = 300
    default_time_uncertainty_seconds: int = 60
    default_location_uncertainty_m: float = 50.0

    # --- 후보 계산 ---
    default_coverage_radius_m: float = 100.0
    # GPS 정확도 값이 없는 지점에 적용하는 보수적인 기본값.
    default_gps_accuracy_m: float = 25.0
    # 이보다 부정확한 위치(기지국 위치 등)로 계산한 통과는 신뢰도 '낮음'
    poor_gps_accuracy_m: float = 100.0
    # GPS 기록 시각 자체의 오차 여유.
    gps_time_margin_seconds: float = 2.0
    # 이보다 긴 GPS 공백은 직선 보간 대신 도달 가능 영역(시공간 프리즘)으로 계산한다.
    max_gap_seconds: float = 30.0
    gap_speed_factor: float = 1.3
    gap_min_speed_mps: float = 5.0
    gap_max_speed_mps: float = 45.0
    # 경로 없이 사고 위치만 있을 때 사고 시각 전후로 더하는 여유.
    location_only_padding_seconds: float = 120.0
    # 보존 요청 범위가 이보다 넓으면 자동 판단하지 않고 사용자 검토로 전환한다.
    max_review_window_seconds: float = 900.0
    request_rounding_seconds: int = 10
    # CCTV 시각 정보가 전혀 없을 때 가정하는 오차(±).
    unknown_clock_uncertainty_seconds: float = 180.0
    default_clock_drift_seconds_per_day: float = 2.0
    high_confidence_max_clock_uncertainty_seconds: float = 10.0
    stale_clock_model_days: float = 30.0
    deletion_warning_days: int = 3

    # --- 정책 ---
    # production 소스의 공식 엔드포인트 호스트 허용 목록. 비어 있으면 실제 소스는 모두 거부된다.
    allowed_hosts: tuple[str, ...] = ()

    # --- 표시 ---
    display_timezone: str = "Asia/Seoul"

    def validate(self) -> Settings:
        positive = [
            "default_incident_retention_days",
            "max_incident_retention_days",
            "audit_retention_days",
            "max_incident_window_seconds",
            "default_coverage_radius_m",
            "default_gps_accuracy_m",
            "poor_gps_accuracy_m",
            "max_gap_seconds",
            "gap_speed_factor",
            "gap_min_speed_mps",
            "gap_max_speed_mps",
            "max_review_window_seconds",
            "request_rounding_seconds",
            "unknown_clock_uncertainty_seconds",
            "high_confidence_max_clock_uncertainty_seconds",
            "stale_clock_model_days",
        ]
        for name in positive:
            if getattr(self, name) <= 0:
                raise ConfigError(f"설정 {name} 은(는) 0보다 커야 합니다.")
        non_negative = [
            "default_before_seconds",
            "default_after_seconds",
            "default_time_uncertainty_seconds",
            "default_location_uncertainty_m",
            "gps_time_margin_seconds",
            "location_only_padding_seconds",
            "default_clock_drift_seconds_per_day",
            "deletion_warning_days",
        ]
        for name in non_negative:
            if getattr(self, name) < 0:
                raise ConfigError(f"설정 {name} 은(는) 음수일 수 없습니다.")
        if not 0 <= self.route_precision_decimals <= 7:
            raise ConfigError("route_precision_decimals 는 0~7 사이여야 합니다.")
        if self.default_incident_retention_days > self.max_incident_retention_days:
            raise ConfigError("기본 사건 보존 기간이 최대 보존 기간보다 깁니다.")
        if self.gap_min_speed_mps > self.gap_max_speed_mps:
            raise ConfigError("gap_min_speed_mps 가 gap_max_speed_mps 보다 큽니다.")
        get_zone(self.display_timezone)
        return self


def _coerce(name: str, default: Any, value: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"설정 {name} 은(는) true/false 여야 합니다.")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"설정 {name} 은(는) 정수여야 합니다.")
        return value
    if isinstance(default, float):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ConfigError(f"설정 {name} 은(는) 유한한 숫자여야 합니다.")
        return float(value)
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"설정 {name} 은(는) 문자열 목록이어야 합니다.")
        return tuple(v.strip().lower() for v in value if v.strip())
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"설정 {name} 은(는) 문자열이어야 합니다.")
        return value
    raise ConfigError(f"설정 {name} 의 형식을 처리할 수 없습니다.")


def load_settings(path: Path | None) -> Settings:
    base = Settings()
    if path is None:
        return base.validate()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from exc
    except IsADirectoryError as exc:
        raise ConfigError(
            f"설정 파일 자리에 폴더가 있습니다: {path} (Docker는 없는 파일을 폴더로 만듭니다. "
            "config.toml 을 먼저 만드세요)"
        ) from exc
    except PermissionError as exc:
        raise ConfigError(
            f"설정 파일을 읽을 권한이 없습니다: {path} (Docker 컨테이너는 uid 1000 으로 실행됩니다)"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"설정 파일은 UTF-8 로 저장해야 합니다: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 형식 오류: {exc}") from exc

    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key in LIVE_SECTIONS:
            continue  # 실시간 자동 녹화 설정은 live.config 에서 읽는다
        items = value.items() if isinstance(value, dict) else [(key, value)]
        for sub_key, sub_value in items:
            if sub_key in flat:
                raise ConfigError(f"설정 {sub_key} 이(가) 중복되었습니다.")
            flat[sub_key] = sub_value

    defaults = {f.name: getattr(base, f.name) for f in fields(Settings)}
    unknown = sorted(set(flat) - set(defaults))
    if unknown:
        raise ConfigError(f"알 수 없는 설정 키: {', '.join(unknown)}")
    coerced = {name: _coerce(name, defaults[name], value) for name, value in flat.items()}
    its = data.get("its")
    if isinstance(its, dict):
        # [its] 를 설정했다는 것은 소유자가 그 API 주소를 승인했다는 뜻이므로 허용 호스트에 더한다.
        endpoint = its.get("endpoint", "https://openapi.its.go.kr:9443/cctvInfo")
        host = urlparse(str(endpoint)).hostname
        if host:
            hosts = set(coerced.get("allowed_hosts", base.allowed_hosts)) | {host.lower()}
            coerced["allowed_hosts"] = tuple(sorted(hosts))
    return replace(base, **coerced).validate()
