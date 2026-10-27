"""``gps2cctv serve``: GPS 수신 → 카메라 녹화 대기 → 통과 구간 자동 저장을 한 프로세스로 실행."""

from __future__ import annotations

import json
import logging
import re
import shutil
import signal
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from ..catalog import SourceSpec, register_specs, review_source, sync_cameras
from ..config import Settings
from ..db import connect
from ..policy import ACTIONS, latest_snapshot
from ..timeutil import from_db, utc_now
from .clips import CameraRef
from .config import LiveConfig, load_live_config
from .engine import LiveEngine, Store
from .ingest import GpsHttpServer
from .its import ITS_SOURCE_ID, ItsCatalog
from .recorder import FfmpegRecorder, HlsRecorder, StreamRecorder, StreamTiming

log = logging.getLogger(__name__)

ITS_TERMS_URL = "https://www.its.go.kr/opendata/opendataList?service=cctv"
CONFIG_REVIEWER = "config(serve)"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def effective_settings(settings: Settings, live: LiveConfig) -> Settings:
    """설정 파일에 적은 ITS 주소는 소유자가 승인한 것으로 보고 허용 호스트에 더한다."""
    hosts = set(settings.allowed_hosts)
    if live.its is not None:
        host = urlparse(live.its.endpoint).hostname
        if host:
            hosts.add(host.lower())
    return replace(settings, allowed_hosts=tuple(sorted(hosts)))


def source_specs(live: LiveConfig) -> list[SourceSpec]:
    specs = []
    if live.its is not None:
        specs.append(
            SourceSpec(
                id=ITS_SOURCE_ID,
                provider_name="국가교통정보센터(ITS) CCTV",
                environment="production",
                capabilities=("catalog", "preservation_request", "video"),
                official_endpoint=live.its.endpoint,
                terms_url=ITS_TERMS_URL,
                preservation_procedure=(
                    "ITS는 실시간 스트림만 제공합니다. 원본 영상 보관·제공은 해당 도로 관리 기관에 문의하세요."
                ),
                provider_retention_days=None,
                adapter={"type": "its"},
            )
        )
    for item in live.camera_files:
        path = Path(item.path)
        path = path if path.is_absolute() else live.base_dir / path
        specs.append(
            SourceSpec(
                id=item.source_id,
                provider_name=item.provider_name,
                environment="owned",
                capabilities=("catalog", "video"),
                official_endpoint="",
                terms_url="",
                preservation_procedure="본인 소유 장비 — 직접 보관",
                provider_retention_days=None,
                adapter={"type": "json_file", "path": str(path.resolve())},
            )
        )
    return specs


def setup_sources(
    store: Store, settings: Settings, live: LiveConfig, now: datetime, config_name: str
) -> list[str]:
    """설정 파일의 소스를 등록하고, 설정에 적은 것을 소유자 승인으로 보아 정책 검토 기록을 갱신한다."""
    specs = source_specs(live)
    if not specs:
        return []
    with store as conn:
        register_specs(conn, specs, now, settings)
        for spec in specs:
            actions = sorted(a for a, cap in ACTIONS.items() if cap in spec.capabilities)
            snapshot = latest_snapshot(conn, spec.id)
            fresh = (
                snapshot is not None
                and snapshot["reviewer_or_automation"] == CONFIG_REVIEWER
                and from_db(snapshot["valid_until"]) - now > timedelta(hours=24)
                and set(actions) <= set(json.loads(snapshot["allowed_actions"]))
                and from_db(snapshot["checked_at"])
                >= from_db(
                    conn.execute(
                        "SELECT material_changed_at FROM cctv_source WHERE id = ?", (spec.id,)
                    ).fetchone()[0]
                )
            )
            if not fresh:
                review_source(
                    conn, spec.id, actions=actions, terms_ref=f"config:{config_name}",
                    valid_days=2, reviewer=CONFIG_REVIEWER,
                    notes="serve 설정 파일에 등록된 소스(소유자 승인)", now=now, settings=settings,
                )  # fmt: skip
        for spec in specs:
            if spec.adapter["type"] == "json_file":
                result = sync_cameras(conn, spec.id, now, settings)
                log.info("카메라 목록 %s: %d대", spec.id, result.upserted)
    return [s.id for s in specs]


def make_recorder_factory(live: LiveConfig, catalog: ItsCatalog | None, ffmpeg: str | None):
    rec = live.recording
    buffer_root = live.data_dir / "buffer"

    def factory(cam: CameraRef, now: datetime, *, fallback: bool = False) -> StreamRecorder | None:
        if not cam.stream_url:
            return None
        timing = StreamTiming(
            delay_seconds=rec.stream_delay_seconds
            if cam.stream_delay_seconds is None
            else cam.stream_delay_seconds,
            delay_uncertainty_seconds=rec.stream_delay_uncertainty_seconds,
            pdt_uncertainty_seconds=rec.pdt_uncertainty_seconds,
            request_timeout_seconds=rec.stream_request_timeout_seconds,
        )
        buffer_dir = buffer_root / _SAFE_RE.sub("_", cam.id) / now.strftime("%Y%m%dT%H%M%S%f")
        if catalog is not None and cam.source_id == catalog.source_id:

            def url_provider(force: bool) -> str | None:
                return catalog.fresh_url(cam.id, force)
        else:
            static_url = cam.stream_url

            def url_provider(force: bool) -> str | None:
                return static_url

        if fallback or cam.stream_url.startswith(("rtsp://", "rtsps://")):
            if not ffmpeg:
                log.warning("ffmpeg가 없어 이 스트림을 녹화할 수 없습니다: %s", cam.name or cam.id)
                return None
            return FfmpegRecorder(cam.id, url_provider, buffer_dir, timing, ffmpeg=ffmpeg)
        return HlsRecorder(cam.id, url_provider, buffer_dir, timing)

    return factory


class LiveService:
    """serve 실행 단위(테스트에서도 그대로 띄울 수 있게 분리)."""

    def __init__(
        self,
        config_path: Path,
        db_path: Path,
        settings: Settings,
        *,
        live: LiveConfig | None = None,
        port: int | None = None,
    ) -> None:
        self.config_path = config_path
        self.live = live or load_live_config(config_path)
        if port is not None:
            self.live = replace(self.live, server=replace(self.live.server, port=port))
        self.settings = effective_settings(settings, self.live)
        self.ffmpeg = shutil.which("ffmpeg")
        self.store = Store(connect(db_path))
        self.stop_event = threading.Event()
        shutil.rmtree(self.live.data_dir / "buffer", ignore_errors=True)
        now = utc_now()
        self.sources = setup_sources(self.store, self.settings, self.live, now, config_path.name)
        self.catalog = (
            ItsCatalog(self.store, self.settings, self.live.its) if self.live.its else None
        )
        self.engine = LiveEngine(
            self.store,
            self.settings,
            self.live,
            recorder_factory=make_recorder_factory(self.live, self.catalog, self.ffmpeg),
            catalog=self.catalog,
            ffmpeg=self.ffmpeg,
        )
        self.server = GpsHttpServer(
            self.live.server.listen,
            self.live.server.port,
            allowed_devices=self.live.device_ids(),
            on_fix=self.engine.on_fix,
            status=self.engine.status,
            max_body_bytes=self.live.server.max_body_bytes,
        )
        self._server_thread: threading.Thread | None = None
        self._last_setup = now

    @property
    def port(self) -> int:
        return self.server.port

    def start(self) -> None:
        if self.catalog is not None:
            self.catalog.start()
        self._server_thread = threading.Thread(
            target=self.server.serve_forever, name="gps-http", daemon=True
        )
        self._server_thread.start()

    def run_until_stopped(self) -> None:
        while not self.stop_event.wait(1.0):
            now = utc_now()
            try:
                self.engine.tick(now)
                if now - self._last_setup > timedelta(hours=12):
                    self._last_setup = now
                    setup_sources(self.store, self.settings, self.live, now, self.config_path.name)
            except Exception:
                log.exception("주기 작업 중 오류")

    def stop(self) -> None:
        self.stop_event.set()
        self.server.shutdown()
        self.engine.shutdown()
        if self.catalog is not None:
            self.catalog.stop()
        with self.store as conn:
            conn.close()


def run_serve(config_path: Path, db_path: Path, settings: Settings) -> int:
    service = LiveService(config_path, db_path, settings)
    live = service.live
    for warning in live.warnings():
        log.warning(warning)
    log.info(
        "GPS 수신 대기: http://%s:%d  (기기 %d대, 카메라 소스: %s, ffmpeg: %s)",
        live.server.listen, service.port, len(live.devices),
        ", ".join(service.sources) or "없음", "있음" if service.ffmpeg else "없음",
    )  # fmt: skip
    log.info("클립 저장 위치: %s", live.data_dir / "clips")

    def handle(signum: int, _frame: object) -> None:
        log.info("종료 신호를 받았습니다. 대기 중인 클립을 저장하고 끝냅니다.")
        service.stop_event.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    service.start()
    try:
        service.run_until_stopped()
    finally:
        service.stop()
    return 0
