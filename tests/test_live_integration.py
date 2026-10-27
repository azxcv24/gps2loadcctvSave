"""실제 동작 통합 테스트(가짜 없음).

- ffmpeg가 실시간 HLS 스트림(테스트 패턴 영상)을 만들고 로컬 HTTP 서버가 내보낸다(=CCTV).
- ``gps2cctv serve`` 와 같은 LiveService 를 띄우고, Traccar 앱처럼 HTTP로 GPS를 실시간으로 보낸다.
- 카메라 앞을 지나가면 클립(원본 + mp4)이 저장되고, mp4 길이가 통과 구간 + 앞뒤 여유와 맞는지 본다.

ffmpeg가 없으면 건너뛴다. 약 30초 걸린다.
"""

from __future__ import annotations

import functools
import itertools
import json
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gps2cctv.config import Settings
from gps2cctv.geo import from_local_xy
from gps2cctv.live.ingest import KNOT_TO_MPS
from gps2cctv.live.serve import LiveService
from gps2cctv.timeutil import from_db

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe 필요",
)
DEVICE = "integration-phone-42"
LAT0, LON0 = 37.0, 127.0


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        return


@pytest.fixture
def live_camera(tmp_path):
    hls_dir = tmp_path / "hls"
    hls_dir.mkdir()
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "10",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "20",
            "-hls_flags",
            "delete_segments+program_date_time",
            "-hls_segment_filename",
            str(hls_dir / "seg_%05d.ts"),
            str(hls_dir / "live.m3u8"),
        ]
    )
    handler = functools.partial(QuietHandler, directory=str(hls_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 15
    while not (hls_dir / "live.m3u8").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    yield f"http://127.0.0.1:{server.server_address[1]}/live.m3u8"
    server.shutdown()
    ffmpeg.terminate()
    ffmpeg.wait(timeout=10)


def test_drive_past_real_stream_saves_clip(tmp_path, live_camera):
    cams = tmp_path / "my_cameras.json"
    cams.write_text(
        json.dumps(
            {
                "cameras": [
                    {
                        "provider_camera_id": "TESTCAM",
                        "name": "통합 테스트 카메라",
                        "latitude": LAT0,
                        "longitude": LON0,
                        "location_uncertainty_m": 5,
                        "coverage_radius_m": 60,
                        "status": "active",
                        "observed_at": "2026-10-27T00:00:00Z",
                        "stream_url": live_camera,
                        "stream_delay_seconds": 0.5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[server]
listen = "127.0.0.1"
data_dir = "data"

[[devices]]
id = "{DEVICE}"

[[camera_files]]
path = "my_cameras.json"
source_id = "my-cams"

[recording]
stream_delay_uncertainty_seconds = 1.0
segment_settle_seconds = 1.0
""",
        encoding="utf-8",
    )
    service = LiveService(config, tmp_path / "data" / "db.sqlite3", Settings(), port=0)
    service.start()
    runner = threading.Thread(target=service.run_until_stopped, daemon=True)
    runner.start()
    try:
        # 서쪽 180m 에서 동쪽으로 30 m/s, 0.5초마다 위치 전송(Traccar OsmAnd 형식, 속도는 노트)
        speed = 30.0
        for step in range(25):
            x = -180 + speed * 0.5 * step
            lat, lon = from_local_xy(x, 10, LAT0, LON0)
            query = (
                f"id={DEVICE}&lat={lat:.7f}&lon={lon:.7f}&timestamp={time.time():.3f}"
                f"&speed={speed / KNOT_TO_MPS:.3f}&bearing=90&accuracy=5"
            )
            urllib.request.urlopen(f"http://127.0.0.1:{service.port}/?{query}", timeout=5).read()
            time.sleep(0.5)

        db = tmp_path / "data" / "db.sqlite3"
        clip = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and clip is None:
            time.sleep(1)
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                clip = conn.execute("SELECT * FROM clip").fetchone()
    finally:
        service.stop()

    assert clip is not None, "클립이 저장되지 않았습니다"
    assert clip["status"] == "saved", clip["failure_reason"]
    assert clip["time_mapping"] == "program_date_time"
    original, viewing = Path(clip["original_path"]), Path(clip["viewing_path"])
    assert original.exists() and viewing.exists()
    # 통과 구간: 유효 반경 = 60 + 5 + 5 = 70m, 최근접 10m → 반경 안 약 2*69/30 ≈ 4.6초
    entry, exit_at = from_db(clip["entry_at"]), from_db(clip["exit_at"])
    passage = (exit_at - entry).total_seconds()
    assert 3.5 < passage < 6.0
    expected = passage + 2 + 2 + 2 * clip["time_uncertainty_seconds"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(viewing)],
        capture_output=True, check=True,
    )  # fmt: skip
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    assert abs(duration - expected) < 1.0, (duration, expected)
    assert abs((datetime.now(UTC) - entry).total_seconds()) < 120
    meta = json.loads(Path(clip["metadata_path"]).read_text(encoding="utf-8"))
    assert meta["files"]["original_sha256"] == clip["original_sha256"]


def test_ffmpeg_recorder_maps_segments(tmp_path, live_camera):
    """RTSP 등 HLS가 아닌 스트림에 쓰는 ffmpeg 녹화기(여기서는 같은 HLS 주소로 시험)."""
    from gps2cctv.live.recorder import FfmpegRecorder, StreamTiming, wait_until

    recorder = FfmpegRecorder(
        "cam", lambda force: live_camera, tmp_path / "buf", StreamTiming(0.5, 1.0, 1.0),
        segment_seconds=1.0,
    )  # fmt: skip
    started = datetime.now(UTC)
    recorder.start()
    try:
        assert wait_until(lambda: len(recorder.segments) >= 3, timeout=20)
    finally:
        recorder.stop(remove_files=False)
    segs = recorder.segments
    assert all(s.mapping == "ffmpeg_segment" and s.path.exists() for s in segs)
    assert all(
        a.real_end <= b.real_start + timedelta(seconds=0.01) for a, b in itertools.pairwise(segs)
    )
    assert segs[0].real_start >= started - timedelta(seconds=5)
