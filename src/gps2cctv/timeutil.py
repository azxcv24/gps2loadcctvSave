"""시각 파싱·저장·표시 도구.

내부 저장은 모두 UTC이며, 시간대 정보가 없는 시각은 명시적으로 시간대를 지정하지
않는 한 거부한다(잘못된 시간대 가정으로 확인 범위가 몇 시간씩 어긋나는 것을 막기 위함).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import Gps2CctvError

_DB_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class TimeInputError(Gps2CctvError):
    """시각·기간 입력 형식 오류."""


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeInputError(f"알 수 없는 시간대입니다: {name!r}") from exc


def parse_timestamp(text: str, assume_tz: str | None = None) -> datetime:
    """ISO 8601 시각을 UTC datetime으로 변환한다.

    시간대가 없는 입력은 ``assume_tz``가 주어진 경우에만 받아들인다.
    """
    raw = text.strip()
    if not raw:
        raise TimeInputError("시각이 비어 있습니다.")
    normalized = raw[:-1] + "+00:00" if raw[-1] in "Zz" else raw
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimeInputError(f"ISO 8601 형식의 시각이 아닙니다: {raw!r}") from exc
    if value.tzinfo is None:
        if assume_tz is None:
            raise TimeInputError(
                f"시간대가 없는 시각입니다: {raw!r}. "
                "예: 2026-10-26T14:32:10+09:00 처럼 시간대를 붙이거나 --assume-tz 를 지정하세요."
            )
        value = value.replace(tzinfo=get_zone(assume_tz))
    return value.astimezone(UTC)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise TimeInputError("내부 오류: 시간대가 없는 datetime입니다.")
    return value.astimezone(UTC)


def to_db(value: datetime) -> str:
    """정렬 가능한 고정 폭 UTC 문자열(밀리초)로 변환한다."""
    return require_aware(value).strftime(_DB_FORMAT)[:-4] + "Z"


def from_db(text: str) -> datetime:
    return datetime.strptime(text, _DB_FORMAT).replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_duration(text: str) -> float:
    """'90', '90s', '10m', '1.5h', '2d' 형식의 기간을 초로 변환한다."""
    match = _DURATION_RE.match(text)
    if not match:
        raise TimeInputError(f"기간 형식이 올바르지 않습니다: {text!r} (예: 90s, 10m, 1h)")
    return float(match.group(1)) * _DURATION_UNITS[match.group(2)]


def floor_to(value: datetime, step_seconds: int) -> datetime:
    """UTC 기준 ``step_seconds`` 단위로 내림한다."""
    if step_seconds <= 1:
        return value.replace(microsecond=0)
    ts = value.timestamp()
    return datetime.fromtimestamp(math.floor(ts / step_seconds) * step_seconds, UTC)


def ceil_to(value: datetime, step_seconds: int) -> datetime:
    """UTC 기준 ``step_seconds`` 단위로 올림한다."""
    step = max(step_seconds, 1)
    ts = value.timestamp()
    return datetime.fromtimestamp(math.ceil(ts / step) * step, UTC)


def shift(value: datetime, seconds: float) -> datetime:
    return value + timedelta(seconds=seconds)


def seconds_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def fmt_local(value: datetime, tz_name: str, *, with_date: bool = True) -> str:
    """사람이 읽는 표시용 시각(초 단위 반올림)."""
    local = (value + timedelta(microseconds=500_000)).replace(microsecond=0)
    local = local.astimezone(get_zone(tz_name))
    if with_date:
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    return local.strftime("%H:%M:%S")


def fmt_seconds(value: float) -> str:
    """부호 있는 초 값을 사람이 읽기 쉽게 표시한다."""
    return f"{value:+.1f}초"
