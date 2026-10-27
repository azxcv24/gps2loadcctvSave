"""실시간 자동 녹화 설정(``gps2cctv serve``).

같은 TOML 파일의 ``[server]``, ``[[devices]]``, ``[its]``, ``[[camera_files]]``, ``[recording]``
섹션을 읽는다. 나머지 섹션은 :mod:`gps2cctv.config` 가 읽는다.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ..config import ConfigError

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
ITS_API_KEY_ENV = "GPS2CCTV_ITS_API_KEY"
DATA_DIR_ENV = "GPS2CCTV_DATA_DIR"
MIN_RECOMMENDED_DEVICE_ID_LENGTH = 10


@dataclass(frozen=True)
class ServerConfig:
    listen: str = "0.0.0.0"
    port: int = 5055
    data_dir: str = "data"
    max_body_bytes: int = 65536


@dataclass(frozen=True)
class DeviceConfig:
    id: str = ""
    name: str = ""


@dataclass(frozen=True)
class ItsConfig:
    """국가교통정보센터(ITS) CCTV 오픈 API."""

    api_key: str = ""
    endpoint: str = "https://openapi.its.go.kr:9443/cctvInfo"
    # ex: 고속도로, its: 국도
    road_types: tuple[str, ...] = ("ex", "its")
    # 스트림 유형. 4 = HLS(HTTPS). 1 = HLS(HTTP). 제공처 문서에서 확인 후 조정.
    cctv_type: int = 4
    tile_degrees: float = 0.1
    refresh_minutes: float = 30.0
    # 스트림 주소에 시간 제한 토큰이 붙는 경우를 대비해 녹화 시작 전에 이보다 오래된 주소는 새로 받는다.
    stream_url_max_age_minutes: float = 5.0
    coverage_radius_m: float = 150.0
    location_uncertainty_m: float = 20.0
    request_timeout_seconds: float = 10.0
    min_request_interval_seconds: float = 1.0


@dataclass(frozen=True)
class CameraFileConfig:
    """본인 소유 카메라 등 스트림 주소가 들어 있는 JSON 카탈로그."""

    path: str = ""
    source_id: str = "my-cameras"
    provider_name: str = "본인 소유 카메라"


@dataclass(frozen=True)
class RecordingConfig:
    # 통과 구간 앞뒤로 더 저장할 시간(요청: 1~2초)
    pre_seconds: float = 2.0
    post_seconds: float = 2.0
    # 스트림 시각 추정 오차만큼 앞뒤를 더 넓힐지(넓히지 않으면 차량이 잘릴 수 있음)
    include_time_uncertainty: bool = True
    # 이 시간 안에 도착할 카메라는 미리 녹화 대기
    arm_lookahead_seconds: float = 60.0
    arm_min_radius_m: float = 300.0
    arm_heading_tolerance_deg: float = 75.0
    max_concurrent_streams: int = 6
    max_clip_seconds: float = 180.0
    buffer_seconds: float = 300.0
    # 스트림에 시각 태그(PROGRAM-DATE-TIME)가 없을 때: 촬영 → 재생목록 등장까지 추정 지연과 오차
    stream_delay_seconds: float = 3.0
    stream_delay_uncertainty_seconds: float = 4.0
    pdt_uncertainty_seconds: float = 1.0
    stream_request_timeout_seconds: float = 10.0
    # 통과 직후 장면이 스트림 버퍼에 들어올 때까지 처음 기다리는 시간(이후 2초마다 확인)
    segment_settle_seconds: float = 4.0
    clip_retention_days: int = 14
    position_retention_days: int = 14
    max_storage_gb: float = 20.0
    # trim_reencode: 정확히 잘라 mp4로 재인코딩한 보기용 사본 / remux: 자르지 않고 mp4로만 / none
    viewing_copy: str = "trim_reencode"
    # 급정거(사고 의심) 감지: window 초 안에 이만큼(m/s) 줄고 거의 멈추면 주변 클립을 보호
    hard_brake_drop_mps: float = 7.0
    hard_brake_window_seconds: float = 3.0
    hard_brake_stop_mps: float = 3.0
    event_protect_before_seconds: float = 600.0
    event_protect_after_seconds: float = 300.0
    # 사고 의심 이벤트 때 사건을 자동으로 만들고 보존 요청 후보까지 계산
    auto_incident: bool = True
    track_window_seconds: float = 900.0
    # 촬영 반경 안에서 위치가 이만큼 끊기면 마지막 위치까지의 구간을 저장
    gps_silence_seconds: float = 30.0
    # 이보다 부정확한 위치(기지국 위치 등)로는 카메라를 판단하지 않음
    max_fix_accuracy_m: float = 150.0
    stationary_radius_m: float = 30.0
    stationary_seconds: float = 120.0


@dataclass(frozen=True)
class LiveConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    devices: tuple[DeviceConfig, ...] = ()
    its: ItsConfig | None = None
    camera_files: tuple[CameraFileConfig, ...] = ()
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    base_dir: Path = Path(".")

    @property
    def data_dir(self) -> Path:
        path = Path(self.server.data_dir)
        return path if path.is_absolute() else self.base_dir / path

    def device_ids(self) -> frozenset[str]:
        return frozenset(d.id for d in self.devices)

    def warnings(self) -> list[str]:
        notes = []
        for device in self.devices:
            if len(device.id) < MIN_RECOMMENDED_DEVICE_ID_LENGTH:
                notes.append(
                    f"기기 식별자 '{device.id[:3]}…'가 짧습니다. 다른 사람이 추측해 위치를 보낼 수 "
                    f"없도록 {MIN_RECOMMENDED_DEVICE_ID_LENGTH}자 이상의 임의 문자열을 권장합니다."
                )
        if self.its is None and not self.camera_files:
            notes.append("카메라 소스가 없습니다. [its] 또는 [[camera_files]] 를 설정하세요.")
        return notes


def _build(cls, data: Any, section: str):
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] 섹션 형식이 올바르지 않습니다.")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"[{section}] 알 수 없는 설정 키: {', '.join(unknown)}")
    base = cls()
    values = {}
    for name, value in data.items():
        default = getattr(base, name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif isinstance(default, tuple):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            value = tuple(value) if ok else value
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ConfigError(f"[{section}] {name} 값의 형식이 올바르지 않습니다.")
        values[name] = value
    return replace(base, **values)


def _required(data: dict, key: str, section: str) -> Any:
    if key not in data:
        raise ConfigError(f"[{section}] {key} 값이 필요합니다.")
    return data[key]


def parse_live_config(data: dict[str, Any], base_dir: Path) -> LiveConfig:
    server = _build(ServerConfig, data.get("server"), "server")
    if not 1 <= server.port <= 65535:
        raise ConfigError("[server] port 는 1~65535 사이여야 합니다.")

    devices = []
    for item in data.get("devices") or []:
        if not isinstance(item, dict):
            raise ConfigError("[[devices]] 항목 형식이 올바르지 않습니다.")
        device = _build(DeviceConfig, {"id": _required(item, "id", "devices"), **item}, "devices")
        if not DEVICE_ID_RE.match(device.id):
            raise ConfigError("[[devices]] id 는 영문·숫자·_.:- 1~128자여야 합니다.")
        devices.append(device)
    if len({d.id for d in devices}) != len(devices):
        raise ConfigError("[[devices]] id 가 중복되었습니다.")

    its = None
    if "its" in data:
        its = _build(ItsConfig, data["its"], "its")
        api_key = os.environ.get(ITS_API_KEY_ENV) or its.api_key
        if not api_key:
            raise ConfigError(
                f"[its] api_key 가 비어 있습니다. 설정 파일이나 환경 변수 {ITS_API_KEY_ENV} 로 지정하세요."
            )
        bad = sorted(set(its.road_types) - {"ex", "its"})
        if bad or not its.road_types:
            raise ConfigError("[its] road_types 는 'ex'(고속도로), 'its'(국도) 중에서 고르세요.")
        if not 0.01 <= its.tile_degrees <= 1.0:
            raise ConfigError("[its] tile_degrees 는 0.01~1.0 사이여야 합니다.")
        its = replace(its, api_key=api_key)

    camera_files = []
    for item in data.get("camera_files") or []:
        if not isinstance(item, dict):
            raise ConfigError("[[camera_files]] 항목 형식이 올바르지 않습니다.")
        cfg = _build(
            CameraFileConfig,
            {"path": _required(item, "path", "camera_files"), **item},
            "camera_files",
        )
        camera_files.append(cfg)

    recording = _build(RecordingConfig, data.get("recording"), "recording")
    if recording.viewing_copy not in ("trim_reencode", "remux", "none"):
        raise ConfigError(
            "[recording] viewing_copy 는 trim_reencode, remux, none 중 하나여야 합니다."
        )
    for name in ("pre_seconds", "post_seconds", "stream_delay_seconds"):
        if getattr(recording, name) < 0:
            raise ConfigError(f"[recording] {name} 은(는) 음수일 수 없습니다.")
    for name in ("arm_lookahead_seconds", "max_clip_seconds", "buffer_seconds", "max_storage_gb"):
        if getattr(recording, name) <= 0:
            raise ConfigError(f"[recording] {name} 은(는) 0보다 커야 합니다.")
    if recording.buffer_seconds < recording.max_clip_seconds + recording.pre_seconds + 60:
        raise ConfigError(
            "[recording] buffer_seconds 는 max_clip_seconds + pre_seconds + 60 이상이어야 합니다"
            "(촬영 반경 안에 오래 있을 때 클립 앞부분이 지워지지 않도록)."
        )
    if recording.max_concurrent_streams < 1:
        raise ConfigError("[recording] max_concurrent_streams 는 1 이상이어야 합니다.")

    return LiveConfig(
        server=server,
        devices=tuple(devices),
        its=its,
        camera_files=tuple(camera_files),
        recording=recording,
        base_dir=base_dir,
    )


def load_live_config(path: Path) -> LiveConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 형식 오류: {exc}") from exc
    config = parse_live_config(data, path.resolve().parent)
    data_dir = os.environ.get(DATA_DIR_ENV)
    if data_dir:  # 컨테이너 등에서 저장 위치를 바꿀 때
        config = replace(config, server=replace(config.server, data_dir=data_dir))
    if not config.devices:
        raise ConfigError("[[devices]] 에 GPS를 보낼 기기를 하나 이상 등록하세요.")
    return config
