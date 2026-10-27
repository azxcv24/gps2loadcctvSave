"""SQLite 저장소.

- 파일은 소유자만 읽고 쓸 수 있게(0600) 만든다.
- ``secure_delete`` 로 삭제된 행이 파일의 빈 페이지에 남지 않게 덮어쓴다.
- 저널은 DELETE 모드로 두어 트랜잭션이 끝나면 임시 저널 파일이 사라지게 한다.
- 아직 저장 시 암호화는 하지 않는다. README에 같은 내용을 명시한다.
- 스키마 v2: 실시간 자동 녹화(위치·클립·이벤트, 카메라 스트림 주소)를 추가했다.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cctv_source (
    id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('synthetic', 'production', 'owned')),
    capabilities TEXT NOT NULL,
    official_endpoint TEXT NOT NULL,
    terms_url TEXT NOT NULL,
    preservation_procedure TEXT NOT NULL,
    provider_retention_days INTEGER,
    adapter TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_verified_at TEXT,
    -- 엔드포인트·약관·기능이 바뀐 시각. 이 시각 이전의 정책 검토 기록은 무효.
    material_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_snapshot (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES cctv_source(id) ON DELETE CASCADE,
    terms_version_or_hash TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    allowed_actions TEXT NOT NULL,
    reviewer_or_automation TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS policy_snapshot_source ON policy_snapshot(source_id, checked_at);

CREATE TABLE IF NOT EXISTS camera (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES cctv_source(id) ON DELETE CASCADE,
    provider_camera_id TEXT NOT NULL,
    name TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    location_uncertainty_m REAL NOT NULL,
    coverage_radius_m REAL,
    road_name TEXT,
    road_direction TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'inactive', 'unknown', 'missing_from_catalog')),
    observed_at TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    stream_url TEXT,
    stream_url_fetched_at TEXT,
    stream_delay_seconds REAL,
    UNIQUE (source_id, provider_camera_id)
);
CREATE INDEX IF NOT EXISTS camera_lat_lon ON camera(latitude, longitude);

CREATE TABLE IF NOT EXISTS camera_time_model (
    id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES camera(id) ON DELETE CASCADE,
    reference_time TEXT NOT NULL,
    estimated_offset_seconds REAL NOT NULL,
    uncertainty_seconds REAL NOT NULL,
    drift_seconds_per_day REAL NOT NULL,
    evidence TEXT NOT NULL CHECK (evidence IN ('provider_declared', 'manual_observation')),
    evidence_note TEXT,
    calculated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS camera_time_model_camera ON camera_time_model(camera_id);

CREATE TABLE IF NOT EXISTS incident (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    occurred_at_uncertainty_seconds REAL NOT NULL,
    window_before_seconds REAL NOT NULL,
    window_after_seconds REAL NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    latitude_rounded REAL,
    longitude_rounded REAL,
    location_uncertainty_m REAL,
    location_precision_decimals INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('draft', 'preservation_requested', 'reviewing', 'closed')),
    retention_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incident_retention ON incident(retention_until);

CREATE TABLE IF NOT EXISTS route_point (
    id INTEGER PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    latitude_rounded REAL NOT NULL,
    longitude_rounded REAL NOT NULL,
    precision_decimals INTEGER NOT NULL,
    accuracy_m REAL,
    speed_mps REAL,
    UNIQUE (incident_id, recorded_at)
);

CREATE TABLE IF NOT EXISTS camera_candidate (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    camera_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    provider_camera_id TEXT NOT NULL,
    pass_index INTEGER NOT NULL,
    estimated_passage_at TEXT NOT NULL,
    closest_distance_m REAL,
    review_window_start TEXT NOT NULL,
    review_window_end TEXT NOT NULL,
    camera_clock_offset_seconds REAL NOT NULL,
    uncertainty_seconds REAL NOT NULL,
    camera_clock_window_start TEXT NOT NULL,
    camera_clock_window_end TEXT NOT NULL,
    request_window_start TEXT NOT NULL,
    request_window_end TEXT NOT NULL,
    calculation_method TEXT NOT NULL,
    calculation_details TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    needs_manual_review INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    provider_deletion_estimate TEXT,
    policy_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS camera_candidate_incident ON camera_candidate(incident_id);

CREATE TABLE IF NOT EXISTS preservation_request (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    candidate_id TEXT REFERENCES camera_candidate(id) ON DELETE SET NULL,
    camera_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    provider_camera_id TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    estimated_passage_at TEXT NOT NULL,
    camera_clock_offset_seconds REAL NOT NULL,
    uncertainty_seconds REAL NOT NULL,
    provider_deletion_estimate TEXT,
    -- 요청을 준비할 때의 계산 근거(후보를 다시 계산해도 남도록 복사)
    calculation_snapshot TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'submitted', 'acknowledged', 'fulfilled', 'rejected', 'expired', 'withdrawn'
    )),
    provider_request_id TEXT,
    submitted_at TEXT,
    responded_at TEXT,
    response_reference TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preservation_request_incident ON preservation_request(incident_id);

CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'error')),
    reason_code TEXT,
    detail TEXT,
    retention_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_event_retention ON audit_event(retention_until);

-- 실시간 자동 녹화 ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS position (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    accuracy_m REAL,
    speed_mps REAL,
    heading_deg REAL,
    retention_until TEXT NOT NULL,
    UNIQUE (device_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS position_device_time ON position(device_id, recorded_at);
CREATE INDEX IF NOT EXISTS position_retention ON position(retention_until);

CREATE TABLE IF NOT EXISTS clip (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    provider_camera_id TEXT NOT NULL,
    camera_name TEXT,
    entry_at TEXT NOT NULL,
    exit_at TEXT NOT NULL,
    estimated_passage_at TEXT NOT NULL,
    closest_distance_m REAL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    covered_start TEXT,
    covered_end TEXT,
    time_mapping TEXT NOT NULL,
    time_uncertainty_seconds REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved', 'partial', 'failed')),
    failure_reason TEXT,
    original_path TEXT,
    original_sha256 TEXT,
    original_bytes INTEGER,
    viewing_path TEXT,
    viewing_sha256 TEXT,
    metadata_path TEXT,
    flags TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0,
    retention_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clip_device_time ON clip(device_id, window_start);
CREATE INDEX IF NOT EXISTS clip_retention ON clip(retention_until);

CREATE TABLE IF NOT EXISTS live_event (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('hard_brake', 'alarm', 'manual')),
    occurred_at TEXT NOT NULL,
    detail TEXT,
    latitude REAL,
    longitude REAL,
    protect_from TEXT NOT NULL,
    protect_until TEXT NOT NULL,
    incident_id TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS live_event_device_time ON live_event(device_id, occurred_at);
"""

_SOURCE_COLUMNS = (
    "id, provider_name, environment, capabilities, official_endpoint, terms_url, "
    "preservation_procedure, provider_retention_days, adapter, enabled, last_verified_at, "
    "material_changed_at, created_at, updated_at"
)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """cctv_source 에 'owned'(본인 소유 장비) 환경을 허용하고 camera 에 스트림 열을 추가한다.

    SQLite는 CHECK 제약을 바꿀 수 없어 표를 새로 만들어 옮긴다. 이때 외래 키를 끄지 않으면
    DROP TABLE 이 카메라·정책 기록을 CASCADE로 지우므로 반드시 끈 상태에서 진행한다.
    """
    source_sql = SCHEMA.split("CREATE TABLE IF NOT EXISTS cctv_source (", 1)[1].split(");", 1)[0]
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(f"CREATE TABLE cctv_source__v2 ({source_sql})")
        conn.execute(
            f"INSERT INTO cctv_source__v2 ({_SOURCE_COLUMNS}) "
            f"SELECT {_SOURCE_COLUMNS} FROM cctv_source"
        )
        conn.execute("DROP TABLE cctv_source")
        conn.execute("ALTER TABLE cctv_source__v2 RENAME TO cctv_source")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(camera)")}
        for name, kind in (
            ("stream_url", "TEXT"),
            ("stream_url_fetched_at", "TEXT"),
            ("stream_delay_seconds", "REAL"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE camera ADD COLUMN {name} {kind}")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"마이그레이션 중 외래 키 오류: {problems[:3]}")
        conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


_MIGRATIONS = {1: _migrate_v1_to_v2}


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        with conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
    else:
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"데이터베이스 스키마 버전({version})이 프로그램({SCHEMA_VERSION})보다 새롭습니다."
            )
        while version < SCHEMA_VERSION:
            _MIGRATIONS[version](conn)
            version += 1
    return conn


def connect(path: Path) -> sqlite3.Connection:
    """데이터베이스 파일을 열고(없으면 0600 권한으로 생성) 스키마를 준비한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        os.chmod(path, 0o600)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode = DELETE")
    return _prepare(conn)


def connect_memory() -> sqlite3.Connection:
    """테스트용 메모리 데이터베이스."""
    return _prepare(sqlite3.connect(":memory:", check_same_thread=False))
