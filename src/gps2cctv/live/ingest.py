"""GPS 수신(Traccar 호환).

Traccar Client 앱의 OsmAnd 프로토콜을 그대로 받는다. 앱의 서버 주소를 이 서버(기본 포트 5055)로
지정하면 된다. 확인한 형식(Traccar 서버의 OsmAndProtocolDecoder 기준):

- 쿼리/폼 방식: ``id``/``deviceid``, ``timestamp``(초·밀리초 epoch, ISO 8601,
  ``yyyy-MM-dd HH:mm:ss``), ``lat``, ``lon`` 또는 ``location=lat,lon``, ``speed``(**노트**),
  ``bearing``/``heading``, ``altitude``, ``accuracy``, ``hdop``, ``batt``, ``valid``, ``alarm``
- JSON 방식(``Content-Type: application/json``, 새 Traccar Client):
  ``{"device_id": ..., "location": {"timestamp": ISO, "coords": {"latitude", "longitude",
  "speed"(m/s, 음수=모름), "heading"(음수=모름), "accuracy", "altitude"}, "alarm"?, "extras"?}}``
- Traccar 서버의 위치 전달(forward) JSON: ``{"position": {...}, "device": {"uniqueId": ...}}``

등록되지 않은 기기 식별자는 거부하며 좌표를 저장·기록하지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..errors import Gps2CctvError
from ..timeutil import TimeInputError, parse_timestamp

log = logging.getLogger(__name__)

KNOT_TO_MPS = 0.514444
HDOP_TO_METERS = 5.0
MAX_FUTURE_SKEW = timedelta(minutes=5)
# 앱의 오프라인 버퍼링으로 늦게 오는 위치는 받되, 이보다 오래된 위치는 잘못된 값으로 본다.
MAX_PAST_AGE = timedelta(days=30)
REQUEST_TIMEOUT_SECONDS = 15


class IngestError(Gps2CctvError):
    """수신한 위치 형식 오류."""


class UnknownDeviceError(IngestError):
    """등록되지 않은 기기."""


@dataclass(frozen=True)
class Fix:
    device_id: str
    recorded_at: datetime
    received_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    speed_mps: float | None = None
    heading_deg: float | None = None
    alarm: str | None = None


def _float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"{name} 값이 숫자가 아닙니다.") from exc
    if not math.isfinite(result):
        raise IngestError(f"{name} 값이 숫자가 아닙니다.")
    return result


def _optional(value: Any, name: str, *, negative_is_unknown: bool = False) -> float | None:
    if value is None or value == "":
        return None
    result = _float(value, name)
    if negative_is_unknown and result < 0:
        return None
    return result


def parse_time(value: Any, received_at: datetime) -> datetime:
    if value is None or value == "":
        return received_at
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is None:
            try:
                if "T" in text:
                    return parse_timestamp(text, assume_tz="UTC")
                return parse_timestamp(text.replace(" ", "T"), assume_tz="UTC")
            except TimeInputError as exc:
                raise IngestError("timestamp 형식이 올바르지 않습니다.") from exc
    if number < 2**31 - 1:  # Traccar와 같은 규칙: 작으면 초, 크면 밀리초
        number *= 1000
    try:
        return datetime.fromtimestamp(number / 1000.0, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise IngestError("timestamp 범위가 올바르지 않습니다.") from exc


def _validate(fix: Fix) -> Fix:
    if not (-90 <= fix.latitude <= 90 and -180 <= fix.longitude <= 180):
        raise IngestError("좌표 범위가 올바르지 않습니다.")
    if fix.latitude == 0 and fix.longitude == 0:
        raise IngestError("좌표가 0,0 입니다(위치 미확정).")
    if fix.recorded_at > fix.received_at + MAX_FUTURE_SKEW:
        raise IngestError("위치 기록 시각이 현재보다 미래입니다.")
    if fix.recorded_at < fix.received_at - MAX_PAST_AGE:
        raise IngestError("위치 기록 시각이 너무 오래되었습니다.")
    if fix.accuracy_m is not None and fix.accuracy_m < 0:
        return Fix(**{**fix.__dict__, "accuracy_m": None})
    return fix


def parse_query(params: dict[str, list[str]], received_at: datetime) -> Fix | None:
    """OsmAnd 쿼리/폼 파라미터. 위치가 없거나 valid=false 이면 None."""

    def get(*names: str) -> str | None:
        for name in names:
            values = params.get(name)
            if values:
                return values[-1]
        return None

    device_id = get("id", "deviceid")
    if not device_id:
        raise IngestError("기기 식별자(id)가 없습니다.")
    valid = get("valid")
    if valid is not None and valid.lower() not in ("true", "1"):
        return None
    lat, lon = get("lat"), get("lon")
    location = get("location")
    if (lat is None or lon is None) and location:
        parts = location.split(",")
        if len(parts) >= 2:
            lat, lon = parts[0], parts[1]
    if lat is None or lon is None:
        return None
    speed_knots = _optional(get("speed"), "speed", negative_is_unknown=True)
    accuracy = _optional(get("accuracy"), "accuracy", negative_is_unknown=True)
    if accuracy is None:
        hdop = _optional(get("hdop"), "hdop", negative_is_unknown=True)
        accuracy = hdop * HDOP_TO_METERS if hdop is not None else None
    return _validate(
        Fix(
            device_id=device_id,
            recorded_at=parse_time(get("timestamp"), received_at),
            received_at=received_at,
            latitude=_float(lat, "lat"),
            longitude=_float(lon, "lon"),
            accuracy_m=accuracy,
            speed_mps=speed_knots * KNOT_TO_MPS if speed_knots is not None else None,
            heading_deg=_optional(get("bearing", "heading"), "bearing", negative_is_unknown=True),
            alarm=(get("alarm") or None),
        )
    )


def _parse_client_json(root: dict[str, Any], received_at: datetime) -> Fix | None:
    device_id = root.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise IngestError("기기 식별자(device_id)가 없습니다.")
    location = root.get("location")
    if not isinstance(location, dict):
        raise IngestError("location 객체가 없습니다.")
    coords = location.get("coords")
    if not isinstance(coords, dict):
        return None
    extras = location.get("extras") if isinstance(location.get("extras"), dict) else {}
    alarm = location.get("alarm") or extras.get("alarm")
    return _validate(
        Fix(
            device_id=device_id,
            recorded_at=parse_time(location.get("timestamp"), received_at),
            received_at=received_at,
            latitude=_float(coords.get("latitude"), "latitude"),
            longitude=_float(coords.get("longitude"), "longitude"),
            accuracy_m=_optional(coords.get("accuracy"), "accuracy", negative_is_unknown=True),
            speed_mps=_optional(coords.get("speed"), "speed", negative_is_unknown=True),
            heading_deg=_optional(coords.get("heading"), "heading", negative_is_unknown=True),
            alarm=str(alarm) if alarm else None,
        )
    )


def _parse_forward_json(root: dict[str, Any], received_at: datetime) -> Fix | None:
    position, device = root.get("position"), root.get("device")
    if not isinstance(position, dict) or not isinstance(device, dict):
        raise IngestError("position/device 객체가 없습니다.")
    device_id = device.get("uniqueId")
    if not isinstance(device_id, str) or not device_id:
        raise IngestError("device.uniqueId 가 없습니다.")
    if position.get("valid") is False:
        return None
    attributes = position.get("attributes") if isinstance(position.get("attributes"), dict) else {}
    speed_knots = _optional(position.get("speed"), "speed", negative_is_unknown=True)
    return _validate(
        Fix(
            device_id=device_id,
            recorded_at=parse_time(
                position.get("fixTime") or position.get("deviceTime"), received_at
            ),
            received_at=received_at,
            latitude=_float(position.get("latitude"), "latitude"),
            longitude=_float(position.get("longitude"), "longitude"),
            accuracy_m=_optional(position.get("accuracy"), "accuracy", negative_is_unknown=True)
            or None,
            speed_mps=speed_knots * KNOT_TO_MPS if speed_knots is not None else None,
            heading_deg=_optional(position.get("course"), "course", negative_is_unknown=True),
            alarm=str(attributes["alarm"]) if attributes.get("alarm") else None,
        )
    )


def parse_json(body: Any, received_at: datetime) -> list[Fix]:
    items = body if isinstance(body, list) else [body]
    fixes = []
    for item in items:
        if not isinstance(item, dict):
            raise IngestError("JSON 항목은 객체여야 합니다.")
        fix = (
            _parse_forward_json(item, received_at)
            if "position" in item
            else _parse_client_json(item, received_at)
        )
        if fix is not None:
            fixes.append(fix)
    return fixes


def parse_request(
    query: str, body: bytes, content_type: str | None, received_at: datetime
) -> list[Fix]:
    """HTTP 요청 하나에서 위치들을 꺼낸다."""
    is_json = (content_type or "").lower().startswith("application/json") or (
        body[:1] in (b"{", b"[") and not query
    )
    if is_json:
        try:
            root = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise IngestError("JSON 형식이 올바르지 않습니다.") from exc
        return parse_json(root, received_at)
    params = parse_qs(query, keep_blank_values=False)
    if not params and body:
        params = parse_qs(body.decode("ascii", errors="replace"), keep_blank_values=False)
    fix = parse_query(params, received_at)
    return [fix] if fix else []


def device_ids_in(fixes: Iterable[Fix]) -> set[str]:
    return {f.device_id for f in fixes}


class GpsHttpServer:
    """Traccar 호환 위치 수신 HTTP 서버."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        allowed_devices: frozenset[str],
        on_fix: Callable[[Fix], None],
        status: Callable[[], dict[str, Any]] | None = None,
        max_body_bytes: int = 65536,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.allowed_devices = allowed_devices
        self.on_fix = on_fix
        self.status = status or (lambda: {})
        self.max_body_bytes = max_body_bytes
        self.clock = clock
        self.counters = {"accepted": 0, "rejected": 0, "unknown_device": 0}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "gps2cctv"
            timeout = REQUEST_TIMEOUT_SECONDS  # 느린 연결이 스레드를 붙잡지 않도록
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                # 기본 로그는 쿼리 문자열(좌표)을 남기므로 끈다.
                return

            def _reply(self, code: int, payload: bytes = b"", ctype: str = "text/plain") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def _handle(self, body: bytes) -> None:
                parts = urlsplit(self.path)
                if parts.path == "/health" and not parts.query and not body:
                    data = json.dumps({"ok": True, **outer.counters, **outer.status()})
                    self._reply(200, data.encode(), "application/json")
                    return
                code = outer.handle(parts.query, body, self.headers.get("Content-Type"))
                self._reply(code)

            def do_GET(self) -> None:
                self._handle(b"")

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._reply(400)
                    return
                if length < 0 or length > outer.max_body_bytes:
                    self._reply(413)
                    return
                self._handle(self.rfile.read(length) if length > 0 else b"")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def handle(self, query: str, body: bytes, content_type: str | None) -> int:
        try:
            fixes = parse_request(query, body, content_type, self.clock())
        except IngestError as exc:
            log.info("위치 거부: %s", exc)
            self._count("rejected")
            return 400
        unknown = device_ids_in(fixes) - self.allowed_devices
        if unknown:
            self._count("unknown_device")
            log.warning("등록되지 않은 기기의 위치를 거부했습니다(%d개).", len(unknown))
            return 404 if (content_type or "").startswith("application/json") else 400
        for fix in fixes:
            try:
                self.on_fix(fix)
            except Exception:  # 수신은 계속되어야 한다
                log.exception("위치 처리 중 오류")
                return 500
            self._count("accepted")
        return 200

    def _count(self, key: str) -> None:
        with self._lock:
            self.counters[key] += 1

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
