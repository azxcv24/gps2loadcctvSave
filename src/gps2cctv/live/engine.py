"""실시간 자동 녹화 엔진.

GPS 위치가 들어올 때마다:
1. 위치를 저장하고 기기별 최근 경로(기본 15분)에 넣는다.
2. 급정거·SOS 알람이면 "사고 의심 이벤트"로 기록하고 주변 클립을 보호한다.
3. 주변 카메라 목록 갱신을 요청한다(ITS, 백그라운드).
4. 곧 지나갈 카메라(진행 방향 앞쪽, 도착 예상 ``arm_lookahead_seconds`` 이내)의 스트림을
   미리 녹화 대기시킨다. 공공 CCTV 스트림은 실제보다 늦게 나오므로 이 대기만으로 충분히 앞부분을 받는다.
5. 녹화 대기 중인 카메라마다 경로가 촬영 반경을 지나갔는지 계산하고(오프라인 후보 계산과 같은 방식),
   빠져나간 통과 구간은 ``[진입 - pre_seconds, 이탈 + post_seconds]`` 클립으로 예약한다.

1초마다 ``tick`` 이 예약된 클립 가운데 스트림에 해당 장면이 도착한 것을 잘라 저장하고,
더 필요 없는 녹화 대기를 끄고, 버퍼·보존 기간을 정리한다.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import incidents
from ..candidates import CameraInfo, TrackPoint, compute_passages, find_candidates
from ..config import Settings
from ..geo import angle_difference, bearing_deg, degree_margins, haversine_m
from ..policy import evaluate
from ..routeio import RoutePoint
from ..timeutil import from_db, seconds_between, shift, to_db, utc_now
from .clips import (
    CameraRef,
    ClipPlan,
    build_clip,
    mark_event,
    new_clip_id,
    purge_clips,
    save_clip_row,
)
from .config import LiveConfig
from .ingest import Fix
from .recorder import StreamRecorder

log = logging.getLogger(__name__)

RecorderFactory = Callable[..., StreamRecorder | None]
EVENT_KIND_TEXT = {"hard_brake": "급정거", "alarm": "알람(SOS 등)", "manual": "수동 표시"}


class Store:
    """여러 스레드가 함께 쓰는 SQLite 연결(``with store as conn:``)."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.lock = threading.RLock()

    def __enter__(self) -> Any:
        self.lock.acquire()
        return self.conn

    def __exit__(self, *exc: object) -> bool:
        self.lock.release()
        return False


@dataclass
class Watch:
    camera: CameraRef
    recorder: StreamRecorder
    armed_until: datetime
    started_at: datetime
    devices: set[str] = field(default_factory=set)
    fallback_tried: bool = False


@dataclass
class OpenPassage:
    """아직 촬영 반경 안에 있는 통과(트랙이 잘려도 처음 진입 시각을 기억)."""

    entry: datetime
    last_t: datetime
    estimated_passage_at: datetime
    closest_distance_m: float | None
    flags: set[str]
    camera: CameraRef
    recorder: StreamRecorder
    capped: bool = False


def camera_ref(row: Any) -> CameraRef:
    return CameraRef(
        id=row["id"],
        source_id=row["source_id"],
        provider_camera_id=row["provider_camera_id"],
        name=row["name"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        coverage_radius_m=row["coverage_radius_m"],
        location_uncertainty_m=row["location_uncertainty_m"],
        stream_url=row["stream_url"],
        stream_delay_seconds=row["stream_delay_seconds"],
        status=row["status"],
    )


class LiveEngine:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        config: LiveConfig,
        *,
        recorder_factory: RecorderFactory,
        catalog: Any = None,
        clock: Callable[[], datetime] = utc_now,
        ffmpeg: str | None = None,
        run_threads: bool = True,
    ) -> None:
        self.store = store
        self.settings = settings
        self.config = config
        self.rec = config.recording
        self.recorder_factory = recorder_factory
        self.catalog = catalog
        self.clock = clock
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.run_threads = run_threads
        self.clips_dir = config.data_dir / "clips"
        self.tracks: dict[str, list[TrackPoint]] = {}
        self.headings: dict[str, float] = {}
        self.watches: dict[str, Watch] = {}
        self.plans: list[ClipPlan] = []
        self.extracting: dict[str, ClipPlan] = {}
        self.done: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}
        self.capped: dict[tuple[str, str], datetime] = {}
        self.open: dict[tuple[str, str], OpenPassage] = {}
        self.last_seen: dict[str, datetime] = {}
        self.retired: list[StreamRecorder] = []
        self.retry_after: dict[str, datetime] = {}
        self.policy_cache: dict[str, tuple[datetime, bool]] = {}
        self.last_event: dict[str, datetime] = {}
        self.stats: Counter[str] = Counter()
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(2, thread_name_prefix="clip") if run_threads else None
        self._last_purge: datetime | None = None
        self._next_poll: dict[int, datetime] = {}
        # 시각 추정 오차의 상한(폴링 간격의 절반은 최대 2.5초)
        self.max_uncertainty = (
            max(self.rec.stream_delay_uncertainty_seconds, self.rec.pdt_uncertainty_seconds) + 3.0
        )

    # ------------------------------------------------------------------ 위치 수신
    def on_fix(self, fix: Fix) -> None:
        with self.lock:
            now = self.clock()
            self._store_position(fix)
            self.last_seen[fix.device_id] = now
            if fix.accuracy_m is not None and fix.accuracy_m > self.rec.max_fix_accuracy_m:
                return  # 기지국 위치 등 부정확한 위치로 카메라를 판단하지 않는다(저장만 함)
            track = self._add_to_track(fix)
            if fix.heading_deg is not None:
                self.headings[fix.device_id] = fix.heading_deg
            self._detect_events(fix, track, now)
            speed = self._speed(track)
            reach = max(self.rec.arm_min_radius_m, speed * self.rec.arm_lookahead_seconds)
            if self.catalog is not None:
                self.catalog.request_area(fix.latitude, fix.longitude, reach + 1000)
            if is_latest(track, fix):
                self._arm(fix, track, speed, reach, now)
            self._detect_passages(fix.device_id, now)

    def _store_position(self, fix: Fix) -> None:
        retention = fix.received_at + timedelta(days=self.rec.position_retention_days)
        with self.store as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO position(device_id, recorded_at, received_at, latitude,
                    longitude, accuracy_m, speed_mps, heading_deg, retention_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fix.device_id, to_db(fix.recorded_at), to_db(fix.received_at),
                    round(fix.latitude, 7), round(fix.longitude, 7), fix.accuracy_m,
                    fix.speed_mps, fix.heading_deg, to_db(retention),
                ),
            )  # fmt: skip

    def _add_to_track(self, fix: Fix) -> list[TrackPoint]:
        track = self.tracks.setdefault(fix.device_id, [])
        point = TrackPoint(
            fix.recorded_at, fix.latitude, fix.longitude, fix.accuracy_m, fix.speed_mps
        )
        if any(p.t == point.t for p in track[-20:]):
            return track
        track.append(point)
        if len(track) > 1 and track[-2].t > point.t:  # 늦게 도착한(오프라인 저장) 위치
            track.sort(key=lambda p: p.t)
        cutoff = shift(track[-1].t, -self.rec.track_window_seconds)
        while track and track[0].t < cutoff:
            track.pop(0)
        return track

    def _speed(self, track: list[TrackPoint]) -> float:
        last = track[-1]
        if last.speed_mps is not None:
            return last.speed_mps
        for prev in reversed(track[:-1]):
            dt = seconds_between(prev.t, last.t)
            if dt >= 2:
                return haversine_m(prev.lat, prev.lon, last.lat, last.lon) / dt
        return 0.0

    def _heading(self, device_id: str, track: list[TrackPoint]) -> float | None:
        last = track[-1]
        for prev in reversed(track[:-1]):
            if seconds_between(prev.t, last.t) > 30:
                break
            if haversine_m(prev.lat, prev.lon, last.lat, last.lon) >= 10:
                return bearing_deg(prev.lat, prev.lon, last.lat, last.lon)
        return self.headings.get(device_id)

    def _stationary(self, track: list[TrackPoint]) -> bool:
        last = track[-1]
        window = [p for p in track if seconds_between(p.t, last.t) <= self.rec.stationary_seconds]
        if seconds_between(window[0].t, last.t) < self.rec.stationary_seconds * 0.8:
            return False
        speeds = [p.speed_mps for p in window if p.speed_mps is not None]
        if len(speeds) >= 0.8 * len(window) and max(speeds) <= 1.0:
            return True  # 위치가 흔들려도 속도가 0 근처면 정차
        accuracies = sorted(
            p.accuracy_m if p.accuracy_m is not None else self.settings.default_gps_accuracy_m
            for p in window
        )
        radius = max(self.rec.stationary_radius_m, 2 * accuracies[len(accuracies) // 2])
        return all(haversine_m(p.lat, p.lon, last.lat, last.lon) <= radius for p in window)

    # ------------------------------------------------------------------ 녹화 대기
    def _capture_allowed(self, source_id: str, now: datetime) -> bool:
        cached = self.policy_cache.get(source_id)
        if cached and cached[0] > now:
            return cached[1]
        with self.store as conn:
            allowed = evaluate(conn, source_id, "media_capture", now, self.settings).allowed
        self.policy_cache[source_id] = (now + timedelta(seconds=60), allowed)
        return allowed

    def _nearby_cameras(self, lat: float, lon: float, radius_m: float) -> list[CameraRef]:
        dlat, dlon = degree_margins(lat, radius_m)
        with self.store as conn:
            rows = conn.execute(
                """
                SELECT * FROM camera
                WHERE stream_url IS NOT NULL AND status IN ('active', 'unknown')
                  AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
                """,
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
            ).fetchall()
        return [camera_ref(r) for r in rows]

    def _arm(
        self, fix: Fix, track: list[TrackPoint], speed: float, reach: float, now: datetime
    ) -> None:
        stationary = self._stationary(track)
        heading = self._heading(fix.device_id, track)
        accuracy = (
            fix.accuracy_m if fix.accuracy_m is not None else self.settings.default_gps_accuracy_m
        )
        choices: list[tuple[float, CameraRef]] = []
        for cam in self._nearby_cameras(fix.latitude, fix.longitude, reach + 1000):
            if self.retry_after.get(cam.id, now) > now:
                continue
            if not self._capture_allowed(cam.source_id, now):
                continue
            distance = haversine_m(fix.latitude, fix.longitude, cam.latitude, cam.longitude)
            coverage = cam.coverage_radius_m or self.settings.default_coverage_radius_m
            effective = coverage + accuracy + cam.location_uncertainty_m
            if distance <= effective + 30:
                eta = 0.0
                key = (fix.device_id, cam.id)
                op = self.open.get(key)
                if stationary and (key in self.capped or (op is not None and op.capped)):
                    continue  # 카메라 앞에 오래 서 있어 이미 최대 길이로 저장함
            elif stationary or distance > reach + effective:
                continue
            else:
                if heading is not None and speed >= 2.0:
                    direction = bearing_deg(
                        fix.latitude, fix.longitude, cam.latitude, cam.longitude
                    )
                    if angle_difference(direction, heading) > self.rec.arm_heading_tolerance_deg:
                        continue
                eta = (distance - effective) / max(speed, 1.0)
            choices.append((eta, cam))
        for _eta, cam in sorted(choices, key=lambda c: c[0]):
            self._ensure_watch(cam, fix.device_id, now)

    def _has_plans(self, camera_id: str) -> bool:
        """이 카메라의 녹화를 계속해야 하는가(예약된 클립, 아직 반경 안이고 최대 길이 전인 통과)."""
        return any(p.camera.id == camera_id for p in self.plans) or any(
            op.camera.id == camera_id and not op.capped for op in self.open.values()
        )

    def _referenced(self, recorder: StreamRecorder) -> bool:
        return (
            any(p.recorder is recorder for p in self.plans)
            or any(p.recorder is recorder for p in self.extracting.values())
            or any(op.recorder is recorder and not op.capped for op in self.open.values())
        )

    def _release(self, recorder: StreamRecorder) -> None:
        """녹화를 멈춘다. 아직 클립에 쓰일 버퍼 파일은 클립 저장이 끝날 때까지 남긴다."""
        keep = self._referenced(recorder)
        if keep:
            self.retired.append(recorder)
        if self.run_threads:
            threading.Thread(
                target=recorder.stop, kwargs={"remove_files": not keep}, daemon=True
            ).start()
        else:
            recorder.stop(remove_files=not keep)

    def _cleanup_retired(self) -> None:
        for recorder in list(self.retired):
            if not self._referenced(recorder) and not recorder.alive:
                self.retired.remove(recorder)
                recorder.remove_files()

    def _ensure_watch(self, cam: CameraRef, device_id: str, now: datetime) -> None:
        until = shift(now, self.rec.arm_lookahead_seconds + 30)
        watch = self.watches.get(cam.id)
        if watch is not None:
            watch.armed_until = max(watch.armed_until, until)
            watch.devices.add(device_id)
            return
        if len(self.watches) >= self.rec.max_concurrent_streams:
            idle = [w for w in self.watches.values() if not self._has_plans(w.camera.id)]
            if not idle:
                return
            victim = min(idle, key=lambda w: w.armed_until)
            if victim.armed_until >= until:
                return
            self._stop_watch(victim)
        recorder = self.recorder_factory(cam, now)
        if recorder is None:
            self.retry_after[cam.id] = shift(now, 300)
            return
        self.watches[cam.id] = Watch(cam, recorder, until, now, {device_id})
        self.stats["watches_started"] += 1
        if self.run_threads:
            recorder.start()
        log.info("녹화 대기 시작: %s", cam.name or cam.id)

    def _stop_watch(self, watch: Watch) -> None:
        self.watches.pop(watch.camera.id, None)
        self._release(watch.recorder)
        log.info("녹화 대기 종료: %s", watch.camera.name or watch.camera.id)

    # ------------------------------------------------------------------ 통과 감지
    def _detect_passages(self, device_id: str, now: datetime) -> None:
        track = self.tracks.get(device_id) or []
        if not track:
            return
        last_t = track[-1].t
        for watch in list(self.watches.values()):
            if device_id not in watch.devices:
                continue
            cam = watch.camera
            key = (device_id, cam.id)
            info = CameraInfo(
                cam.id, cam.source_id, cam.provider_camera_id, cam.latitude, cam.longitude,
                cam.location_uncertainty_m, cam.coverage_radius_m,
            )  # fmt: skip
            still_inside = False
            for passage in compute_passages(track, info, self.settings, None):
                flags = set(passage.flags) - {"route_starts_in_range", "route_ends_in_range"}
                op = self.open.get(key)
                if "route_ends_in_range" in passage.flags:
                    still_inside = True
                    if op is None:
                        op = OpenPassage(
                            passage.entry_at, last_t, passage.estimated_passage_at,
                            passage.closest_distance_m, flags, cam, watch.recorder,
                        )  # fmt: skip
                        self.open[key] = op
                    else:
                        op.entry = min(op.entry, passage.entry_at)
                        op.last_t, op.recorder = last_t, watch.recorder
                        op.flags |= flags
                        if passage.closest_distance_m is not None and (
                            op.closest_distance_m is None
                            or passage.closest_distance_m < op.closest_distance_m
                        ):
                            op.closest_distance_m = passage.closest_distance_m
                            op.estimated_passage_at = passage.estimated_passage_at
                    if not op.capped:  # 최대 길이를 채운 뒤에는 정차 중 녹화를 이어가지 않음
                        watch.armed_until = max(
                            watch.armed_until, shift(now, self.rec.arm_lookahead_seconds)
                        )
                    if (
                        not op.capped
                        and seconds_between(op.entry, last_t) >= self.rec.max_clip_seconds
                    ):
                        # 카메라 앞에 오래 머무름: 처음 max_clip 초를 저장하고 나머지는 떠날 때 저장
                        self._schedule(
                            device_id, cam, watch.recorder, op.entry,
                            shift(op.entry, self.rec.max_clip_seconds), op.estimated_passage_at,
                            op.closest_distance_m, op.flags, capped=True, now=now,
                        )  # fmt: skip
                        op.capped = True
                    continue
                entry = passage.entry_at
                if op is not None and passage.entry_at <= op.last_t and passage.exit_at >= op.entry:
                    entry = min(op.entry, entry)  # 반경 안에 있던 통과가 이제 끝남
                    flags |= op.flags
                    self.open.pop(key, None)
                if passage.exit_at < shift(watch.started_at, -self.rec.buffer_seconds):
                    continue
                self._schedule_remainder(
                    device_id, cam, watch.recorder, entry, passage.exit_at,
                    passage.estimated_passage_at, passage.closest_distance_m, flags, now,
                )  # fmt: skip
            op = self.open.get(key)
            if op is not None and not still_inside:
                # 늦게 온 위치로 경로가 바뀌어 반경 안 통과가 사라짐: 마지막으로 안에 있던 시각까지 저장
                self.open.pop(key, None)
                self._close_open(device_id, op, now)

    def _close_open(self, device_id: str, op: OpenPassage, now: datetime, extra=()) -> None:
        if op.capped and not op.recorder.alive:
            return  # 앞부분은 이미 저장했고, 정차 중에는 녹화를 멈췄으므로 더 저장할 장면이 없음
        self._schedule_remainder(
            device_id, op.camera, op.recorder, op.entry, op.last_t, op.estimated_passage_at,
            op.closest_distance_m, op.flags | set(extra), now,
        )  # fmt: skip

    def _schedule_remainder(
        self, device_id, cam, recorder, entry, exit_at, estimated, closest, flags, now
    ) -> None:
        """이미 저장(예약)한 구간을 빼고 남은 부분만 예약한다. 남은 부분이 길면 떠나는 장면만."""
        key = (device_id, cam.id)
        start = shift(entry, -self.rec.pre_seconds)
        end = shift(exit_at, self.rec.post_seconds)
        for plan in self.plans:
            if (
                (plan.device_id, plan.camera.id) == key
                and not plan.capped
                and plan.window_end >= start
                and plan.window_start <= end
            ):
                if end > plan.window_end:  # 늦게 들어온 위치로 구간이 늘어남
                    plan.window_end, plan.exit_at = end, exit_at
                    plan.due_at = shift(end, self._settle_seconds(cam))
                return
        covered = [b for a, b in self.done.get(key, []) if a <= end and b >= start]
        if covered:
            covered_end = max(covered)
            if covered_end >= shift(end, -0.5):
                return
            entry = max(entry, shift(covered_end, -self.rec.post_seconds))
            flags = set(flags) | {"continuation"}
        if seconds_between(entry, exit_at) > self.rec.max_clip_seconds:
            entry = shift(exit_at, -self.rec.max_clip_seconds)  # 떠나는 장면
            flags = set(flags) | {"departure"}
        self._schedule(
            device_id, cam, recorder, entry, exit_at, estimated, closest, flags,
            capped=False, now=now,
        )  # fmt: skip

    def _schedule(
        self, device_id, cam, recorder, entry, exit_at, estimated, closest, flags, *, capped, now
    ) -> None:
        key = (device_id, cam.id)
        start = shift(entry, -self.rec.pre_seconds)
        end = shift(exit_at, self.rec.post_seconds)
        plan = ClipPlan(
            id=new_clip_id(),
            device_id=device_id,
            camera=cam,
            entry_at=entry,
            exit_at=exit_at,
            estimated_passage_at=min(max(estimated, entry), exit_at),
            closest_distance_m=closest,
            window_start=start,
            window_end=end,
            due_at=shift(end, self._settle_seconds(cam)),
            flags=set(flags),
            capped=capped,
            recorder=recorder,
        )
        self.plans.append(plan)
        self.done.setdefault(key, []).append((start, end))
        if capped:
            self.capped[key] = now
        self.stats["clips_scheduled"] += 1
        log.info("통과 감지: %s (%s 저장 예약)", cam.name or cam.id, device_id)

    def _settle_seconds(self, cam: CameraRef) -> float:
        delay = cam.stream_delay_seconds
        delay = self.rec.stream_delay_seconds if delay is None else delay
        # 스트림 지연 + 시각 오차 여유 + 세그먼트가 재생목록에 올라오기까지의 여유.
        # 실제로는 tick 에서 해당 장면이 버퍼에 들어왔는지 확인한 뒤 자른다.
        return delay + self.max_uncertainty + self.rec.segment_settle_seconds

    # ------------------------------------------------------------------ 사고 의심 이벤트
    def _detect_events(self, fix: Fix, track: list[TrackPoint], now: datetime) -> None:
        if fix.alarm:
            self.record_event(fix.device_id, "alarm", fix.recorded_at, fix.latitude,
                              fix.longitude, detail=fix.alarm[:40], now=now)  # fmt: skip
            return
        if fix.speed_mps is None:
            return
        window = self.rec.hard_brake_window_seconds
        prior = [
            p.speed_mps
            for p in track
            if p.speed_mps is not None and 0 < seconds_between(p.t, fix.recorded_at) <= window
        ]
        if not prior:
            return
        drop = max(prior) - fix.speed_mps
        if drop >= self.rec.hard_brake_drop_mps and fix.speed_mps <= self.rec.hard_brake_stop_mps:
            detail = f"{max(prior) * 3.6:.0f}->{fix.speed_mps * 3.6:.0f} km/h"
            self.record_event(fix.device_id, "hard_brake", fix.recorded_at, fix.latitude,
                              fix.longitude, detail=detail, now=now)  # fmt: skip

    def record_event(
        self,
        device_id: str,
        kind: str,
        at: datetime,
        lat: float | None,
        lon: float | None,
        *,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        now = now or self.clock()
        last = self.last_event.get(device_id)
        if last is not None and abs(seconds_between(last, at)) < 120:
            return None
        self.last_event[device_id] = at
        with self.store as conn:
            event_id, _protected = mark_event(
                conn, device_id=device_id, kind=kind, at=at, latitude=lat, longitude=lon,
                detail=detail, protect_before_seconds=self.rec.event_protect_before_seconds,
                protect_after_seconds=self.rec.event_protect_after_seconds, now=now,
                settings=self.settings,
            )  # fmt: skip
        self.stats["events"] += 1
        log.warning(
            "사고 의심 이벤트(%s): %s — 앞뒤 클립을 보호합니다.", EVENT_KIND_TEXT[kind], device_id
        )
        return event_id

    def _process_events(self, now: datetime) -> None:
        if not self.rec.auto_incident:
            return
        with self.store as conn:
            rows = conn.execute(
                "SELECT * FROM live_event WHERE processed = 0 AND protect_until <= ?",
                (to_db(shift(now, -30)),),
            ).fetchall()
            for row in rows:
                incident_id = None
                try:
                    incident_id = self._auto_incident(conn, row, now)
                except Exception:
                    log.exception("자동 사건 생성 실패")
                with conn:
                    conn.execute(
                        "UPDATE live_event SET processed = 1, incident_id = ? WHERE id = ?",
                        (incident_id, row["id"]),
                    )

    def _auto_incident(self, conn: Any, event: Any, now: datetime) -> str:
        at = from_db(event["occurred_at"])
        incident = incidents.create_incident(
            conn,
            occurred_at=at,
            reason=f"자동 감지: {EVENT_KIND_TEXT[event['kind']]} ({event['detail'] or '-'}), "
            f"기기 {event['device_id']}",
            now=now,
            settings=self.settings,
            time_uncertainty_seconds=10,
            before_seconds=self.rec.event_protect_before_seconds,
            after_seconds=self.rec.event_protect_after_seconds,
            latitude=event["latitude"],
            longitude=event["longitude"],
            location_uncertainty_m=30,
        )
        rows = conn.execute(
            """
            SELECT recorded_at, latitude, longitude, accuracy_m, speed_mps FROM position
            WHERE device_id = ? AND recorded_at BETWEEN ? AND ? ORDER BY recorded_at
            """,
            (event["device_id"], to_db(incident.started_at), to_db(incident.ended_at)),
        ).fetchall()
        points = [
            RoutePoint(
                from_db(r["recorded_at"]),
                r["latitude"],
                r["longitude"],
                r["accuracy_m"],
                r["speed_mps"],
            )
            for r in rows
        ]
        if points:
            incidents.import_route(conn, incident.id, points, now, self.settings)
        find_candidates(conn, incident.id, now, self.settings)
        log.warning("사고 의심 이벤트로 사건을 만들었습니다: %s", incident.id)
        return incident.id

    # ------------------------------------------------------------------ 주기 작업
    def tick(self, now: datetime | None = None) -> None:
        with self.lock:
            now = now or self.clock()
            self._close_silent(now)
            self._extract_due(now)
            self._check_recorders(now)
            for watch in list(self.watches.values()):
                if watch.armed_until < now and not self._has_plans(watch.camera.id):
                    self._stop_watch(watch)
            self._prune_buffers(now)
            self._cleanup_retired()
            self._prune_memory(now)
        self._process_events(now)
        if self._last_purge is None or now - self._last_purge > timedelta(hours=1):
            self._last_purge = now
            self.purge(now)

    def _close_silent(self, now: datetime) -> None:
        """반경 안에서 위치가 끊긴 기기(앱 종료·통신 두절)는 마지막 위치까지의 구간을 저장한다."""
        for key, op in list(self.open.items()):
            seen = self.last_seen.get(key[0])
            if seen is not None and seconds_between(seen, now) > self.rec.gps_silence_seconds:
                self.open.pop(key, None)
                self._close_open(key[0], op, now, extra=("gps_lost",))

    def purge(self, now: datetime) -> None:
        with self.store as conn:
            purge_clips(
                conn, now, self.settings,
                max_storage_bytes=int(self.rec.max_storage_gb * 1024**3),
            )  # fmt: skip

    def _extract_due(self, now: datetime, *, force: bool = False) -> None:
        for plan in [p for p in self.plans if force or p.due_at <= now]:
            recorder = plan.recorder
            if recorder is not None and recorder.alive and not force:
                latest = recorder.latest_end()
                needed = shift(plan.window_end, self.max_uncertainty)
                if (latest is None or latest < needed) and plan.attempts < 30:
                    plan.attempts += 1
                    plan.due_at = shift(now, 2)
                    continue
            self.plans.remove(plan)
            self.extracting[plan.id] = plan
            if self.executor is not None:
                self.executor.submit(self._extract, plan, recorder)
            else:
                self._extract(plan, recorder)

    def _extract(self, plan: ClipPlan, recorder: StreamRecorder | None) -> None:
        try:
            result = build_clip(
                plan,
                recorder,
                clips_dir=self.clips_dir,
                include_time_uncertainty=self.rec.include_time_uncertainty,
                viewing_copy=self.rec.viewing_copy,
                ffmpeg=self.ffmpeg,
                display_timezone=self.settings.display_timezone,
                max_uncertainty_seconds=self.max_uncertainty,
            )
            with self.store as conn:
                save_clip_row(
                    conn, plan, result, now=self.clock(),
                    retention_days=self.rec.clip_retention_days, settings=self.settings,
                )  # fmt: skip
            self.stats[f"clips_{result.status}"] += 1
            level = logging.INFO if result.status != "failed" else logging.WARNING
            log.log(level, "클립 %s: %s (%s)", result.status, plan.camera.name or plan.camera.id,
                    result.failure_reason or result.original_path)  # fmt: skip
        except Exception:
            self.stats["clips_error"] += 1
            log.exception("클립 저장 중 오류")
        finally:
            with self.lock:
                self.extracting.pop(plan.id, None)

    def _check_recorders(self, now: datetime) -> None:
        for watch in list(self.watches.values()):
            status = watch.recorder.status
            if status == "unsupported" and not watch.fallback_tried and self.ffmpeg:
                fallback = self.recorder_factory(watch.camera, now, fallback=True)
                watch.fallback_tried = True
                if fallback is not None:
                    old = watch.recorder
                    watch.recorder = fallback
                    if self.run_threads:
                        fallback.start()
                    self._release(old)  # 이전 버퍼는 예약된 클립에 쓰이면 남겨 둠
                    log.info("ffmpeg 녹화기로 전환: %s", watch.camera.name or watch.camera.id)
                    continue
            if status in ("error", "unsupported", "stopped") and not any(
                p.camera.id == watch.camera.id for p in self.plans
            ):
                self.retry_after[watch.camera.id] = shift(now, 120)
                self._stop_watch(watch)

    def _prune_buffers(self, now: datetime) -> None:
        keep_after = shift(now, -self.rec.buffer_seconds)
        margin = self.max_uncertainty + 5
        active = [*self.plans, *self.extracting.values()]
        for watch in self.watches.values():
            pinned = [
                (shift(p.window_start, -margin), shift(p.window_end, margin))
                for p in active
                if p.recorder is watch.recorder
            ] + [
                (shift(op.entry, -self.rec.pre_seconds - margin), shift(now, margin))
                for op in self.open.values()
                if op.recorder is watch.recorder and not op.capped
            ]
            watch.recorder.prune(keep_after, pinned)

    def _prune_memory(self, now: datetime) -> None:
        horizon = shift(now, -2 * self.rec.track_window_seconds)
        for key in list(self.done):
            self.done[key] = [(a, b) for a, b in self.done[key] if b >= horizon]
            if not self.done[key]:
                del self.done[key]
        for key, when in list(self.capped.items()):
            if when < horizon and key not in self.open:
                del self.capped[key]
        for camera_id, until in list(self.retry_after.items()):
            if until < now:
                del self.retry_after[camera_id]
        live = {id(w.recorder) for w in self.watches.values()}
        for key in list(self._next_poll):
            if key not in live:
                del self._next_poll[key]
        for device_id, when in list(self.last_event.items()):
            if when < horizon:
                del self.last_event[device_id]

    def step_recorders(self) -> None:
        """스레드 없이 녹화기를 한 번씩 돌린다(시뮬레이션 테스트·동기 실행용)."""
        from .recorder import HlsRecorder

        with self.lock:
            watches = list(self.watches.values())
        for watch in watches:
            recorder = watch.recorder
            if isinstance(recorder, HlsRecorder) and recorder.alive:
                now = self.clock()
                due = self._next_poll.get(id(recorder))
                if due is None or now >= due:
                    self._next_poll[id(recorder)] = shift(now, recorder.step())

    # ------------------------------------------------------------------ 상태·종료
    def status(self) -> dict[str, int]:
        with self.lock:
            return {
                "devices": len(self.tracks),
                "watching": len(self.watches),
                "pending_clips": len(self.plans) + len(self.extracting),
                **{k: v for k, v in self.stats.items()},
            }

    def shutdown(self, now: datetime | None = None) -> None:
        """대기 중인 클립을 지금까지 받은 범위로 저장하고 녹화를 멈춘다."""
        with self.lock:
            now = now or self.clock()
            for key, op in list(self.open.items()):
                self.open.pop(key, None)
                self._close_open(key[0], op, now)
            self._extract_due(now, force=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        with self.lock:
            for watch in list(self.watches.values()):
                self.watches.pop(watch.camera.id, None)
                watch.recorder.stop()
            for recorder in self.retired:
                recorder.stop()
            self.retired.clear()


def is_latest(track: list[TrackPoint], fix: Fix) -> bool:
    return bool(track) and track[-1].t == fix.recorded_at
