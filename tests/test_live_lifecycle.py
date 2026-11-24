"""통과 → 클립 예약 과정의 경계 상황(재검토에서 찾은 문제의 회귀 테스트)."""

from __future__ import annotations

import itertools
import json
from datetime import timedelta
from pathlib import Path

from gps2cctv.geo import from_local_xy
from gps2cctv.live.clips import ClipPlan, build_clip
from gps2cctv.live.engine import subtract_intervals
from gps2cctv.live.ingest import Fix
from gps2cctv.live.recorder import BufferedSegment, FfmpegRecorder, HlsRecorder, StreamTiming
from gps2cctv.timeutil import from_db

from .test_live_engine import (  # noqa: F401 — world 는 pytest fixture
    DEVICE,
    LAT0,
    LON0,
    T0,
    Clock,
    clips,
    drive,
    idle,
    straight,
    world,
)


def fix_at(engine, clock, x, y, speed, accuracy=5.0):
    lat, lon = from_local_xy(x, y, LAT0, LON0)
    engine.on_fix(Fix(DEVICE, clock(), clock(), lat, lon, accuracy, speed, 90.0 if speed else None))


def windows(store):
    return [(from_db(r["window_start"]), from_db(r["window_end"])) for r in clips(store)]


def assert_no_overlap(store):
    for (_a1, b1), (a2, _b2) in itertools.pairwise(sorted(windows(store))):
        assert b1 <= a2, "같은 장면이 두 클립에 저장됨"


def test_subtract_intervals():
    t = [T0 + timedelta(seconds=s) for s in range(10)]
    assert subtract_intervals(t[0], t[9], [[t[2], t[4]], [t[6], t[7]]]) == [
        (t[0], t[2]), (t[4], t[6]), (t[7], t[9]),
    ]  # fmt: skip
    assert subtract_intervals(t[2], t[4], [[t[0], t[9]]]) == []


class TestPassageEdges:
    def test_only_inaccurate_fixes_inside_radius_closes_passage(self, world):
        clock, _s, engine, store, made = world()
        drive(engine, clock, straight(x0=-600, x1=0))
        for _ in range(300):  # 지하주차장: 2km 정확도 위치만 들어옴
            fix_at(engine, clock, 0, 0, 0, accuracy=2000.0)
            engine.step_recorders()
            engine.tick(clock())
            clock.advance(1)
        (clip,) = clips(store)
        assert clip["status"] == "saved" and "gps_lost" in json.loads(clip["flags"])
        assert engine.open == {} and engine.watches == {}
        assert not made[0].alive

    def test_edge_jitter_saves_once(self, world):
        clock, _s, engine, store, _ = world()
        pts = straight(x0=-600, x1=0) + [(15.0 * k, 0.0, 15.0) for k in range(1, 7)]
        pts += [(112.0, 0, 5.0), (125.0, 0, 5.0), (113.0, 0, 3.0), (126.0, 0, 5.0)]
        drive(engine, clock, pts + [(126.0 + 15 * k, 0, 15.0) for k in range(1, 30)])
        idle(engine, clock, 60)
        assert len(clips(store)) == 1

    def test_gps_lost_then_long_stay_no_double_save(self, world):
        clock, _s, engine, store, _ = world()
        drive(engine, clock, straight(x0=-600, x1=-60))
        idle(engine, clock, 40)
        slow = [(-60 + 0.5 * k, 0.0, 0.5) for k in range(260)]
        drive(engine, clock, slow + straight(x0=80, x1=400))
        idle(engine, clock, 60)
        rows = clips(store)
        assert len(rows) >= 2 and all(r["status"] == "saved" for r in rows)
        assert_no_overlap(store)

    def test_sparse_fixes_while_parked(self, world):
        clock, _s, engine, store, _ = world()
        drive(engine, clock, straight(x0=-300, x1=0))
        for _ in range(15):
            idle(engine, clock, 120)
            fix_at(engine, clock, 0, 0, 0)
        idle(engine, clock, 60)
        rows = clips(store)
        assert rows and all(r["status"] != "failed" for r in rows)
        assert_no_overlap(store)

    def test_capped_passage_dropped_after_leaving_far(self, world):
        clock, _s, engine, _store, _ = world()
        drive(engine, clock, straight(x0=-300, x1=0) + [(0.0, 0.0, 0.0)] * 400)
        drive(engine, clock, [(150.0 * k, 0.0, 25.0) for k in range(1, 40)], step=6.0)
        assert engine.open == {}


class TestRecorderFailures:
    def test_switch_mid_passage_keeps_entry_footage(self, world):
        clock, _s, engine, store, made = world()
        drive(
            engine,
            clock,
            straight(x0=-600, x1=-120) + [(-120 + 4.0 * k, 0, 4.0) for k in range(25)],
        )
        assert engine.open
        old_dir = made[0].buffer_dir
        made[0].status = "unsupported"
        engine.ffmpeg = "ffmpeg"  # 가짜 팩토리는 또 다른 HlsRecorder 를 돌려준다
        engine.tick(clock())
        drive(
            engine,
            clock,
            [(-20 + 4.0 * k, 0, 4.0) for k in range(1, 40)] + straight(x0=160, x1=400),
        )
        idle(engine, clock, 60)
        rows = clips(store)
        first = rows[0]
        want = from_db(first["window_start"]) - timedelta(seconds=first["time_uncertainty_seconds"])
        assert from_db(first["covered_start"]) <= want  # 진입 장면이 남아 있음
        assert "recorder_switch" in json.loads(first["flags"])
        assert_no_overlap(store)
        assert not old_dir.exists()

    def test_error_mid_passage_saves_what_was_recorded(self, world):
        clock, _s, engine, store, made = world()
        drive(engine, clock, straight(x0=-600, x1=-90))
        made[0].status = "error"
        engine.tick(clock())
        assert engine.open == {} and engine.watches == {}
        idle(engine, clock, 30)
        (clip,) = clips(store)
        assert clip["status"] in ("saved", "partial")
        assert "stream_lost" in json.loads(clip["flags"])

    def test_stopped_recorder_never_looks_alive(self, tmp_path):
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2.0,\ns1.ts\n"
        rec = HlsRecorder("c", lambda f: "https://x/l.m3u8", tmp_path, StreamTiming(3, 4, 1),
                          fetch=lambda u, t, m: text.encode(), clock=Clock(T0))  # fmt: skip
        rec._stop.set()  # stop() 의 join 이 시간 초과된 뒤 폴링이 끝나는 상황
        rec.poll_once()
        assert not rec.alive and rec.status != "recording"

    def test_source_disabled_stops_recording_within_seconds(self, world):
        from gps2cctv import catalog

        clock, _s, engine, store, made = world()
        drive(engine, clock, straight(x0=-600, x1=150))  # 통과 직후(클립 예약됨)
        assert engine.plans
        with store as conn:
            catalog.set_source_enabled(conn, "my-cams", False, clock(), engine.settings)
        idle(engine, clock, 10)
        assert engine.watches == {} and engine.plans == []
        assert not made[0].alive
        idle(engine, clock, 60)
        assert clips(store) == []


class TestStreamTiming:
    def test_high_latency_stream_still_saves_right_scene(self, world):
        clock, stream, engine, store, made = world()
        stream.delay = 130.0  # 촬영 뒤 130초 지나서야 재생목록에 나오는 스트림
        drive(engine, clock, straight())
        idle(engine, clock, 200)
        (clip,) = clips(store)
        assert clip["status"] == "saved" and clip["time_mapping"] == "program_date_time"
        k = int((T0 + timedelta(seconds=40) - stream.base).total_seconds() // stream.seg)
        assert f"[SEG{k}]".encode() in Path(clip["original_path"]).read_bytes()
        assert not made[0].pdt_rejected

    def test_original_concatenated_in_arrival_order(self, tmp_path):
        class Rec:
            container = "ts"
            init_data = None

            def __init__(self, segs):
                self.segs = segs

            def segments_overlapping(self, a, b):
                return [s for s in self.segs if s.real_end > a and s.real_start < b]

        segs = []
        # 도착 순서(파일 이름)는 1,2,3 인데 시각 추정은 2가 1보다 앞선 상황
        for name, start in (("00000001_1.ts", 2.0), ("00000002_2.ts", 0.0), ("00000003_3.ts", 4.0)):
            path = tmp_path / name
            path.write_bytes(name[:8].encode())
            st = T0 + timedelta(seconds=start)
            segs.append(BufferedSegment(int(name[9]), path, 2, st, st + timedelta(seconds=2),
                                        "receive_time", 1.0, 8))  # fmt: skip
        from gps2cctv.live.clips import CameraRef

        from .test_live_engine import write_cameras  # noqa: F401

        cam = CameraRef("s:c", "s", "c", None, 37.0, 127.0, 100, 10, None, None)
        plan = ClipPlan("clip_x", "dev", cam, T0, T0 + timedelta(seconds=6), T0, None,
                        T0, T0 + timedelta(seconds=6), T0)  # fmt: skip
        result = build_clip(plan, Rec(segs), clips_dir=tmp_path / "clips",
                            include_time_uncertainty=False, viewing_copy="none", ffmpeg=None,
                            display_timezone="Asia/Seoul", max_uncertainty_seconds=1)  # fmt: skip
        assert result.original_path.read_bytes() == b"000000010000000200000003"

    def test_ffmpeg_partial_csv_line_and_forward_jump(self, tmp_path):
        clock = Clock(T0)
        d = tmp_path / "ff"
        rec = FfmpegRecorder("c", None, d, StreamTiming(0.0, 1.0, 1.0), input_args=["-i", "x"],
                             clock=clock)  # fmt: skip
        for i in range(5):
            (d / f"seg_{i:08d}.ts").write_bytes(b"x")
        rec.list_path.write_text("seg_00000000.ts,0.000000,2.000000\nseg_00000001.ts,2.000000,4")
        rec._scan()
        assert len(rec.segments) == 1  # 다 쓰이지 않은 줄은 아직 읽지 않음
        lines = ["seg_00000000.ts,0.000000,2.000000", "seg_00000001.ts,2.000000,4.000000",
                 "seg_00000002.ts,4.000000,6.000000"]  # fmt: skip
        rec.list_path.write_text("\n".join(lines) + "\n")
        clock.advance(2)
        rec._scan()
        before = [(s.sequence, s.real_start) for s in rec.segments]
        assert all(s.duration > 0 for s in rec.segments)
        lines.append("seg_00000003.ts,3606.000000,3608.000000")  # 스트림 시각이 1시간 건너뜀
        rec.list_path.write_text("\n".join(lines) + "\n")
        clock.advance(2)
        rec._scan()
        assert [(s.sequence, s.real_start) for s in rec.segments[:3]] == before
