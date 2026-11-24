"""코드 리뷰에서 찾은 문제의 회귀 테스트."""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps2cctv.geo import from_local_xy
from gps2cctv.live.ingest import Fix, GpsHttpServer, IngestError, parse_query, parse_request
from gps2cctv.live.recorder import HlsRecorder, StreamTiming

from .test_live_engine import (  # noqa: F401 — world 는 pytest fixture
    DEVICE,
    LAT0,
    LON0,
    clips,
    drive,
    idle,
    straight,
    world,
)

NOW = datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)


class TestPassageLifecycle:
    def test_phone_goes_silent_inside_radius_still_saves(self, world):
        clock, _s, engine, store, _ = world()
        drive(engine, clock, straight(x0=-600, x1=0))  # 카메라 앞에서 위치가 끊김
        idle(engine, clock, 120)
        (clip,) = clips(store)
        assert clip["status"] == "saved"
        assert "gps_lost" in json.loads(clip["flags"])
        assert engine.watches == {} and engine.open == {}

    def test_slow_traffic_saves_arrival_and_rest(self, world):
        clock, _s, engine, store, _ = world()
        slow = [(-150 + 1.0 * k, 0.0, 1.0) for k in range(300)]  # 1 m/s 로 반경 통과(약 230초)
        drive(engine, clock, slow + straight(x0=150, x1=400))
        idle(engine, clock, 40)
        rows = clips(store)
        assert [json.loads(r["flags"]) for r in rows] == [["capped"], ["continuation"]]
        # 두 클립의 저장 구간이 빈틈·겹침 없이 이어진다
        assert rows[0]["window_end"] == rows[1]["window_start"]
        assert all(r["status"] == "saved" for r in rows)

    def test_park_then_leave_saves_arrival_and_departure(self, world):
        clock, _s, engine, store, _ = world()
        park = [(0.0, 0.0, 0.0)] * 600
        drive(engine, clock, straight(x0=-300, x1=0) + park + straight(x0=0, x1=300))
        idle(engine, clock, 40)
        flags = [json.loads(r["flags"]) for r in clips(store)]
        assert flags[0] == ["capped"]
        assert "departure" in flags[1] and len(flags) == 2

    def test_parked_with_gps_jitter_stops_recording(self, world):
        clock, _s, engine, store, _ = world()
        park = [((40.0 if k % 2 else 0.0), 0.0, 0.0) for k in range(2400)]  # 40분, 40m 흔들림
        drive(engine, clock, straight(x0=-300, x1=0) + park)
        assert len(clips(store)) == 1
        assert engine.watches == {}

    def test_inaccurate_fix_does_not_arm(self, world):
        clock, _s, engine, store, made = world()
        lat, lon = from_local_xy(-100, 0, LAT0, LON0)
        engine.on_fix(Fix(DEVICE, clock(), clock(), lat, lon, 2000.0, 15.0, 90.0))
        assert made == []
        with store as conn:  # 위치 자체는 저장
            assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 1


class TestRecorderSwitch:
    def test_fallback_keeps_old_buffer_for_pending_clip(self, world):
        clock, _s, engine, store, made = world()
        drive(engine, clock, straight(x0=-600, x1=150))  # 통과 직후(클립 예약됨, 아직 자르기 전)
        assert engine.plans, "통과 클립이 예약되어 있어야 함"
        old = made[0]
        old_dir = old.buffer_dir
        old.status = "unsupported"  # 예: 재생목록에 암호화 키가 나타남
        engine.ffmpeg = "ffmpeg"
        engine.tick(clock())
        assert len(made) == 2 and old_dir.exists()  # 새 녹화기로 바꿔도 옛 버퍼는 남음
        idle(engine, clock, 40)
        (clip,) = clips(store)
        assert clip["status"] in ("saved", "partial")
        k = int(
            (
                datetime(2026, 10, 27, 5, 0, 40, tzinfo=UTC) - (NOW - timedelta(seconds=60))
            ).total_seconds()
            // 2
        )
        assert f"[SEG{k}]".encode() in Path(clip["original_path"]).read_bytes()
        assert not old_dir.exists()  # 클립을 만든 뒤에는 정리


MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:{seq}
#EXT-X-PROGRAM-DATE-TIME:{pdt}
#EXTINF:2.000,
a{seq}.ts
#EXTINF:2.000,
b{seq}.ts
"""


class TestStreamTimestamps:
    def _rec(self, tmp_path, text, now):
        return HlsRecorder(
            "c", lambda force: "https://x/i.m3u8", tmp_path, StreamTiming(3, 4, 1),
            fetch=lambda url, t, m: text.encode() if url.endswith("m3u8") else url.encode(),
            clock=lambda: now,
        )  # fmt: skip

    def test_encoder_clock_fast_is_rejected(self, tmp_path):
        now = NOW
        text = MEDIA.format(seq=1, pdt=(now + timedelta(seconds=20)).isoformat())
        rec = self._rec(tmp_path, text, now)
        rec.poll_once()
        assert rec.pdt_rejected and {s.mapping for s in rec.segments} == {"receive_time"}
        assert rec.segments[-1].real_end == now - timedelta(seconds=3.5)  # 지연 3 + 폴링/2

    def test_local_time_without_offset_is_rejected(self, tmp_path):
        kst_as_utc = (NOW + timedelta(hours=9)).replace(tzinfo=None).isoformat()
        rec = self._rec(tmp_path, MEDIA.format(seq=1, pdt=kst_as_utc), NOW)
        rec.poll_once()
        assert rec.pdt_rejected and rec.pdt_skew_seconds == pytest.approx(9 * 3600 + 7.5, abs=1)

    def test_plausible_pdt_is_used(self, tmp_path):
        rec = self._rec(
            tmp_path, MEDIA.format(seq=1, pdt=(NOW - timedelta(seconds=12)).isoformat()), NOW
        )
        rec.poll_once()
        assert not rec.pdt_rejected and rec.segments[0].real_start == NOW - timedelta(seconds=12)

    def test_sequence_reset_does_not_overwrite_buffer(self, tmp_path):
        text = {"t": MEDIA.format(seq=5, pdt=(NOW - timedelta(seconds=10)).isoformat())}
        rec = HlsRecorder(
            "c", lambda force: "https://x/i.m3u8", tmp_path, StreamTiming(3, 4, 1),
            fetch=lambda url, t, m: text["t"].encode() if url.endswith("m3u8") else b"v-" + url.encode(),
            clock=lambda: NOW,
        )  # fmt: skip
        rec.poll_once()
        first = rec.segments[0]
        text["t"] = MEDIA.format(seq=0, pdt=(NOW - timedelta(seconds=6)).isoformat()).replace(
            "a0", "z0"
        )
        rec.poll_once()
        assert first.path.read_bytes() == b"v-https://x/a5.ts"
        assert len({s.path for s in rec.segments}) == len(rec.segments) == 4


class TestIngestHardening:
    def test_deeply_nested_json_is_rejected(self):
        body = b"[" * 100_000 + b"]" * 100_000
        with pytest.raises(IngestError):
            parse_request("", body, "application/json", NOW)

    def test_ancient_timestamp_rejected(self):
        with pytest.raises(IngestError):
            parse_query(
                {"id": ["d"], "lat": ["37"], "lon": ["127"], "timestamp": ["-5000000000"]}, NOW
            )

    def test_bad_content_length_gets_400(self):
        import threading

        server = GpsHttpServer("127.0.0.1", 0, allowed_devices=frozenset({"x"}),
                               on_fix=lambda f: None, clock=lambda: NOW)  # fmt: skip
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/", data=b"id=x", method="POST",
                headers={"Content-Length": "abc"},
            )  # fmt: skip
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(request, timeout=5)
            assert info.value.code == 400
        finally:
            server.shutdown()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 필요")
def test_ffmpeg_recorder_uses_real_segment_lengths(tmp_path):
    """키프레임 간격(4초)이 세그먼트 목표(2초)보다 길어도 길이·시각이 실제와 맞아야 한다."""
    from gps2cctv.live.recorder import FfmpegRecorder, wait_until

    src = tmp_path / "src.ts"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
         "-t", "14", "-c:v", "libx264", "-g", "40", "-keyint_min", "40", "-sc_threshold", "0",
         "-f", "mpegts", str(src)],
        check=True,
    )  # fmt: skip
    rec = FfmpegRecorder("cam", None, tmp_path / "buf", StreamTiming(0.0, 1.0, 1.0),
                         input_args=["-re", "-i", str(src)])  # fmt: skip
    rec.start()
    try:
        assert wait_until(lambda: len(rec.segments) >= 2, timeout=30)
    finally:
        rec.stop(remove_files=False)
    segs = rec.segments
    for seg in segs[:-1]:
        assert seg.duration == pytest.approx(4.0, abs=0.3)
    for a, b in itertools.pairwise(segs):
        assert abs((b.real_start - a.real_end).total_seconds()) < 0.01
