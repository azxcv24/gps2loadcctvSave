"""공식 CCTV 소스·정책 검토·카메라 카탈로그 관리.

- 소스 등록 시 엔드포인트·약관 URL을 검증해 허용 목록 밖의 소스는 등록 자체를 거부한다(FR-03).
- 카메라 동기화는 정책 검증(catalog_sync)을 통과해야만 실행한다.
- 어댑터는 현재 로컬 JSON 파일(합성 카탈로그·제공처가 배포한 파일)만 지원한다.
  실제 HTTP API 어댑터는 제공처별 약관 검토 뒤 M2에서 추가한다.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import audit
from .config import Settings
from .errors import Gps2CctvError, NotFoundError, PolicyDeniedError
from .geo import CoordinateError, validate_lat_lon
from .policy import (
    CAPABILITIES,
    REASON_TEXT,
    REVIEWABLE_ACTIONS,
    check_policy,
    endpoint_problem,
    latest_snapshot,
    terms_problem,
)
from .timeutil import TimeInputError, from_db, parse_timestamp, to_db

ENVIRONMENTS = ("synthetic", "production", "owned")
# json_file: 로컬 JSON 카탈로그 / its: 국가교통정보센터 CCTV API(실시간 자동 녹화에서 주변만 갱신)
ADAPTER_TYPES = ("json_file", "its")
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,63}$")
PROVIDER_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
CAMERA_STATUSES = frozenset({"active", "inactive", "unknown"})
MAX_CAMERAS_PER_SYNC = 200_000
MAX_CLOCK_OFFSET_SECONDS = 86_400.0


class CatalogError(Gps2CctvError):
    """소스·카탈로그 형식 오류."""


def _text(data: dict[str, Any], key: str, where: str, *, max_len: int, required: bool = True):
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CatalogError(f"{where}: '{key}' 값이 필요합니다.")
        return None
    if not isinstance(value, str):
        raise CatalogError(f"{where}: '{key}' 값은 문자열이어야 합니다.")
    value = value.strip()
    if len(value) > max_len:
        raise CatalogError(f"{where}: '{key}' 값이 너무 깁니다(최대 {max_len}자).")
    return value


def _number(
    data: dict[str, Any],
    key: str,
    where: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    required: bool = False,
) -> float | None:
    value = data.get(key, default)
    if value is None:
        if required:
            raise CatalogError(f"{where}: '{key}' 값이 필요합니다.")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CatalogError(f"{where}: '{key}' 값은 숫자여야 합니다.")
    if minimum is not None and value < minimum:
        raise CatalogError(f"{where}: '{key}' 값은 {minimum} 이상이어야 합니다.")
    if maximum is not None and value > maximum:
        raise CatalogError(f"{where}: '{key}' 값은 {maximum} 이하여야 합니다.")
    return float(value)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"파일이 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path.name}: JSON 형식 오류: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CatalogError(f"{path.name}: UTF-8 로 저장된 JSON 이어야 합니다.") from exc
    except OSError as exc:
        raise CatalogError(f"파일을 읽을 수 없습니다: {path} ({exc.strerror})") from exc


# ---------------------------------------------------------------------------
# 소스 등록·정책 검토
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    id: str
    provider_name: str
    environment: str
    capabilities: tuple[str, ...]
    official_endpoint: str
    terms_url: str
    preservation_procedure: str
    provider_retention_days: int | None
    adapter: dict[str, str]


def parse_source_spec(data: Any, base_dir: Path, settings: Settings) -> SourceSpec:
    if not isinstance(data, dict):
        raise CatalogError("소스 항목은 객체여야 합니다.")
    source_id = _text(data, "id", "소스", max_len=64)
    where = f"소스 {source_id}"
    if not SOURCE_ID_RE.match(source_id):
        raise CatalogError(f"{where}: id는 영문 소문자·숫자·하이픈 2~64자여야 합니다.")
    environment = _text(data, "environment", where, max_len=20)
    if environment not in ENVIRONMENTS:
        raise CatalogError(f"{where}: environment는 {', '.join(ENVIRONMENTS)} 중 하나여야 합니다.")
    owned = environment == "owned"
    capabilities = data.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or not all(isinstance(c, str) and c in CAPABILITIES for c in capabilities)
    ):
        raise CatalogError(
            f"{where}: capabilities는 {sorted(CAPABILITIES)} 중 하나 이상의 목록이어야 합니다."
        )
    # 본인 소유 장비(owned)는 외부 제공처가 아니므로 공식 엔드포인트·약관이 없어도 된다.
    endpoint = _text(data, "official_endpoint", where, max_len=500, required=not owned) or ""
    terms_url = _text(data, "terms_url", where, max_len=500, required=not owned) or ""
    if not owned:
        problem = endpoint_problem(environment, endpoint, settings)
        if problem:
            raise CatalogError(f"{where}: 등록 거부 — {REASON_TEXT[problem]} ({endpoint})")
        if terms_problem(terms_url):
            raise CatalogError(f"{where}: 등록 거부 — {REASON_TEXT['terms_url_invalid']}")
    procedure = (
        _text(data, "preservation_procedure", where, max_len=2000, required=not owned)
        or "본인 소유 장비 — 직접 보관"
    )
    retention = _number(data, "provider_retention_days", where, minimum=1, maximum=3650)
    adapter = data.get("adapter")
    if not isinstance(adapter, dict) or adapter.get("type") not in ADAPTER_TYPES:
        raise CatalogError(
            f"{where}: adapter.type 은 {', '.join(ADAPTER_TYPES)} 중 하나여야 합니다."
        )
    if adapter["type"] == "json_file":
        adapter_path = _text(adapter, "path", f"{where} adapter", max_len=1000)
        adapter_config = {"type": "json_file", "path": str((base_dir / adapter_path).resolve())}
    else:
        adapter_config = {"type": adapter["type"]}
    return SourceSpec(
        id=source_id,
        provider_name=_text(data, "provider_name", where, max_len=200),
        environment=environment,
        capabilities=tuple(sorted(set(capabilities))),
        official_endpoint=endpoint,
        terms_url=terms_url,
        preservation_procedure=procedure,
        provider_retention_days=int(retention) if retention is not None else None,
        adapter=adapter_config,
    )


def register_sources(
    conn: sqlite3.Connection, path: Path, now: datetime, settings: Settings
) -> list[str]:
    """소스 정의 파일을 읽어 등록(또는 갱신)한다. 하나라도 잘못되면 전체를 거부한다."""
    data = _load_json(path)
    items = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise CatalogError(f"{path.name}: 'sources' 목록이 필요합니다.")
    specs = [parse_source_spec(item, path.parent, settings) for item in items]
    return register_specs(conn, specs, now, settings)


def register_specs(
    conn: sqlite3.Connection, specs: list[SourceSpec], now: datetime, settings: Settings
) -> list[str]:
    """검증을 마친 소스 정의를 등록(또는 갱신)한다."""
    ids = [spec.id for spec in specs]
    if len(set(ids)) != len(ids):
        raise CatalogError("소스 id가 중복되었습니다.")

    stamp = to_db(now)
    with conn:
        for spec in specs:
            existing = conn.execute("SELECT * FROM cctv_source WHERE id = ?", (spec.id,)).fetchone()
            capabilities = json.dumps(list(spec.capabilities))
            adapter = json.dumps(spec.adapter, ensure_ascii=False)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cctv_source(id, provider_name, environment, capabilities,
                        official_endpoint, terms_url, preservation_procedure,
                        provider_retention_days, adapter, enabled, last_verified_at,
                        material_changed_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)
                    """,
                    (
                        spec.id,
                        spec.provider_name,
                        spec.environment,
                        capabilities,
                        spec.official_endpoint,
                        spec.terms_url,
                        spec.preservation_procedure,
                        spec.provider_retention_days,
                        adapter,
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                continue
            material_changed = (
                existing["environment"] != spec.environment
                or existing["capabilities"] != capabilities
                or existing["official_endpoint"] != spec.official_endpoint
                or existing["terms_url"] != spec.terms_url
                or existing["adapter"] != adapter
            )
            conn.execute(
                """
                UPDATE cctv_source SET provider_name = ?, environment = ?, capabilities = ?,
                    official_endpoint = ?, terms_url = ?, preservation_procedure = ?,
                    provider_retention_days = ?, adapter = ?, material_changed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    spec.provider_name,
                    spec.environment,
                    capabilities,
                    spec.official_endpoint,
                    spec.terms_url,
                    spec.preservation_procedure,
                    spec.provider_retention_days,
                    adapter,
                    stamp if material_changed else existing["material_changed_at"],
                    stamp,
                    spec.id,
                ),
            )
    for spec in specs:
        audit.record(
            conn,
            action="source.register",
            resource_type="cctv_source",
            resource_id=spec.id,
            now=now,
            settings=settings,
        )
    return ids


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cctv_source WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"등록되지 않은 소스입니다: {source_id}")
    return row


def review_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    actions: list[str],
    terms_ref: str,
    valid_days: int,
    reviewer: str,
    notes: str | None,
    now: datetime,
    settings: Settings,
) -> str:
    """사람(또는 승인된 자동화)이 약관·절차를 검토한 결과를 정책 검토 기록으로 남긴다."""
    get_source(conn, source_id)
    if not actions:
        raise CatalogError("허용할 행위를 하나 이상 지정하세요.")
    unknown = sorted(set(actions) - REVIEWABLE_ACTIONS)
    if unknown:
        raise CatalogError(
            f"알 수 없는 행위: {', '.join(unknown)} (가능: {', '.join(sorted(REVIEWABLE_ACTIONS))})"
        )
    terms_ref = terms_ref.strip()
    reviewer = reviewer.strip()
    if not terms_ref or len(terms_ref) > 200:
        raise CatalogError("약관 버전 또는 해시(--terms-ref)는 1~200자로 입력하세요.")
    if not reviewer or len(reviewer) > 100:
        raise CatalogError("검토자(--reviewer)는 1~100자로 입력하세요.")
    if notes is not None and len(notes) > 1000:
        raise CatalogError("메모는 1000자 이하로 입력하세요.")
    if not 1 <= valid_days <= 365:
        raise CatalogError("정책 검토 유효 기간은 1~365일이어야 합니다.")

    snapshot_id = f"pol_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            """
            INSERT INTO policy_snapshot(id, source_id, terms_version_or_hash, checked_at,
                valid_until, allowed_actions, reviewer_or_automation, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_id,
                terms_ref,
                to_db(now),
                to_db(now + timedelta(days=valid_days)),
                json.dumps(sorted(set(actions))),
                reviewer,
                notes,
            ),
        )
        conn.execute(
            "UPDATE cctv_source SET last_verified_at = ? WHERE id = ?", (to_db(now), source_id)
        )
    audit.record(
        conn,
        action="source.review",
        resource_type="cctv_source",
        resource_id=source_id,
        now=now,
        settings=settings,
        detail={"valid_days": valid_days},
    )
    return snapshot_id


def set_source_enabled(
    conn: sqlite3.Connection, source_id: str, enabled: bool, now: datetime, settings: Settings
) -> None:
    """소스 활성/비활성. 비활성화는 비상 중단·철회에 사용하며 즉시 모든 행위가 거부된다."""
    source = get_source(conn, source_id)
    if enabled and source["environment"] != "owned":
        problem = endpoint_problem(source["environment"], source["official_endpoint"], settings)
        if problem:
            raise PolicyDeniedError(f"활성화 거부 — {REASON_TEXT[problem]}", problem)
    with conn:
        conn.execute(
            "UPDATE cctv_source SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, to_db(now), source_id),
        )
    audit.record(
        conn,
        action="source.enable" if enabled else "source.disable",
        resource_type="cctv_source",
        resource_id=source_id,
        now=now,
        settings=settings,
    )


@dataclass(frozen=True)
class SourceSummary:
    id: str
    provider_name: str
    environment: str
    enabled: bool
    capabilities: tuple[str, ...]
    provider_retention_days: int | None
    camera_count: int
    snapshot_valid_until: datetime | None
    snapshot_actions: tuple[str, ...]


def list_sources(conn: sqlite3.Connection) -> list[SourceSummary]:
    summaries = []
    for row in conn.execute("SELECT * FROM cctv_source ORDER BY id"):
        snapshot = latest_snapshot(conn, row["id"])
        count = conn.execute(
            "SELECT COUNT(*) FROM camera WHERE source_id = ?", (row["id"],)
        ).fetchone()[0]
        summaries.append(
            SourceSummary(
                id=row["id"],
                provider_name=row["provider_name"],
                environment=row["environment"],
                enabled=bool(row["enabled"]),
                capabilities=tuple(json.loads(row["capabilities"])),
                provider_retention_days=row["provider_retention_days"],
                camera_count=count,
                snapshot_valid_until=from_db(snapshot["valid_until"]) if snapshot else None,
                snapshot_actions=tuple(json.loads(snapshot["allowed_actions"])) if snapshot else (),
            )
        )
    return summaries


# ---------------------------------------------------------------------------
# 카메라 카탈로그 어댑터
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockDeclaration:
    reference_time: datetime
    offset_seconds: float
    uncertainty_seconds: float
    drift_seconds_per_day: float
    note: str | None


@dataclass(frozen=True)
class CameraRecord:
    provider_camera_id: str
    name: str | None
    latitude: float
    longitude: float
    location_uncertainty_m: float
    coverage_radius_m: float | None
    road_name: str | None
    road_direction: str | None
    status: str
    observed_at: datetime
    clock: ClockDeclaration | None
    stream_url: str | None = None
    stream_delay_seconds: float | None = None


STREAM_SCHEMES = ("http", "https", "rtsp", "rtsps")


def validate_stream_url(url: str, where: str) -> str:
    """녹화에 쓸 스트림 주소 형식 검사(주소 자체는 로그에 남기지 않는다)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise CatalogError(f"{where}: stream_url 형식이 올바르지 않습니다.") from exc
    if parsed.scheme not in STREAM_SCHEMES or not parsed.hostname:
        raise CatalogError(f"{where}: stream_url 은 {', '.join(STREAM_SCHEMES)} 주소여야 합니다.")
    return url


class CatalogAdapter(Protocol):
    def fetch_cameras(self) -> list[CameraRecord]: ...


def _parse_clock(data: Any, where: str, settings: Settings) -> ClockDeclaration | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: clock 은 객체여야 합니다.")
    try:
        reference_time = parse_timestamp(_text(data, "reference_time", where, max_len=40))
    except TimeInputError as exc:
        raise CatalogError(f"{where}: clock.reference_time — {exc}") from exc
    return ClockDeclaration(
        reference_time=reference_time,
        offset_seconds=_number(
            data,
            "offset_seconds",
            where,
            required=True,
            minimum=-MAX_CLOCK_OFFSET_SECONDS,
            maximum=MAX_CLOCK_OFFSET_SECONDS,
        ),
        uncertainty_seconds=_number(
            data, "uncertainty_seconds", where, required=True, minimum=0.001, maximum=3600
        ),
        drift_seconds_per_day=_number(
            data,
            "drift_seconds_per_day",
            where,
            default=settings.default_clock_drift_seconds_per_day,
            minimum=0,
            maximum=600,
        ),
        note=_text(data, "note", where, max_len=500, required=False),
    )


def parse_camera(data: Any, index: int, settings: Settings) -> CameraRecord:
    where = f"카메라 #{index + 1}"
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: 객체여야 합니다.")
    provider_camera_id = _text(data, "provider_camera_id", where, max_len=64)
    where = f"카메라 {provider_camera_id}"
    if not PROVIDER_CAMERA_ID_RE.match(provider_camera_id):
        raise CatalogError(f"{where}: provider_camera_id 형식이 올바르지 않습니다.")
    lat = _number(data, "latitude", where, required=True)
    lon = _number(data, "longitude", where, required=True)
    try:
        validate_lat_lon(lat, lon)
    except CoordinateError as exc:
        raise CatalogError(f"{where}: {exc}") from exc
    status = data.get("status", "unknown")
    if status not in CAMERA_STATUSES:
        raise CatalogError(f"{where}: status 는 {sorted(CAMERA_STATUSES)} 중 하나여야 합니다.")
    try:
        observed_at = parse_timestamp(_text(data, "observed_at", where, max_len=40))
    except TimeInputError as exc:
        raise CatalogError(f"{where}: observed_at — {exc}") from exc
    return CameraRecord(
        provider_camera_id=provider_camera_id,
        name=_text(data, "name", where, max_len=200, required=False),
        latitude=lat,
        longitude=lon,
        location_uncertainty_m=_number(
            data, "location_uncertainty_m", where, default=10.0, minimum=0, maximum=5000
        ),
        coverage_radius_m=_number(data, "coverage_radius_m", where, minimum=1, maximum=2000),
        road_name=_text(data, "road_name", where, max_len=200, required=False),
        road_direction=_text(data, "road_direction", where, max_len=100, required=False),
        status=status,
        observed_at=observed_at,
        clock=_parse_clock(data.get("clock"), where, settings),
        stream_url=(
            validate_stream_url(url, where)
            if (url := _text(data, "stream_url", where, max_len=2000, required=False))
            else None
        ),
        stream_delay_seconds=_number(data, "stream_delay_seconds", where, minimum=0, maximum=120),
    )


class JsonFileCatalogAdapter:
    """로컬 JSON 카탈로그 파일(합성 데이터 또는 제공처가 공식 배포한 파일)."""

    def __init__(self, path: Path, source_id: str, settings: Settings) -> None:
        self.path = path
        self.source_id = source_id
        self.settings = settings

    def fetch_cameras(self) -> list[CameraRecord]:
        data = _load_json(self.path)
        if not isinstance(data, dict) or not isinstance(data.get("cameras"), list):
            raise CatalogError(f"{self.path.name}: 'cameras' 목록이 필요합니다.")
        declared = data.get("source_id")
        if declared is not None and declared != self.source_id:
            raise CatalogError(
                f"{self.path.name}: 파일의 source_id({declared})가 소스({self.source_id})와 다릅니다."
            )
        items = data["cameras"]
        if len(items) > MAX_CAMERAS_PER_SYNC:
            raise CatalogError(f"{self.path.name}: 카메라 수가 상한을 넘습니다.")
        records = [parse_camera(item, i, self.settings) for i, item in enumerate(items)]
        ids = [r.provider_camera_id for r in records]
        if len(set(ids)) != len(ids):
            raise CatalogError(f"{self.path.name}: provider_camera_id 가 중복되었습니다.")
        return records


def build_adapter(source: sqlite3.Row, settings: Settings) -> CatalogAdapter:
    config = json.loads(source["adapter"])
    if config.get("type") == "json_file":
        return JsonFileCatalogAdapter(Path(config["path"]), source["id"], settings)
    if config.get("type") == "its":
        raise CatalogError(
            "ITS 소스는 전체 동기화하지 않습니다. 'gps2cctv serve' 가 기기 주변만 자동으로 갱신합니다."
        )
    raise CatalogError(f"지원하지 않는 어댑터 유형입니다: {config.get('type')}")


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    upserted: int
    marked_missing: int
    clock_declarations: int


def camera_key(source_id: str, provider_camera_id: str) -> str:
    return f"{source_id}:{provider_camera_id}"


def upsert_camera(conn: sqlite3.Connection, source_id: str, rec: CameraRecord, stamp: str) -> str:
    """카메라 한 대를 넣거나 갱신한다(트랜잭션은 호출하는 쪽에서 관리)."""
    cam_id = camera_key(source_id, rec.provider_camera_id)
    conn.execute(
        """
        INSERT INTO camera(id, source_id, provider_camera_id, name, latitude, longitude,
            location_uncertainty_m, coverage_radius_m, road_name, road_direction, status,
            observed_at, synced_at, stream_url, stream_url_fetched_at, stream_delay_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name = excluded.name,
            latitude = excluded.latitude, longitude = excluded.longitude,
            location_uncertainty_m = excluded.location_uncertainty_m,
            coverage_radius_m = excluded.coverage_radius_m,
            road_name = excluded.road_name, road_direction = excluded.road_direction,
            status = excluded.status, observed_at = excluded.observed_at,
            synced_at = excluded.synced_at, stream_url = excluded.stream_url,
            stream_url_fetched_at = excluded.stream_url_fetched_at,
            stream_delay_seconds = excluded.stream_delay_seconds
        """,
        (
            cam_id,
            source_id,
            rec.provider_camera_id,
            rec.name,
            rec.latitude,
            rec.longitude,
            rec.location_uncertainty_m,
            rec.coverage_radius_m,
            rec.road_name,
            rec.road_direction,
            rec.status,
            to_db(rec.observed_at),
            stamp,
            rec.stream_url,
            stamp if rec.stream_url else None,
            rec.stream_delay_seconds,
        ),
    )
    return cam_id


def sync_cameras(
    conn: sqlite3.Connection,
    source_id: str,
    now: datetime,
    settings: Settings,
    adapter: CatalogAdapter | None = None,
) -> SyncResult:
    source = get_source(conn, source_id)
    decision = check_policy(conn, source_id, "catalog_sync", now, settings)
    if not decision.allowed:
        raise PolicyDeniedError(
            f"카탈로그 동기화 거부 — {decision.reason_text}", decision.reason_code
        )
    records = (adapter or build_adapter(source, settings)).fetch_cameras()
    stamp = to_db(now)
    seen: set[str] = set()
    clocks = 0
    with conn:
        for rec in records:
            cam_id = camera_key(source_id, rec.provider_camera_id)
            seen.add(cam_id)
            upsert_camera(conn, source_id, rec, stamp)
            # 제공처가 밝힌 시각 정보는 카탈로그의 최신 값으로 교체한다(사용자 관측값은 유지).
            conn.execute(
                "DELETE FROM camera_time_model WHERE camera_id = ? AND evidence = 'provider_declared'",
                (cam_id,),
            )
            if rec.clock is not None:
                clocks += 1
                conn.execute(
                    """
                    INSERT INTO camera_time_model(id, camera_id, reference_time,
                        estimated_offset_seconds, uncertainty_seconds, drift_seconds_per_day,
                        evidence, evidence_note, calculated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'provider_declared', ?, ?)
                    """,
                    (
                        f"clk_{uuid.uuid4().hex[:12]}",
                        cam_id,
                        to_db(rec.clock.reference_time),
                        rec.clock.offset_seconds,
                        rec.clock.uncertainty_seconds,
                        rec.clock.drift_seconds_per_day,
                        rec.clock.note,
                        stamp,
                    ),
                )
        missing = 0
        for row in conn.execute(
            "SELECT id FROM camera WHERE source_id = ? AND status != 'missing_from_catalog'",
            (source_id,),
        ).fetchall():
            if row["id"] not in seen:
                missing += 1
                conn.execute(
                    "UPDATE camera SET status = 'missing_from_catalog', synced_at = ? WHERE id = ?",
                    (stamp, row["id"]),
                )
    audit.record(
        conn,
        action="camera.sync",
        resource_type="cctv_source",
        resource_id=source_id,
        now=now,
        settings=settings,
        detail={"upserted": len(records), "marked_missing": missing, "clock_declarations": clocks},
    )
    return SyncResult(source_id, len(records), missing, clocks)


def get_camera(conn: sqlite3.Connection, camera_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM camera WHERE id = ?", (camera_id,)).fetchone()
    if row is None:
        raise NotFoundError(
            f"등록되지 않은 카메라입니다: {camera_id} (형식: <소스ID>:<제공처 카메라ID>)"
        )
    return row


def list_cameras(
    conn: sqlite3.Connection, source_id: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    query = """
        SELECT c.*, (SELECT COUNT(*) FROM camera_time_model m WHERE m.camera_id = c.id)
            AS clock_models
        FROM camera c
    """
    params: list[object] = []
    if source_id:
        query += " WHERE c.source_id = ?"
        params.append(source_id)
    query += " ORDER BY c.id LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def add_time_observation(
    conn: sqlite3.Connection,
    camera_id: str,
    *,
    reference_time: datetime,
    camera_time: datetime,
    uncertainty_seconds: float,
    drift_seconds_per_day: float | None,
    note: str | None,
    now: datetime,
    settings: Settings,
) -> tuple[str, float]:
    """사용자가 같은 순간의 표준 시각과 CCTV 화면 표시 시각을 비교해 기록한 시각 오차.

    오차 부호: offset = CCTV 표시 시각 - 표준 시각. 음수면 CCTV 시계가 느리다.
    """
    get_camera(conn, camera_id)
    offset = (camera_time - reference_time).total_seconds()
    if abs(offset) > MAX_CLOCK_OFFSET_SECONDS:
        raise CatalogError(
            f"시각 오차가 {offset:.0f}초로 너무 큽니다. 날짜나 시간대 입력을 확인하세요."
        )
    if not 0 < uncertainty_seconds <= 3600:
        raise CatalogError("관측 불확실성은 0초 초과 3600초 이하여야 합니다.")
    drift = (
        settings.default_clock_drift_seconds_per_day
        if drift_seconds_per_day is None
        else drift_seconds_per_day
    )
    if not 0 <= drift <= 600:
        raise CatalogError("시계 드리프트는 하루 0~600초 범위여야 합니다.")
    if reference_time > now + timedelta(minutes=5):
        raise CatalogError("관측 시각이 현재보다 미래입니다.")
    if note is not None and len(note) > 500:
        raise CatalogError("메모는 500자 이하로 입력하세요.")
    model_id = f"clk_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            """
            INSERT INTO camera_time_model(id, camera_id, reference_time, estimated_offset_seconds,
                uncertainty_seconds, drift_seconds_per_day, evidence, evidence_note, calculated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'manual_observation', ?, ?)
            """,
            (
                model_id,
                camera_id,
                to_db(reference_time),
                offset,
                uncertainty_seconds,
                drift,
                note,
                to_db(now),
            ),
        )
    audit.record(
        conn,
        action="camera.time_observe",
        resource_type="camera",
        resource_id=camera_id,
        now=now,
        settings=settings,
    )
    return model_id, offset
