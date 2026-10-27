"""실시간 자동 녹화: GPS 수신 파싱, HLS 녹화기, ITS 카탈로그."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from gps2cctv.live.config import ItsConfig
from gps2cctv.live.hls import MediaPlaylist, PlaylistError, parse_playlist
from gps2cctv.live.ingest import (
    KNOT_TO_MPS,
    GpsHttpServer,
    IngestError,
    parse_query,
    parse_request,
)
from gps2cctv.live.its import its_records, parse_its_response
from gps2cctv.live.recorder import HlsRecorder, StreamTiming, UnsupportedStreamError

NOW = datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)


class TestIngestParsing:
    def test_osmand_query_speed_is_knots(self):
        fix = parse_query(
            {
                "id": ["dev1"], "lat": ["37.5"], "lon": ["127.0"], "timestamp": ["1793077200"],
                "speed": ["10"], "bearing": ["90"], "accuracy": ["4.5"],
            },
            NOW,
        )  # fmt: skip
        assert fix.speed_mps == pytest.approx(10 * KNOT_TO_MPS)
        assert fix.heading_deg == 90 and fix.accuracy_m == 4.5
        assert fix.recorded_at == datetime.fromtimestamp(1793077200, UTC)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1793077200000", datetime.fromtimestamp(1793077200, UTC)),
            ("2026-10-27T05:00:00Z", NOW),
            ("2026-10-27 05:00:00", NOW),
            ("", NOW),
        ],
    )
    def test_timestamp_formats(self, raw, expected):
        params = {"id": ["d"], "lat": ["37"], "lon": ["127"], "timestamp": [raw]}
        assert parse_query(params, NOW).recorded_at == expected

    def test_location_param_and_hdop(self):
        fix = parse_query({"id": ["d"], "location": ["37.1,127.2"], "hdop": ["2"]}, NOW)
        assert (fix.latitude, fix.longitude, fix.accuracy_m) == (37.1, 127.2, 10.0)

    def test_invalid_fix_is_ignored(self):
        assert (
            parse_query({"id": ["d"], "lat": ["37"], "lon": ["127"], "valid": ["false"]}, NOW)
            is None
        )

    def test_future_and_bad_values_rejected(self):
        with pytest.raises(IngestError):
            parse_query({"id": ["d"], "lat": ["99"], "lon": ["127"]}, NOW)
        future = str(int((NOW + timedelta(hours=1)).timestamp()))
        with pytest.raises(IngestError):
            parse_query({"id": ["d"], "lat": ["37"], "lon": ["127"], "timestamp": [future]}, NOW)

    def test_traccar_client_json(self):
        body = json.dumps(
            {
                "device_id": "dev1",
                "location": {
                    "timestamp": "2026-10-27T04:59:58.500Z",
                    "coords": {"latitude": 37.5, "longitude": 127.0, "speed": 12.5,
                               "heading": -1, "accuracy": 6, "altitude": 30},
                    "extras": {"alarm": "sos"},
                },
            }
        ).encode()  # fmt: skip
        (fix,) = parse_request("", body, "application/json", NOW)
        assert fix.speed_mps == 12.5 and fix.heading_deg is None and fix.alarm == "sos"
        assert fix.recorded_at == NOW - timedelta(seconds=1.5)

    def test_traccar_forward_json(self):
        body = json.dumps(
            {
                "position": {"latitude": 37.5, "longitude": 127.0, "speed": 20, "course": 45,
                             "accuracy": 0, "fixTime": "2026-10-27T05:00:00.000+00:00"},
                "device": {"uniqueId": "dev1", "name": "x"},
            }
        ).encode()  # fmt: skip
        (fix,) = parse_request("", body, "application/json", NOW)
        assert fix.device_id == "dev1" and fix.speed_mps == pytest.approx(20 * KNOT_TO_MPS)
        assert fix.accuracy_m is None and fix.heading_deg == 45


class TestIngestServer:
    def test_http_server_accepts_only_registered_devices(self):
        received = []
        server = GpsHttpServer(
            "127.0.0.1", 0, allowed_devices=frozenset({"my-device-123"}), on_fix=received.append,
            clock=lambda: NOW,
        )  # fmt: skip
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.port}"
        try:
            with urllib.request.urlopen(f"{base}/?id=my-device-123&lat=37.5&lon=127") as r:
                assert r.status == 200
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(f"{base}/?id=stranger&lat=37.5&lon=127")
            assert info.value.code == 400
            with urllib.request.urlopen(f"{base}/health") as r:
                health = json.loads(r.read())
            assert health["accepted"] == 1 and health["unknown_device"] == 1
            assert len(received) == 1 and received[0].device_id == "my-device-123"
        finally:
            server.shutdown()


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=300000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000
high/index.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-PROGRAM-DATE-TIME:2026-10-27T05:00:00.000+0000
#EXTINF:2.000,
seg100.ts
#EXTINF:2.000,
seg101.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.000,
seg102.ts
"""


class TestPlaylist:
    def test_master_playlist(self):
        variants = parse_playlist(MASTER, "https://cam.example/live/master.m3u8")
        assert [v.uri for v in variants][1] == "https://cam.example/live/high/index.m3u8"

    def test_media_playlist_pdt_and_discontinuity(self):
        pl = parse_playlist(MEDIA, "https://cam.example/live/index.m3u8")
        assert isinstance(pl, MediaPlaylist)
        assert [s.sequence for s in pl.segments] == [100, 101, 102]
        assert pl.segments[0].program_date_time == datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)
        assert pl.segments[1].program_date_time == datetime(2026, 10, 27, 5, 0, 2, tzinfo=UTC)
        assert pl.segments[2].program_date_time is None and pl.segments[2].discontinuity
        assert pl.segments[0].uri == "https://cam.example/live/seg100.ts"

    def test_not_hls(self):
        with pytest.raises(PlaylistError):
            parse_playlist("<html>", "https://x")

    def test_encrypted_and_vod_are_unsupported(self):
        timing = StreamTiming(3, 4, 1)

        def fetch_text(text):
            return lambda url, timeout, max_bytes: text.encode()

        for text in (
            MEDIA.replace("#EXT-X-VERSION:3", '#EXT-X-KEY:METHOD=AES-128,URI="k"'),
            MEDIA + "#EXT-X-ENDLIST\n",
        ):
            rec = HlsRecorder("c", lambda force: "https://x/i.m3u8", _tmp(), timing,
                              fetch=fetch_text(text), clock=lambda: NOW)  # fmt: skip
            with pytest.raises(UnsupportedStreamError):
                rec.poll_once()


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


class TestRecorderTiming:
    def test_pdt_mapping(self, tmp_path):
        files = {"https://x/index.m3u8": MEDIA.encode()}
        rec = HlsRecorder(
            "c", lambda force: "https://x/index.m3u8", tmp_path, StreamTiming(3, 4, 1),
            fetch=lambda url, t, m: files.get(url, b"SEG:" + url.encode()),
            clock=lambda: datetime(2026, 10, 27, 5, 0, 10, tzinfo=UTC),
        )  # fmt: skip
        rec.poll_once()
        first, second, third = rec.segments
        assert first.mapping == "program_date_time" and first.uncertainty_seconds == 1
        assert first.real_start == datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)
        assert second.real_end == datetime(2026, 10, 27, 5, 0, 4, tzinfo=UTC)
        # PDT가 끊긴 세그먼트는 수신 시각으로 추정
        assert third.mapping == "receive_time"
        assert (tmp_path / "0000000100.ts").read_bytes() == b"SEG:https://x/seg100.ts"

    def test_receive_time_mapping_chains_segments(self, tmp_path):
        clock = {"now": datetime(2026, 10, 27, 5, 0, 10, tzinfo=UTC)}
        playlist = {"text": MEDIA.replace("#EXT-X-PROGRAM-DATE-TIME:2026-10-27T05:00:00.000+0000\n", "")
                    .replace("#EXT-X-DISCONTINUITY\n", "")}  # fmt: skip
        rec = HlsRecorder(
            "c", lambda force: "https://x/index.m3u8", tmp_path, StreamTiming(3, 4, 1),
            fetch=lambda url, t, m: playlist["text"].encode() if url.endswith("m3u8") else b"x",
            clock=lambda: clock["now"],
        )  # fmt: skip
        rec.poll_once()
        last = rec.segments[-1]
        # 가장 새 세그먼트 끝 = 수신 시각 - 지연(3) - 폴링 간격/2(0.5)
        assert last.real_end == clock["now"] - timedelta(seconds=3.5)
        assert last.uncertainty_seconds == pytest.approx(4.5)
        # 다음 세그먼트는 앞 세그먼트 끝에 이어 붙인다
        clock["now"] += timedelta(seconds=2)
        playlist["text"] += "#EXTINF:2.000,\nseg103.ts\n"
        rec.poll_once()
        assert rec.segments[-1].sequence == 103
        assert rec.segments[-1].real_start == last.real_end

    def test_prune_keeps_pinned(self, tmp_path):
        files = {"https://x/index.m3u8": MEDIA.encode()}
        rec = HlsRecorder(
            "c", lambda force: "https://x/index.m3u8", tmp_path, StreamTiming(3, 4, 1),
            fetch=lambda url, t, m: files.get(url, b"seg"), clock=lambda: NOW,
        )  # fmt: skip
        rec.poll_once()
        base = datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)
        removed = rec.prune(base + timedelta(hours=1), [(base, base + timedelta(seconds=1))])
        assert removed == 2 and [s.sequence for s in rec.segments] == [100]


class TestIts:
    def test_parse_json(self):
        body = json.dumps(
            {"response": {"coordtype": 1, "datacount": 2, "data": [
                {"cctvname": "[경부선] 테스트1", "coordx": "127.1", "coordy": "37.2",
                 "cctvurl": "https://cctv.example/a/playlist.m3u8", "cctvformat": "HLS"},
                {"cctvname": "좌표 없음", "coordx": "", "coordy": "", "cctvurl": "https://b"},
            ]}}
        ).encode()  # fmt: skip
        items = parse_its_response(body)
        records = its_records(items, ItsConfig(api_key="k"), NOW, "ex")
        assert len(records) == 1
        rec = records[0]
        assert (rec.latitude, rec.longitude) == (37.2, 127.1)
        assert rec.stream_url == "https://cctv.example/a/playlist.m3u8"
        assert rec.provider_camera_id.startswith("its-") and rec.road_name == "고속도로"

    def test_parse_xml_and_single_object(self):
        xml = (
            "<response><data><cctvname>국도1</cctvname><coordx>127.3</coordx>"
            "<coordy>37.4</coordy><cctvurl>http://c/x.m3u8</cctvurl></data></response>"
        ).encode()
        assert parse_its_response(xml)[0]["cctvname"] == "국도1"
        single = json.dumps({"response": {"data": {"cctvname": "a", "coordx": 1, "coordy": 2,
                                                   "cctvurl": "u"}}}).encode()  # fmt: skip
        assert len(parse_its_response(single)) == 1

    def test_error_response(self):
        from gps2cctv.live.its import ItsError

        with pytest.raises(ItsError):
            parse_its_response(json.dumps({"resultCode": "99", "message": "인증키 오류"}).encode())
