"""HLS(m3u8) 재생목록 해석.

실시간(live) 미디어 재생목록에서 세그먼트 번호·길이·주소와, 있으면
``#EXT-X-PROGRAM-DATE-TIME``(세그먼트의 실제 시각)을 꺼낸다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin

from ..errors import Gps2CctvError
from ..timeutil import TimeInputError, parse_timestamp


class PlaylistError(Gps2CctvError):
    """재생목록 형식 오류."""


@dataclass(frozen=True)
class Variant:
    uri: str
    bandwidth: int


@dataclass(frozen=True)
class PlaylistSegment:
    uri: str
    duration: float
    sequence: int
    program_date_time: datetime | None
    discontinuity: bool


@dataclass
class MediaPlaylist:
    target_duration: float
    media_sequence: int
    segments: list[PlaylistSegment] = field(default_factory=list)
    endlist: bool = False
    init_uri: str | None = None
    encrypted: bool = False
    byterange: bool = False

    @property
    def is_fmp4(self) -> bool:
        return self.init_uri is not None


def _attributes(text: str) -> dict[str, str]:
    """``KEY=VALUE,KEY="VALUE"`` 형식 속성."""
    result: dict[str, str] = {}
    key, value, in_quotes, reading_key = "", "", False, True
    for ch in text + ",":
        if reading_key:
            if ch == "=":
                reading_key = False
            elif ch != ",":
                key += ch
        elif ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            result[key.strip().upper()] = value.strip()
            key, value, reading_key = "", "", True
        else:
            value += ch
    return result


def parse_playlist(text: str, base_url: str) -> MediaPlaylist | list[Variant]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise PlaylistError("HLS 재생목록(#EXTM3U)이 아닙니다.")

    variants: list[Variant] = []
    pending_bandwidth: int | None = None
    playlist = MediaPlaylist(target_duration=0.0, media_sequence=0)
    duration: float | None = None
    pdt_next: datetime | None = None
    discontinuity = False
    sequence: int | None = None

    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attributes(line.split(":", 1)[1])
            try:
                pending_bandwidth = int(attrs.get("BANDWIDTH", "0"))
            except ValueError:
                pending_bandwidth = 0
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            playlist.target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            playlist.media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError as exc:
                raise PlaylistError("EXTINF 길이 형식 오류") from exc
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                pdt_next = parse_timestamp(line.split(":", 1)[1], assume_tz="UTC")
            except TimeInputError:
                pdt_next = None
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith(
            "#EXT-X-DISCONTINUITY-SEQUENCE"
        ):
            discontinuity = True
            pdt_next = None
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.endlist = True
        elif line.startswith("#EXT-X-KEY:"):
            method = _attributes(line.split(":", 1)[1]).get("METHOD", "NONE")
            if method.upper() != "NONE":
                playlist.encrypted = True
        elif line.startswith("#EXT-X-BYTERANGE"):
            playlist.byterange = True
        elif line.startswith("#EXT-X-MAP:"):
            uri = _attributes(line.split(":", 1)[1]).get("URI", "").strip('"')
            if uri:
                playlist.init_uri = urljoin(base_url, uri)
        elif line.startswith("#"):
            continue
        else:
            if pending_bandwidth is not None:
                variants.append(Variant(urljoin(base_url, line), pending_bandwidth))
                pending_bandwidth = None
                continue
            if duration is None:
                raise PlaylistError("EXTINF 없는 세그먼트가 있습니다.")
            if sequence is None:
                sequence = playlist.media_sequence
            playlist.segments.append(
                PlaylistSegment(
                    uri=urljoin(base_url, line),
                    duration=duration,
                    sequence=sequence,
                    program_date_time=pdt_next,
                    discontinuity=discontinuity,
                )
            )
            if pdt_next is not None:
                pdt_next = pdt_next + timedelta(seconds=duration)
            sequence += 1
            duration = None
            discontinuity = False

    if variants:
        return variants
    if playlist.target_duration <= 0 and playlist.segments:
        playlist.target_duration = max(s.duration for s in playlist.segments)
    return playlist


def best_variant(variants: list[Variant]) -> Variant:
    """증거 품질을 위해 가장 높은 화질(대역폭)을 고른다."""
    return max(variants, key=lambda v: v.bandwidth)
