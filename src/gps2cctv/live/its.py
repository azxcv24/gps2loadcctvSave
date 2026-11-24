"""국가교통정보센터(ITS) CCTV 오픈 API 연동.

요청: ``{endpoint}?apiKey=..&type=ex|its&cctvType=..&minX=..&maxX=..&minY=..&maxY=..&getType=json``
응답: ``response.data`` 목록(JSON) 또는 ``<data>`` 요소(XML). 각 항목에 ``cctvurl``(스트림 주소),
``coordx``(경도), ``coordy``(위도), ``cctvname`` 등이 있다. API 키는 ITS 누리집에서 발급한다.

전국 카메라를 한 번에 받지 않고, 기기 주변 타일(기본 0.1도)만 필요할 때 갱신한다.
스트림 주소에 시간 제한이 있을 수 있어 녹화를 시작하기 직전에 오래된 주소는 다시 받는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ..catalog import CameraRecord, upsert_camera
from ..config import Settings
from ..errors import Gps2CctvError
from ..geo import degree_margins
from ..policy import evaluate
from ..timeutil import from_db, to_db, utc_now
from .config import ItsConfig
from .recorder import Fetcher, FetchError, http_fetch

log = logging.getLogger(__name__)

ITS_SOURCE_ID = "its"
MAX_RESPONSE_BYTES = 10_000_000


class ItsError(Gps2CctvError):
    """ITS API 오류."""


def _text_items_from_xml(text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ItsError("ITS 응답(XML) 형식 오류") from exc
    items = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "data" and len(element):
            items.append({c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in element})
    if not items:
        message = " ".join(t.strip() for t in root.itertext() if t.strip())
        if any(word in message for word in ("인증", "KEY", "key", "ERROR", "error")):
            raise ItsError(f"ITS 응답 오류: {message[:200]}")
    return items


def parse_its_response(body: bytes) -> list[dict[str, Any]]:
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if text.startswith("<"):
        return _text_items_from_xml(text)
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ItsError("ITS 응답 형식 오류") from exc
    container = root.get("response", root) if isinstance(root, dict) else root
    data = container.get("data") if isinstance(container, dict) else container
    if data is None:
        message = json.dumps(root, ensure_ascii=False)[:200]
        if isinstance(container, dict) and ("resultCode" in container or "message" in container):
            raise ItsError(f"ITS 응답 오류: {message}")
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ItsError("ITS 응답의 data 형식이 올바르지 않습니다.")
    return [item for item in data if isinstance(item, dict)]


def camera_id_for(name: str, lat: float, lon: float) -> str:
    """ITS 응답에는 고정 카메라 ID가 없어 이름·좌표로 안정적인 ID를 만든다."""
    digest = hashlib.sha1(f"{name}|{lat:.4f}|{lon:.4f}".encode()).hexdigest()
    return f"its-{digest[:16]}"


def its_records(
    items: list[dict[str, Any]], cfg: ItsConfig, now: datetime, road_type: str
) -> list[CameraRecord]:
    records = []
    for item in items:
        url = str(item.get("cctvurl") or "").strip()
        try:
            lon = float(item.get("coordx"))
            lat = float(item.get("coordy"))
        except (TypeError, ValueError):
            continue
        if not (url and math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = str(item.get("cctvname") or "").strip() or None
        records.append(
            CameraRecord(
                provider_camera_id=camera_id_for(name or "", lat, lon),
                name=name,
                latitude=lat,
                longitude=lon,
                location_uncertainty_m=cfg.location_uncertainty_m,
                coverage_radius_m=cfg.coverage_radius_m,
                road_name={"ex": "고속도로", "its": "국도"}.get(road_type),
                road_direction=None,
                status="active",
                observed_at=now,
                clock=None,
                stream_url=url,
            )
        )
    return records


Tile = tuple[int, int]


class ItsCatalog:
    """기기 주변 타일 단위로 ITS 카메라 목록을 갱신한다."""

    def __init__(
        self,
        store: Any,
        settings: Settings,
        cfg: ItsConfig,
        *,
        source_id: str = ITS_SOURCE_ID,
        fetch: Fetcher = http_fetch,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.settings = settings
        self.cfg = cfg
        self.source_id = source_id
        self.fetch = fetch
        self.clock = clock
        self.fresh: dict[Tile, datetime] = {}
        self._queued: set[Tile] = set()
        self._queue: queue.Queue[Tile | None] = queue.Queue()
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._last_request = 0.0
        self._thread: threading.Thread | None = None
        self.errors = 0
        self.last_error: str | None = None

    # --- 타일 ----------------------------------------------------------------
    def tile_of(self, lat: float, lon: float) -> Tile:
        size = self.cfg.tile_degrees
        return math.floor(lat / size), math.floor(lon / size)

    def tiles_around(self, lat: float, lon: float, radius_m: float) -> list[Tile]:
        dlat, dlon = degree_margins(lat, radius_m)
        a = self.tile_of(lat - dlat, lon - dlon)
        b = self.tile_of(lat + dlat, lon + dlon)
        return [(i, j) for i in range(a[0], b[0] + 1) for j in range(a[1], b[1] + 1)]

    def _stale(self, tile: Tile, now: datetime) -> bool:
        stamp = self.fresh.get(tile)
        return stamp is None or now - stamp > timedelta(minutes=self.cfg.refresh_minutes)

    # --- 갱신 ----------------------------------------------------------------
    def request_area(self, lat: float, lon: float, radius_m: float) -> None:
        """비동기 갱신 요청(수신 처리를 막지 않음)."""
        now = self.clock()
        with self._lock:
            for tile in self.tiles_around(lat, lon, radius_m):
                if self._stale(tile, now) and tile not in self._queued:
                    self._queued.add(tile)
                    self._queue.put(tile)

    def _url(self, tile: Tile, road_type: str) -> str:
        size = self.cfg.tile_degrees
        params = {
            "apiKey": self.cfg.api_key,
            "type": road_type,
            "cctvType": self.cfg.cctv_type,
            "minX": f"{tile[1] * size:.6f}",
            "maxX": f"{(tile[1] + 1) * size:.6f}",
            "minY": f"{tile[0] * size:.6f}",
            "maxY": f"{(tile[0] + 1) * size:.6f}",
            "getType": "json",
        }
        return f"{self.cfg.endpoint}?{urlencode(params)}"

    def refresh_tile(self, tile: Tile) -> int:
        """타일 하나를 동기적으로 갱신하고 받은 카메라 수를 돌려준다."""
        now = self.clock()
        with self.store as conn:
            decision = evaluate(conn, self.source_id, "catalog_sync", now, self.settings)
        if not decision.allowed:
            raise ItsError(f"ITS 카탈로그 갱신이 정책상 거부되었습니다: {decision.reason_code}")
        records: list[CameraRecord] = []
        for road_type in self.cfg.road_types:
            with self._fetch_lock:
                wait = self.cfg.min_request_interval_seconds - (
                    time.monotonic() - self._last_request
                )
                if wait > 0:
                    time.sleep(wait)
                try:
                    body = self.fetch(
                        self._url(tile, road_type),
                        self.cfg.request_timeout_seconds,
                        MAX_RESPONSE_BYTES,
                    )
                finally:
                    self._last_request = time.monotonic()
            records += its_records(parse_its_response(body), self.cfg, now, road_type)
        stamp = to_db(now)
        size = self.cfg.tile_degrees
        with self.store as conn, conn:
            for rec in records:
                upsert_camera(conn, self.source_id, rec, stamp)
            # 이번 응답에 없는 이 타일의 카메라는 목록에서 빠진 것으로 표시(녹화 대상에서 제외).
            # 빈 응답은 일시 장애일 수 있으므로 표시하지 않는다.
            if records:
                conn.execute(
                    """
                    UPDATE camera SET status = 'missing_from_catalog'
                    WHERE source_id = ? AND synced_at < ? AND status != 'missing_from_catalog'
                      AND latitude >= ? AND latitude < ? AND longitude >= ? AND longitude < ?
                    """,
                    (
                        self.source_id, stamp, tile[0] * size, (tile[0] + 1) * size,
                        tile[1] * size, (tile[1] + 1) * size,
                    ),
                )  # fmt: skip
        with self._lock:
            self.fresh[tile] = now
        log.info("ITS 타일 %s 갱신: 카메라 %d대", tile, len(records))
        return len(records)

    def fresh_url(self, camera_id: str, force: bool = False) -> str | None:
        """녹화 직전 스트림 주소. 오래됐거나 거부되면(force) 해당 타일을 다시 받는다."""
        with self.store as conn:
            row = conn.execute(
                "SELECT latitude, longitude, stream_url, stream_url_fetched_at FROM camera "
                "WHERE id = ?",
                (camera_id,),
            ).fetchone()
        if row is None:
            return None
        fetched = from_db(row["stream_url_fetched_at"]) if row["stream_url_fetched_at"] else None
        max_age = timedelta(minutes=self.cfg.stream_url_max_age_minutes)
        now = self.clock()
        if force and fetched is not None and now - fetched < timedelta(seconds=60):
            force = False  # 방금 받은 주소로도 실패하면 API를 반복 호출하지 않는다
        if force or fetched is None or now - fetched > max_age:
            try:
                self.refresh_tile(self.tile_of(row["latitude"], row["longitude"]))
            except (FetchError, ItsError) as exc:
                self._note_error(exc)
                return row["stream_url"]
            with self.store as conn:
                row = conn.execute(
                    "SELECT stream_url FROM camera WHERE id = ?", (camera_id,)
                ).fetchone()
        return row["stream_url"] if row else None

    def _note_error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = str(exc)
        log.warning("ITS 갱신 실패: %s", exc)

    # --- 백그라운드 작업 -------------------------------------------------------
    def _process(self, tile: Tile) -> None:
        try:
            self.refresh_tile(tile)
        except (FetchError, ItsError) as exc:
            self._note_error(exc)
            with self._lock:  # 실패한 타일은 1분 뒤 다시 시도
                self.fresh[tile] = self.clock() - timedelta(minutes=self.cfg.refresh_minutes - 1)
        except Exception:
            log.exception("ITS 타일 갱신 중 오류")
        finally:
            with self._lock:
                self._queued.discard(tile)

    def process_pending(self) -> int:
        """대기 중인 타일 갱신을 지금 처리한다(테스트·동기 실행용)."""
        count = 0
        while True:
            try:
                tile = self._queue.get_nowait()
            except queue.Empty:
                return count
            if tile is None:
                return count
            self._process(tile)
            count += 1

    def start(self) -> None:
        def run() -> None:
            while True:
                tile = self._queue.get()
                if tile is None:
                    return
                self._process(tile)

        self._thread = threading.Thread(target=run, name="its-catalog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)
