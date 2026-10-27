"""자동 녹화 엔진 시뮬레이션: 가짜 시계 + 가짜 실시간 HLS 스트림으로 카메라 앞을 지나간다."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps2cctv import db
from gps2cctv.config import Settings
from gps2cctv.geo import from_local_xy
from gps2cctv.live.config import (
    CameraFileConfig,
    DeviceConfig,
    LiveConfig,
    RecordingConfig,
    ServerConfig,
)
from gps2cctv.live.engine import LiveEngine, Store
from gps2cctv.live.ingest import KNOT_TO_MPS, Fix
from gps2cctv.live.recorder import HlsRecorder, StreamTiming
from gps2cctv.live.serve import setup_sources
from gps2cctv.timeutil import from_db

LAT0, LON0 = 37.0, 127.0
T0 = datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)
DEVICE = "my-phone-0123456789"


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeLiveHls:
    """세그먼트 k 는 [T0-60s + k*d, +d] 장면. 장면이 끝나고 delay 초 뒤 재생목록에 올라온다."""

    def __init__(self, clock: Clock, *, seg: float = 2.0, delay: float = 3.0, pdt: bool = True):
        self.clock, self.seg, self.delay, self.pdt = clock, seg, delay, pdt
        self.base = T0 - timedelta(seconds=60)
        self.requests = 0

    def scene(self, k: int) -> tuple[datetime, datetime]:
        start = self.base + timedelta(seconds=k * self.seg)
        return start, start + timedelta(seconds=self.seg)

    def fetch(self, url: str, timeout: float, max_bytes: int) -> bytes:
        self.requests += 1
        if url.endswith(".m3u8"):
            visible = math.floor(
                ((self.clock() - self.base).total_seconds() - self.delay) / self.seg
            )
            first = max(0, visible - 6)
            lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{int(self.seg)}",
                     f"#EXT-X-MEDIA-SEQUENCE:{first}"]  # fmt: skip
            for k in range(first, visible):
                if self.pdt:
                    lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{self.scene(k)[0].isoformat()}")
                lines += [f"#EXTINF:{self.seg:.3f},", f"seg{k}.ts"]
            return ("\n".join(lines) + "\n").encode()
        k = int(url.rsplit("seg", 1)[1].split(".")[0])
        return f"[SEG{k}]".encode()


def write_cameras(path: Path, cameras: list[tuple[str, float, float]]) -> None:
    items = []
    for cam_id, x, y in cameras:
        lat, lon = from_local_xy(x, y, LAT0, LON0)
        items.append(
            {
                "provider_camera_id": cam_id, "name": f"테스트 {cam_id}", "latitude": lat,
                "longitude": lon, "location_uncertainty_m": 10, "coverage_radius_m": 100,
                "status": "active", "observed_at": "2026-10-27T00:00:00Z",
                "stream_url": f"https://stream.example/{cam_id}/live.m3u8",
            }
        )  # fmt: skip
    path.write_text(json.dumps({"source_id": "my-cams", "cameras": items}), encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    def build(cameras=(("CAM1", 0.0, 20.0),), pdt=True, **recording):
        clock = Clock(T0)
        stream = FakeLiveHls(clock, pdt=pdt)
        cams = tmp_path / "cams.json"
        write_cameras(cams, list(cameras))
        rec = replace(RecordingConfig(), viewing_copy="none", **recording)
        live = LiveConfig(
            server=ServerConfig(data_dir=str(tmp_path / "data")),
            devices=(DeviceConfig(DEVICE),),
            camera_files=(CameraFileConfig(path=str(cams), source_id="my-cams"),),
            recording=rec,
            base_dir=tmp_path,
        )
        settings = Settings()
        store = Store(db.connect(tmp_path / "t.sqlite3"))
        setup_sources(store, settings, live, clock(), "test.toml")
        made = []

        def factory(cam, now, *, fallback=False):
            timing = StreamTiming(rec.stream_delay_seconds, rec.stream_delay_uncertainty_seconds,
                                  rec.pdt_uncertainty_seconds)  # fmt: skip
            recorder = HlsRecorder(
                cam.id, lambda force: cam.stream_url,
                tmp_path / "buf" / f"{cam.provider_camera_id}-{len(made)}", timing,
                fetch=stream.fetch, clock=clock,
            )  # fmt: skip
            made.append(recorder)
            return recorder

        engine = LiveEngine(store, settings, live, recorder_factory=factory, clock=clock,
                            ffmpeg="", run_threads=False)  # fmt: skip
        engine.ffmpeg = None
        return clock, stream, engine, store, made

    return build


def drive(engine, clock, points, *, step=1.0, accuracy=5.0, device=DEVICE):
    """(x, y, speed) 목록을 1초 간격으로 보낸다. 각 단계마다 녹화기·tick 을 돌린다."""
    for x, y, speed in points:
        lat, lon = from_local_xy(x, y, LAT0, LON0)
        engine.on_fix(
            Fix(device, clock(), clock(), lat, lon, accuracy, speed, 90.0 if speed else None)
        )
        engine.step_recorders()
        engine.tick(clock())
        clock.advance(step)


def idle(engine, clock, seconds):
    for _ in range(int(seconds)):
        engine.step_recorders()
        engine.tick(clock())
        clock.advance(1.0)


def clips(store):
    with store as conn:
        return conn.execute("SELECT * FROM clip ORDER BY window_start").fetchall()


def straight(x0=-600.0, x1=600.0, speed=15.0, y=0.0):
    n = int((x1 - x0) / speed)
    return [(x0 + speed * k, y, speed) for k in range(n + 1)]


class TestDrivePast:
    def test_clip_covers_passage_plus_two_seconds(self, world):
        clock, stream, engine, store, _made = world()
        drive(engine, clock, straight())
        idle(engine, clock, 30)
        (clip,) = clips(store)
        assert clip["status"] == "saved"
        assert clip["time_mapping"] == "program_date_time"
        # 유효 반경 = 촬영 100 + GPS 5 + 카메라 위치 10 = 115m, 최근접 20m
        half = math.sqrt(115**2 - 20**2) / 15.0
        passage = T0 + timedelta(seconds=40)  # x=-600 에서 15 m/s → 40초 뒤 원점
        entry, exit_at = from_db(clip["entry_at"]), from_db(clip["exit_at"])
        assert abs((entry - (passage - timedelta(seconds=half))).total_seconds()) < 0.5
        assert abs((exit_at - (passage + timedelta(seconds=half))).total_seconds()) < 0.5
        assert from_db(clip["window_start"]) == entry - timedelta(seconds=2)
        assert from_db(clip["window_end"]) == exit_at + timedelta(seconds=2)
        # 저장된 원본은 [진입-2-오차, 이탈+2+오차] 에 걸치는 세그먼트를 그대로 이어 붙인 것
        unc = clip["time_uncertainty_seconds"]
        assert unc == 1.0
        want_start = entry - timedelta(seconds=2 + unc)
        want_end = exit_at + timedelta(seconds=2 + unc)
        expected = [
            k for k in range(200)
            if stream.scene(k)[1] > want_start and stream.scene(k)[0] < want_end
        ]  # fmt: skip
        data = Path(clip["original_path"]).read_bytes()
        assert data == b"".join(f"[SEG{k}]".encode() for k in expected)
        assert from_db(clip["covered_start"]) <= want_start
        assert from_db(clip["covered_end"]) >= want_end
        import hashlib

        assert clip["original_sha256"] == hashlib.sha256(data).hexdigest()
        meta = json.loads(Path(clip["metadata_path"]).read_text(encoding="utf-8"))
        assert meta["device_id"] == DEVICE and meta["status"] == "saved"
        assert len(meta["segments"]) == len(expected)

    def test_receive_time_mapping_widens_with_uncertainty(self, world):
        clock, stream, engine, store, _ = world(pdt=False)
        drive(engine, clock, straight())
        idle(engine, clock, 40)
        (clip,) = clips(store)
        assert clip["status"] == "saved"
        assert clip["time_mapping"] == "receive_time"
        assert clip["time_uncertainty_seconds"] > 4
        # 추정 오차를 넉넉히 더해도 실제 통과 장면(세그먼트)은 반드시 포함
        passage = T0 + timedelta(seconds=40)
        data = Path(clip["original_path"]).read_bytes()
        k = int((passage - stream.base).total_seconds() // stream.seg)
        assert f"[SEG{k}]".encode() in data

    def test_camera_behind_is_not_armed(self, world):
        clock, _stream, engine, store, made = world(cameras=(("BEHIND", -900.0, 0.0),))
        drive(engine, clock, straight(x0=-500, x1=200))
        idle(engine, clock, 20)
        assert made == [] and clips(store) == []

    def test_far_camera_never_clipped(self, world):
        clock, _s, engine, store, _made = world(cameras=(("SIDE", 0.0, 400.0),))
        drive(engine, clock, straight())
        idle(engine, clock, 20)
        assert clips(store) == []

    def test_watch_stops_after_leaving(self, world):
        clock, _s, engine, _store, made = world()
        drive(engine, clock, straight(x1=900))
        idle(engine, clock, 120)
        assert engine.watches == {}
        assert made[0].status == "stopped"

    def test_two_devices_get_separate_clips(self, world):
        clock, _s, engine, store, _ = world()
        other = "my-car-tracker-98765"
        engine.config = replace(
            engine.config, devices=(*engine.config.devices, DeviceConfig(other))
        )
        for x, y, speed in straight():
            for dev, offset in ((DEVICE, 0.0), (other, -60.0)):
                lat, lon = from_local_xy(x + offset, y, LAT0, LON0)
                engine.on_fix(Fix(dev, clock(), clock(), lat, lon, 5.0, speed, 90.0))
            engine.step_recorders()
            engine.tick(clock())
            clock.advance(1)
        idle(engine, clock, 30)
        assert sorted(c["device_id"] for c in clips(store)) == sorted([DEVICE, other])


class TestParkedAndEvents:
    def test_parked_near_camera_is_capped_once(self, world):
        clock, _s, engine, store, _ = world(max_clip_seconds=30)
        approach = straight(x0=-300, x1=0)
        drive(engine, clock, approach + [(0.0, 0.0, 0.0)] * 200)
        idle(engine, clock, 30)
        rows = clips(store)
        assert len(rows) == 1
        assert "capped" in json.loads(rows[0]["flags"])
        span = from_db(rows[0]["exit_at"]) - from_db(rows[0]["entry_at"])
        assert span == timedelta(seconds=30)
        assert engine.watches == {}  # 계속 서 있어도 다시 녹화하지 않음

    def test_hard_brake_protects_clips_and_creates_incident(self, world):
        clock, _s, engine, store, _ = world()
        # 카메라를 지나 200m 더 간 뒤 2초 만에 15 m/s → 0
        path = straight(x1=210) + [(225.0, 0.0, 7.0), (230.0, 0.0, 0.0)] + [(230.0, 0.0, 0.0)] * 5
        drive(engine, clock, path)
        idle(engine, clock, 40)
        (clip,) = clips(store)
        assert clip["protected"] == 1
        with store as conn:
            event = conn.execute("SELECT * FROM live_event").fetchone()
        assert event["kind"] == "hard_brake" and event["incident_id"] is None
        # 보호 구간(사고 후 5분)이 지나면 사건을 자동으로 만들고 경로를 넣는다
        idle(engine, clock, 340)
        with store as conn:
            event = conn.execute("SELECT * FROM live_event").fetchone()
            incident = conn.execute("SELECT * FROM incident").fetchone()
            points = conn.execute("SELECT COUNT(*) FROM route_point").fetchone()[0]
        assert event["processed"] == 1 and event["incident_id"] == incident["id"]
        assert "급정거" in incident["reason"] and points > 0

    def test_sos_alarm_is_event(self, world):
        clock, _s, engine, store, _ = world()
        lat, lon = from_local_xy(-600, 0, LAT0, LON0)
        engine.on_fix(Fix(DEVICE, clock(), clock(), lat, lon, 5.0, 10.0, 90.0, alarm="sos"))
        with store as conn:
            assert conn.execute("SELECT kind FROM live_event").fetchone()[0] == "alarm"


class TestPolicyAndFailures:
    def test_disabled_source_is_not_recorded(self, world):
        from gps2cctv import catalog

        clock, _s, engine, store, made = world()
        with store as conn:
            catalog.set_source_enabled(conn, "my-cams", False, clock(), engine.settings)
        drive(engine, clock, straight())
        idle(engine, clock, 20)
        assert made == [] and clips(store) == []

    def test_stream_down_records_failed_clip(self, world):
        clock, stream, engine, store, _ = world()

        def broken(url, timeout, max_bytes):
            from gps2cctv.live.recorder import FetchError

            raise FetchError("HTTP 503", 503)

        stream.fetch = broken  # 녹화기가 만들어지기 전에 바꿈
        drive(engine, clock, straight())
        idle(engine, clock, 90)
        rows = clips(store)
        assert len(rows) == 1 and rows[0]["status"] == "failed"
        assert rows[0]["failure_reason"] in ("no_footage", "not_recording")


def test_migration_from_v1_keeps_data(tmp_path):
    """v0.1(스키마 1) DB를 열면 자료를 잃지 않고 v2로 바뀐다."""
    import sqlite3

    from gps2cctv import catalog
    from tests.conftest import FIXTURES, NOW

    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    catalog.register_sources(conn, FIXTURES / "sources.json", NOW, Settings())
    catalog.review_source(conn, "synthetic-city", actions=["catalog_sync"], terms_ref="v1",
                          valid_days=30, reviewer="t", notes=None, now=NOW, settings=Settings())  # fmt: skip
    catalog.sync_cameras(conn, "synthetic-city", NOW, Settings())
    conn.close()
    # v1 모양으로 되돌리기: CHECK 제약이 옛것인 표 + 스트림 열 없음 + 버전 1
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'cctv_source'").fetchone()[0]
    raw.executescript(
        "CREATE TABLE s_old AS SELECT * FROM cctv_source;"
        "DROP TABLE cctv_source;"
        + sql.replace("'synthetic', 'production', 'owned'", "'synthetic', 'production'")
        + ";INSERT INTO cctv_source SELECT * FROM s_old; DROP TABLE s_old;"
        "ALTER TABLE camera DROP COLUMN stream_url;"
        "ALTER TABLE camera DROP COLUMN stream_url_fetched_at;"
        "ALTER TABLE camera DROP COLUMN stream_delay_seconds;"
        "UPDATE schema_meta SET value = '1';"
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("SELECT value FROM schema_meta").fetchone()[0] == "2"
    assert conn.execute("SELECT COUNT(*) FROM camera").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM policy_snapshot").fetchone()[0] == 1
    conn.execute("SELECT stream_url FROM camera LIMIT 1").fetchone()
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'cctv_source'").fetchone()[0]
    assert "'owned'" in sql
    _ = KNOT_TO_MPS
