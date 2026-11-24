"""ITS 타일 갱신, 실시간 설정 파일, clips CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from gps2cctv import catalog, db
from gps2cctv.cli import main
from gps2cctv.config import ConfigError, load_settings
from gps2cctv.live.config import ITS_API_KEY_ENV, load_live_config, parse_live_config
from gps2cctv.live.engine import Store
from gps2cctv.live.its import ItsCatalog, ItsError
from gps2cctv.live.serve import effective_settings, setup_sources

NOW = datetime(2026, 10, 27, 5, 0, 0, tzinfo=UTC)

CONFIG = """
[server]
port = 5055

[[devices]]
id = "my-phone-abcdef12345"
name = "내 휴대폰"

[its]
api_key = "TEST-KEY"
road_types = ["ex"]

[recording]
pre_seconds = 1.5
post_seconds = 1.5

[candidates]
default_coverage_radius_m = 120.0
"""


class FakeIts:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def fetch(self, url, timeout, max_bytes):
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.calls.append(params)
        cx = (float(params["minX"]) + float(params["maxX"])) / 2
        cy = (float(params["minY"]) + float(params["maxY"])) / 2
        token = len(self.calls)
        return json.dumps(
            {"response": {"data": [
                {"cctvname": "[테스트] 1번", "coordx": cx, "coordy": cy,
                 "cctvurl": f"https://stream.example/1.m3u8?token={token}"},
            ]}}
        ).encode()  # fmt: skip


@pytest.fixture
def its_world(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    live = load_live_config(path)
    settings = effective_settings(load_settings(path), live)
    store = Store(db.connect(tmp_path / "t.sqlite3"))
    clock = {"now": NOW}
    setup_sources(store, settings, live, NOW, "config.toml")
    fake = FakeIts(lambda: clock["now"])
    its = ItsCatalog(store, settings, live.its, fetch=fake.fetch, clock=lambda: clock["now"])
    its.cfg = type(live.its)(**{**live.its.__dict__, "min_request_interval_seconds": 0.0})
    return live, settings, store, its, fake, clock


class TestLiveConfig:
    def test_parse_and_defaults(self, its_world):
        live, settings, *_ = its_world
        assert live.device_ids() == {"my-phone-abcdef12345"}
        assert live.recording.pre_seconds == 1.5 and live.recording.max_concurrent_streams == 6
        assert live.its.api_key == "TEST-KEY" and live.its.road_types == ("ex",)
        # 같은 파일의 일반 설정도 그대로 읽힘
        assert settings.default_coverage_radius_m == 120.0
        assert "openapi.its.go.kr" in settings.allowed_hosts

    def test_api_key_from_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ITS_API_KEY_ENV, "ENV-KEY")
        live = parse_live_config({"its": {}, "devices": [{"id": "abcdefghijk"}]}, tmp_path)
        assert live.its.api_key == "ENV-KEY"

    def test_missing_api_key_and_unknown_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ITS_API_KEY_ENV, raising=False)
        with pytest.raises(ConfigError, match="api_key"):
            parse_live_config({"its": {}}, tmp_path)
        with pytest.raises(ConfigError, match="알 수 없는"):
            parse_live_config({"recording": {"pre_second": 1}}, tmp_path)

    def test_short_device_id_warning(self, tmp_path):
        live = parse_live_config({"devices": [{"id": "123456"}]}, tmp_path)
        assert any("짧습니다" in w for w in live.warnings())


class TestItsCatalog:
    def test_area_refresh_stores_cameras_with_stream(self, its_world):
        _live, _s, store, its, fake, _clock = its_world
        its.request_area(37.55, 127.05, 2000)
        assert its.process_pending() >= 1
        assert fake.calls[0]["apiKey"] == "TEST-KEY" and fake.calls[0]["type"] == "ex"
        assert fake.calls[0]["getType"] == "json"
        with store as conn:
            rows = conn.execute("SELECT * FROM camera WHERE source_id = 'its'").fetchall()
        assert rows and all(r["stream_url"].startswith("https://stream.example/") for r in rows)
        # 이미 새로 받은 타일은 다시 요청하지 않음
        calls = len(fake.calls)
        its.request_area(37.55, 127.05, 2000)
        its.process_pending()
        assert len(fake.calls) == calls

    def test_fresh_url_refreshes_expired_token(self, its_world):
        _live, _s, store, its, fake, clock = its_world
        its.request_area(37.55, 127.05, 500)
        its.process_pending()
        with store as conn:
            cam = conn.execute("SELECT id, stream_url FROM camera LIMIT 1").fetchone()
        assert its.fresh_url(cam["id"]) == cam["stream_url"]  # 아직 새 주소
        calls = len(fake.calls)
        clock["now"] += timedelta(minutes=10)
        url = its.fresh_url(cam["id"])
        assert len(fake.calls) > calls and url != cam["stream_url"]
        calls = len(fake.calls)
        its.fresh_url(cam["id"], force=True)  # 방금 받은 주소면 반복 호출하지 않음
        assert len(fake.calls) == calls
        clock["now"] += timedelta(seconds=61)
        its.fresh_url(cam["id"], force=True)  # 403 등으로 강제 갱신
        assert len(fake.calls) > calls

    def test_disabled_source_stops_its_requests(self, its_world):
        _live, settings, store, its, fake, _clock = its_world
        with store as conn:
            catalog.set_source_enabled(conn, "its", False, NOW, settings)
        with pytest.raises(ItsError):
            its.refresh_tile(its.tile_of(37.55, 127.05))
        assert fake.calls == []

    def test_sources_get_config_review_with_capture(self, its_world):
        _live, settings, store, *_ = its_world
        from gps2cctv.policy import evaluate

        with store as conn:
            for action in ("catalog_sync", "catalog_lookup", "media_capture"):
                assert evaluate(conn, "its", action, NOW, settings).allowed, action


class TestClipsCli:
    def _seed_clip(self, path, *, protected=0, retention="2026-10-01T00:00:00.000Z"):
        conn = db.connect(path)
        with conn:
            conn.execute(
                """
                INSERT INTO clip(id, device_id, camera_id, source_id, provider_camera_id,
                    camera_name, entry_at, exit_at, estimated_passage_at, window_start,
                    window_end, time_mapping, time_uncertainty_seconds, status, flags,
                    protected, retention_until, created_at, original_bytes)
                VALUES ('clip_aaaaaaaaaaaa', 'dev', 'its:x', 'its', 'x', '카메라',
                    '2026-10-26T05:00:00.000Z', '2026-10-26T05:00:05.000Z',
                    '2026-10-26T05:00:02.000Z', '2026-10-26T04:59:58.000Z',
                    '2026-10-26T05:00:07.000Z', 'program_date_time', 1, 'saved', '[]', ?, ?,
                    '2026-10-26T05:01:00.000Z', 100)
                """,
                (protected, retention),
            )
        conn.close()

    def test_list_protect_and_purge(self, tmp_path, capsys):
        path = tmp_path / "t.sqlite3"
        self._seed_clip(path)
        assert main(["--db", str(path), "clips", "list"]) == 0
        assert "clip_aaaaaaaaaaaa" in capsys.readouterr().out
        assert main(["--db", str(path), "clips", "protect", "clip_aaaaaaaaaaaa"]) == 0
        assert main(["--db", str(path), "purge"]) == 0  # 보호된 클립은 남음
        assert "자동 저장 클립 0건" in capsys.readouterr().out
        assert main(["--db", str(path), "clips", "unprotect", "clip_aaaaaaaaaaaa"]) == 0
        assert main(["--db", str(path), "purge"]) == 0
        assert "자동 저장 클립 1건" in capsys.readouterr().out
        assert main(["--db", str(path), "clips", "protect", "clip_aaaaaaaaaaaa"]) == 2

    def test_mark_protects_existing_clip(self, tmp_path, capsys):
        path = tmp_path / "t.sqlite3"
        self._seed_clip(path)
        code = main(["--db", str(path), "clips", "mark", "--device", "dev",
                     "--time", "2026-10-26T14:03:00+09:00"])  # fmt: skip
        assert code == 0 and "지금 보호한 클립 1건" in capsys.readouterr().out

    def test_serve_requires_config(self, tmp_path, capsys):
        assert main(["--db", str(tmp_path / "t.sqlite3"), "serve"]) == 2
        assert "--config" in capsys.readouterr().err


def test_settings_loader_ignores_live_sections(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(CONFIG, encoding="utf-8")
    assert load_settings(path).default_coverage_radius_m == 120.0


def test_its_section_allows_its_host_for_manual_commands(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(CONFIG, encoding="utf-8")
    assert "openapi.its.go.kr" in load_settings(path).allowed_hosts


def test_buffer_must_exceed_max_clip(tmp_path):
    with pytest.raises(ConfigError, match="buffer_seconds"):
        parse_live_config({"recording": {"max_clip_seconds": 600.0}}, tmp_path)
