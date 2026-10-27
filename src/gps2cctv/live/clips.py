"""통과 구간 클립 만들기·저장·정리.

클립마다 두 파일을 만든다.
- 원본(``*.orig.ts`` 또는 ``*.orig.mp4``): 스트림에서 받은 세그먼트 바이트를 **그대로** 이어 붙인 것.
  가공하지 않았으므로 증거용 원본으로 쓰고 SHA-256 해시를 남긴다. 세그먼트 단위라 앞뒤가 조금 더 길다.
- 보기용(``*.mp4``): 원본에서 요청 구간(통과 구간 + 앞뒤 여유)만 정확히 잘라 낸 사본(ffmpeg 필요).
그리고 계산 근거를 담은 ``*.json`` 을 함께 남긴다.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import __version__, audit
from ..config import Settings
from ..timeutil import from_db, get_zone, seconds_between, shift, to_db
from .recorder import BufferedSegment, StreamRecorder

log = logging.getLogger(__name__)

CLIP_NOTE = (
    "GPS 기록으로 계산한 통과 시각 기준으로 자동 저장한 공공 CCTV 스트림 구간입니다. "
    "영상 속 사람·차량을 식별한 결과가 아니며, 법적 증거 효력을 보증하지 않습니다."
)
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class CameraRef:
    id: str
    source_id: str
    provider_camera_id: str
    name: str | None
    latitude: float
    longitude: float
    coverage_radius_m: float | None
    location_uncertainty_m: float
    stream_url: str | None
    stream_delay_seconds: float | None
    status: str = "active"


@dataclass
class ClipPlan:
    id: str
    device_id: str
    camera: CameraRef
    entry_at: datetime
    exit_at: datetime
    estimated_passage_at: datetime
    closest_distance_m: float | None
    window_start: datetime  # 통과 구간 + 앞뒤 여유(시각 오차 여유 제외)
    window_end: datetime
    due_at: datetime
    flags: set[str] = field(default_factory=set)
    capped: bool = False
    attempts: int = 0


@dataclass(frozen=True)
class ClipResult:
    id: str
    status: str
    failure_reason: str | None
    window_start: datetime
    window_end: datetime
    covered_start: datetime | None
    covered_end: datetime | None
    time_mapping: str
    time_uncertainty_seconds: float
    original_path: Path | None
    original_sha256: str | None
    original_bytes: int | None
    viewing_path: Path | None
    viewing_sha256: str | None
    metadata_path: Path | None


def new_clip_id() -> str:
    return f"clip_{uuid.uuid4().hex[:12]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _ffmpeg_viewing_copy(
    ffmpeg: str, source: Path, target: Path, mode: str, offset: float, duration: float
) -> bool:
    if mode == "trim_reencode":
        args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(offset, 0):.3f}", "-i", str(source), "-t", f"{max(duration, 0.5):.3f}",
            "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(target),
        ]  # fmt: skip
    else:
        args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(target),
        ]  # fmt: skip
    try:
        result = subprocess.run(args, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("보기용 사본 생성 실패: %s", exc)
        return False
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        log.warning("보기용 사본 생성 실패: %s", result.stderr.decode(errors="replace")[-300:])
        target.unlink(missing_ok=True)
        return False
    os.chmod(target, 0o600)
    return True


def _gaps(segments: list[BufferedSegment], tolerance: float = 0.75) -> bool:
    return any(
        seconds_between(a.real_end, b.real_start) > tolerance
        for a, b in itertools.pairwise(segments)
    )


def build_clip(
    plan: ClipPlan,
    recorder: StreamRecorder | None,
    *,
    clips_dir: Path,
    include_time_uncertainty: bool,
    viewing_copy: str,
    ffmpeg: str | None,
    display_timezone: str,
    max_uncertainty_seconds: float,
) -> ClipResult:
    """버퍼에서 통과 구간 세그먼트를 골라 클립 파일을 만든다."""

    def failed(reason: str, mapping: str = "none", unc: float = 0.0) -> ClipResult:
        return ClipResult(
            plan.id, "failed", reason, plan.window_start, plan.window_end, None, None, mapping,
            unc, None, None, None, None, None, None,
        )  # fmt: skip

    if recorder is None:
        return failed("not_recording")
    # 1차: 가능한 최대 시각 오차까지 넓혀 세그먼트를 찾고, 실제 쓰인 시각 추정 방식의 오차로 확정
    probe = recorder.segments_overlapping(
        shift(plan.window_start, -max_uncertainty_seconds),
        shift(plan.window_end, max_uncertainty_seconds),
    )
    if not probe:
        return failed("no_footage")
    unc = max(s.uncertainty_seconds for s in probe) if include_time_uncertainty else 0.0
    mappings = sorted({s.mapping for s in probe})
    mapping = mappings[0] if len(mappings) == 1 else "mixed"
    want_start = shift(plan.window_start, -unc)
    want_end = shift(plan.window_end, unc)
    segments = recorder.segments_overlapping(want_start, want_end)
    if not segments:
        return failed("no_footage", mapping, unc)
    covered_start, covered_end = segments[0].real_start, segments[-1].real_end
    partial = (
        seconds_between(want_start, covered_start) > 0.5
        or seconds_between(covered_end, want_end) > 0.5
        or _gaps(segments)
    )

    local = plan.estimated_passage_at.astimezone(get_zone(display_timezone))
    safe_device = _SAFE_RE.sub("_", plan.device_id)[:40]
    safe_camera = _SAFE_RE.sub("_", plan.camera.provider_camera_id)[:40]
    folder = clips_dir / safe_device / local.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{local.strftime('%H%M%S')}_{safe_camera}_{plan.id[-6:]}"
    ext = "mp4" if recorder.container == "mp4" else "ts"
    original = folder / f"{stem}.orig.{ext}"
    with original.open("wb") as out:
        os.chmod(original, 0o600)
        if recorder.init_data:
            out.write(recorder.init_data)
        for seg in segments:
            with seg.path.open("rb") as handle:
                shutil.copyfileobj(handle, out)
    original_sha = sha256_file(original)
    original_bytes = original.stat().st_size

    viewing = None
    viewing_sha = None
    if viewing_copy != "none" and ffmpeg:
        target = folder / f"{stem}.mp4"
        clip_start = max(want_start, covered_start)
        clip_end = min(want_end, covered_end)
        if _ffmpeg_viewing_copy(
            ffmpeg,
            original,
            target,
            viewing_copy,
            seconds_between(covered_start, clip_start),
            seconds_between(clip_start, clip_end),
        ):
            viewing, viewing_sha = target, sha256_file(target)

    metadata = {
        "clip_id": plan.id,
        "note": CLIP_NOTE,
        "generator": f"gps2cctv {__version__}",
        "device_id": plan.device_id,
        "camera": {
            "id": plan.camera.id,
            "source_id": plan.camera.source_id,
            "provider_camera_id": plan.camera.provider_camera_id,
            "name": plan.camera.name,
            "latitude": plan.camera.latitude,
            "longitude": plan.camera.longitude,
        },
        "passage": {
            "entry_at": to_db(plan.entry_at),
            "exit_at": to_db(plan.exit_at),
            "estimated_passage_at": to_db(plan.estimated_passage_at),
            "closest_distance_m": (
                round(plan.closest_distance_m, 1) if plan.closest_distance_m is not None else None
            ),
            "flags": sorted(plan.flags),
            "capped": plan.capped,
        },
        "window": {
            "padded_start": to_db(plan.window_start),
            "padded_end": to_db(plan.window_end),
            "with_time_uncertainty_start": to_db(want_start),
            "with_time_uncertainty_end": to_db(want_end),
            "covered_start": to_db(covered_start),
            "covered_end": to_db(covered_end),
            "time_mapping": mapping,
            "time_uncertainty_seconds": round(unc, 3),
        },
        "segments": [
            {
                "sequence": s.sequence,
                "real_start": to_db(s.real_start),
                "real_end": to_db(s.real_end),
                "mapping": s.mapping,
                "bytes": s.size,
            }
            for s in segments
        ],
        "files": {
            "original": original.name,
            "original_sha256": original_sha,
            "viewing": viewing.name if viewing else None,
            "viewing_sha256": viewing_sha,
        },
        "status": "partial" if partial else "saved",
    }
    meta_path = folder / f"{stem}.json"
    _private_write(meta_path, json.dumps(metadata, ensure_ascii=False, indent=2).encode())
    return ClipResult(
        plan.id, "partial" if partial else "saved", "incomplete_coverage" if partial else None,
        plan.window_start, plan.window_end, covered_start, covered_end, mapping, unc,
        original, original_sha, original_bytes, viewing, viewing_sha, meta_path,
    )  # fmt: skip


def is_protected(conn: sqlite3.Connection, device_id: str, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM live_event
        WHERE device_id = ? AND protect_from <= ? AND protect_until >= ? LIMIT 1
        """,
        (device_id, to_db(end), to_db(start)),
    ).fetchone()
    return row is not None


def save_clip_row(
    conn: sqlite3.Connection,
    plan: ClipPlan,
    result: ClipResult,
    *,
    now: datetime,
    retention_days: int,
    settings: Settings,
) -> None:
    protected = is_protected(conn, plan.device_id, plan.window_start, plan.window_end)
    with conn:
        conn.execute(
            """
            INSERT INTO clip(id, device_id, camera_id, source_id, provider_camera_id, camera_name,
                entry_at, exit_at, estimated_passage_at, closest_distance_m, window_start,
                window_end, covered_start, covered_end, time_mapping, time_uncertainty_seconds,
                status, failure_reason, original_path, original_sha256, original_bytes,
                viewing_path, viewing_sha256, metadata_path, flags, protected, retention_until,
                created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?)
            """,
            (
                plan.id, plan.device_id, plan.camera.id, plan.camera.source_id,
                plan.camera.provider_camera_id, plan.camera.name, to_db(plan.entry_at),
                to_db(plan.exit_at), to_db(plan.estimated_passage_at), plan.closest_distance_m,
                to_db(result.window_start), to_db(result.window_end),
                to_db(result.covered_start) if result.covered_start else None,
                to_db(result.covered_end) if result.covered_end else None,
                result.time_mapping, result.time_uncertainty_seconds, result.status,
                result.failure_reason,
                str(result.original_path) if result.original_path else None,
                result.original_sha256, result.original_bytes,
                str(result.viewing_path) if result.viewing_path else None,
                result.viewing_sha256,
                str(result.metadata_path) if result.metadata_path else None,
                json.dumps(sorted(plan.flags | ({"capped"} if plan.capped else set()))),
                1 if protected else 0, to_db(now + timedelta(days=retention_days)), to_db(now),
            ),
        )  # fmt: skip
    audit.record(
        conn,
        action="clip.save",
        resource_type="clip",
        resource_id=plan.id,
        now=now,
        settings=settings,
        outcome="success" if result.status != "failed" else "error",
        reason_code=result.failure_reason or result.status,
        detail={"protected": protected},
    )


def protect_window(conn: sqlite3.Connection, device_id: str, start: datetime, end: datetime) -> int:
    with conn:
        cursor = conn.execute(
            """
            UPDATE clip SET protected = 1
            WHERE device_id = ? AND window_end >= ? AND window_start <= ? AND protected = 0
            """,
            (device_id, to_db(start), to_db(end)),
        )
    return cursor.rowcount


def set_protected(
    conn: sqlite3.Connection, clip_id: str, protected: bool, now: datetime, settings: Settings
) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE clip SET protected = ? WHERE id = ?", (1 if protected else 0, clip_id)
        )
    if cursor.rowcount:
        audit.record(
            conn,
            action="clip.protect" if protected else "clip.unprotect",
            resource_type="clip",
            resource_id=clip_id,
            now=now,
            settings=settings,
        )
    return bool(cursor.rowcount)


def _delete_files(row: sqlite3.Row) -> None:
    for key in ("original_path", "viewing_path", "metadata_path"):
        if row[key]:
            Path(row[key]).unlink(missing_ok=True)


@dataclass(frozen=True)
class ClipPurgeReport:
    expired_clips: int
    quota_clips: int
    positions: int
    freed_bytes: int


def purge_clips(
    conn: sqlite3.Connection,
    now: datetime,
    settings: Settings,
    *,
    max_storage_bytes: int | None = None,
    dry_run: bool = False,
) -> ClipPurgeReport:
    """보존 기간이 지난(보호되지 않은) 클립과 위치 기록을 지우고, 용량 상한을 넘으면 오래된 것부터 지운다."""
    stamp = to_db(now)
    expired = conn.execute(
        "SELECT * FROM clip WHERE protected = 0 AND retention_until <= ? ORDER BY created_at",
        (stamp,),
    ).fetchall()
    positions = conn.execute(
        "SELECT COUNT(*) FROM position WHERE retention_until <= ?", (stamp,)
    ).fetchone()[0]
    to_delete = list(expired)
    quota = 0
    if max_storage_bytes is not None:
        total = conn.execute(
            "SELECT COALESCE(SUM(original_bytes), 0) FROM clip WHERE status != 'failed'"
        ).fetchone()[0]
        total -= sum(r["original_bytes"] or 0 for r in expired)
        if total > max_storage_bytes:
            expired_ids = {r["id"] for r in expired}
            for row in conn.execute(
                "SELECT * FROM clip WHERE protected = 0 ORDER BY created_at"
            ).fetchall():
                if total <= max_storage_bytes:
                    break
                if row["id"] in expired_ids:
                    continue
                to_delete.append(row)
                total -= row["original_bytes"] or 0
                quota += 1
    freed = sum(
        sum(Path(r[k]).stat().st_size for k in ("original_path", "viewing_path")
            if r[k] and Path(r[k]).exists())
        for r in to_delete
    )  # fmt: skip
    if dry_run:
        return ClipPurgeReport(len(expired), quota, positions, freed)
    for row in to_delete:
        _delete_files(row)
    with conn:
        conn.executemany("DELETE FROM clip WHERE id = ?", [(r["id"],) for r in to_delete])
        conn.execute("DELETE FROM position WHERE retention_until <= ?", (stamp,))
    if to_delete or positions:
        audit.record(
            conn,
            action="clip.purge",
            resource_type="clip",
            now=now,
            settings=settings,
            detail={"expired": len(expired), "quota": quota, "positions": positions},
        )
    return ClipPurgeReport(len(expired), quota, positions, freed)


def list_clips(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    limit: int = 50,
    protected_only: bool = False,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM clip WHERE 1 = 1"
    params: list[object] = []
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    if protected_only:
        query += " AND protected = 1"
    query += " ORDER BY estimated_passage_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def clip_times(row: sqlite3.Row) -> tuple[datetime, datetime]:
    return from_db(row["window_start"]), from_db(row["window_end"])


EVENT_KINDS = ("hard_brake", "alarm", "manual")


def mark_event(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    kind: str,
    at: datetime,
    latitude: float | None,
    longitude: float | None,
    detail: str | None,
    protect_before_seconds: float,
    protect_after_seconds: float,
    now: datetime,
    settings: Settings,
) -> tuple[str, int]:
    """사고 의심 이벤트를 기록하고 앞뒤 구간의 클립을 보호한다(나중에 저장될 클립도 보호됨)."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"알 수 없는 이벤트 종류: {kind}")
    start = shift(at, -protect_before_seconds)
    end = shift(at, protect_after_seconds)
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            """
            INSERT INTO live_event(id, device_id, kind, occurred_at, detail, latitude, longitude,
                protect_from, protect_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, device_id, kind, to_db(at), detail, latitude, longitude, to_db(start),
             to_db(end), to_db(now)),
        )  # fmt: skip
    protected = protect_window(conn, device_id, start, end)
    audit.record(
        conn, action="live.event", resource_type="live_event", resource_id=event_id,
        reason_code=kind, now=now, settings=settings, detail={"protected_clips": protected},
    )  # fmt: skip
    return event_id, protected
