"""SQLite 저장소.

- 파일은 소유자만 읽고 쓸 수 있게(0600) 만든다.
- ``secure_delete`` 로 삭제된 행이 파일의 빈 페이지에 남지 않게 덮어쓴다.
- 저널은 DELETE 모드로 두어 트랜잭션이 끝나면 임시 저널 파일이 사라지게 한다.
- 아직 저장 시 암호화는 하지 않는다(M3 예정). README에 같은 내용을 명시한다.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cctv_source (
    id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('synthetic', 'production')),
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
"""


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
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"데이터베이스 스키마 버전({row['value']})이 프로그램({SCHEMA_VERSION})과 다릅니다."
        )
    return conn


def connect(path: Path) -> sqlite3.Connection:
    """데이터베이스 파일을 열고(없으면 0600 권한으로 생성) 스키마를 준비한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    return _prepare(conn)


def connect_memory() -> sqlite3.Connection:
    """테스트용 메모리 데이터베이스."""
    return _prepare(sqlite3.connect(":memory:"))
