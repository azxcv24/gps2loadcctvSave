"""카메라 스트림 녹화 대기(링 버퍼).

녹화기는 카메라 스트림을 짧은 세그먼트 단위로 받아 디스크 버퍼에 두고, 각 세그먼트가
**실제 몇 시부터 몇 시까지의 장면인지**를 추정해 함께 기록한다. 클립은 이 실제 시각을 기준으로
통과 구간에 걸치는 세그먼트를 골라 만든다.

시각 추정 방법:
- ``program_date_time``: 재생목록의 ``#EXT-X-PROGRAM-DATE-TIME`` 태그(정확)
- ``receive_time``: 태그가 없으면 세그먼트가 재생목록에 처음 나타난 시각에서 스트림 지연을 빼서 추정
- ``ffmpeg_segment``: HLS가 아닌 스트림(RTSP 등)을 ffmpeg로 받을 때 세그먼트가 닫힌 시각 기준
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..errors import Gps2CctvError
from ..timeutil import seconds_between, shift, utc_now
from .hls import MediaPlaylist, PlaylistError, best_variant, parse_playlist

log = logging.getLogger(__name__)

USER_AGENT = f"gps2cctv/{__version__}"
MAX_PLAYLIST_BYTES = 1_000_000
MAX_SEGMENT_BYTES = 50_000_000


class FetchError(Gps2CctvError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UnsupportedStreamError(Gps2CctvError):
    """이 녹화기로 받을 수 없는 스트림(암호화 HLS, 실시간이 아닌 영상 등)."""


Fetcher = Callable[[str, float, int], bytes]


def http_fetch(url: str, timeout: float, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"연결 실패: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise FetchError("응답이 너무 큽니다.")
    return data


@dataclass(frozen=True)
class BufferedSegment:
    sequence: int
    path: Path
    duration: float
    real_start: datetime
    real_end: datetime
    mapping: str
    uncertainty_seconds: float
    size: int


@dataclass(frozen=True)
class StreamTiming:
    delay_seconds: float
    delay_uncertainty_seconds: float
    pdt_uncertainty_seconds: float
    request_timeout_seconds: float = 10.0


class StreamRecorder:
    """녹화기 공통 부분: 세그먼트 목록, 조회, 정리, 스레드 수명."""

    container = "ts"

    def __init__(self, camera_id: str, buffer_dir: Path, clock: Callable[[], datetime]) -> None:
        self.camera_id = camera_id
        self.buffer_dir = buffer_dir
        self.clock = clock
        self.segments: list[BufferedSegment] = []
        self.init_data: bytes | None = None
        self.status = "starting"
        self.error: str | None = None
        self.started_at = clock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        buffer_dir.mkdir(parents=True, exist_ok=True)

    # --- 조회 ---------------------------------------------------------------
    def segments_overlapping(self, start: datetime, end: datetime) -> list[BufferedSegment]:
        with self._lock:
            return [s for s in self.segments if s.real_end > start and s.real_start < end]

    def coverage(self) -> tuple[datetime, datetime] | None:
        with self._lock:
            if not self.segments:
                return None
            return self.segments[0].real_start, self.segments[-1].real_end

    def latest_end(self) -> datetime | None:
        cov = self.coverage()
        return cov[1] if cov else None

    @property
    def alive(self) -> bool:
        return self.status in ("starting", "recording")

    # --- 정리 ---------------------------------------------------------------
    def prune(self, keep_after: datetime, pinned: list[tuple[datetime, datetime]]) -> int:
        """``keep_after`` 이전에 끝났고 대기 중인 클립 구간과 겹치지 않는 세그먼트를 지운다."""
        removed = 0
        with self._lock:
            keep = []
            for seg in self.segments:
                needed = seg.real_end >= keep_after or any(
                    seg.real_end > a and seg.real_start < b for a, b in pinned
                )
                if needed:
                    keep.append(seg)
                else:
                    seg.path.unlink(missing_ok=True)
                    removed += 1
            self.segments = keep
        return removed

    def _add(self, segment: BufferedSegment) -> None:
        with self._lock:
            self.segments.append(segment)
            self.segments.sort(key=lambda s: (s.real_start, s.sequence))

    # --- 수명 ---------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"rec-{self.camera_id}", daemon=True)
        self._thread.start()

    def stop(self, *, remove_files: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        self._on_stop()
        if self.status in ("starting", "recording"):
            self.status = "stopped"
        if remove_files:
            shutil.rmtree(self.buffer_dir, ignore_errors=True)

    def _on_stop(self) -> None:
        pass

    def _run(self) -> None:
        raise NotImplementedError


class HlsRecorder(StreamRecorder):
    """HLS 실시간 스트림 녹화기(외부 프로그램 없이 동작)."""

    def __init__(
        self,
        camera_id: str,
        url_provider: Callable[[bool], str | None],
        buffer_dir: Path,
        timing: StreamTiming,
        *,
        fetch: Fetcher = http_fetch,
        clock: Callable[[], datetime] = utc_now,
        max_consecutive_errors: int = 8,
    ) -> None:
        super().__init__(camera_id, buffer_dir, clock)
        self.url_provider = url_provider
        self.timing = timing
        self.fetch = fetch
        self.max_consecutive_errors = max_consecutive_errors
        self.media_url: str | None = None
        self.last_sequence: int | None = None
        self.poll_interval = 1.0
        self.mapping_used: set[str] = set()
        self._errors = 0

    def _playlist(self) -> MediaPlaylist:
        url = self.media_url or self.url_provider(False)
        if not url:
            raise UnsupportedStreamError("스트림 주소가 없습니다.")
        parsed = parse_playlist(self._get_text(url), url)
        if isinstance(parsed, list):
            variant = best_variant(parsed)
            url = variant.uri
            parsed = parse_playlist(self._get_text(url), url)
            if isinstance(parsed, list):
                raise UnsupportedStreamError("재생목록이 중첩되어 있습니다.")
        self.media_url = url
        if parsed.encrypted or parsed.byterange:
            raise UnsupportedStreamError("암호화 또는 바이트 범위 HLS는 ffmpeg 녹화기로 받습니다.")
        if parsed.endlist:
            raise UnsupportedStreamError("실시간 스트림이 아닙니다(#EXT-X-ENDLIST).")
        return parsed

    def _get_text(self, url: str) -> str:
        data = self.fetch(url, self.timing.request_timeout_seconds, MAX_PLAYLIST_BYTES)
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsupportedStreamError("HLS 재생목록이 아닙니다.") from exc

    def poll_once(self) -> float:
        """재생목록을 한 번 읽고 새 세그먼트를 받는다. 다음 폴링까지 기다릴 초를 돌려준다."""
        playlist = self._playlist()
        fetched_at = self.clock()
        target = playlist.target_duration or 2.0
        self.poll_interval = min(max(target / 2.0, 0.5), 5.0)
        if playlist.is_fmp4:
            self.container = "mp4"
            if self.init_data is None:
                self.init_data = self.fetch(
                    playlist.init_uri, self.timing.request_timeout_seconds, MAX_SEGMENT_BYTES
                )
        segments = playlist.segments
        if (
            self.last_sequence is not None
            and segments
            and segments[-1].sequence < self.last_sequence
        ):
            log.info("%s: 재생목록 번호가 초기화되어 다시 맞춥니다.", self.camera_id)
            self.last_sequence = None
        new = [s for s in segments if self.last_sequence is None or s.sequence > self.last_sequence]
        if not new:
            return self.poll_interval

        # 수신 시각 기준 추정: 가장 새 세그먼트는 "지금 - 스트림 지연 - 폴링 간격/2" 에 끝났다고 본다.
        anchored_end = shift(fetched_at, -(self.timing.delay_seconds + self.poll_interval / 2))
        anchored: list[tuple[datetime, datetime]] = []
        end = anchored_end
        for seg in reversed(new):
            start = shift(end, -seg.duration)
            anchored.append((start, end))
            end = start
        anchored.reverse()
        receive_unc = self.timing.delay_uncertainty_seconds + self.poll_interval / 2
        with self._lock:
            previous = self.segments[-1] if self.segments else None
        chain_ok = (
            previous is not None
            and previous.mapping == "receive_time"
            and new[0].sequence == previous.sequence + 1
            and not new[0].discontinuity
            and abs(seconds_between(previous.real_end, anchored[0][0])) <= max(2.0, target)
        )

        for index, seg in enumerate(new):
            data = self.fetch(seg.uri, self.timing.request_timeout_seconds, MAX_SEGMENT_BYTES)
            path = self.buffer_dir / f"{seg.sequence:010d}.{'m4s' if playlist.is_fmp4 else 'ts'}"
            path.write_bytes(data)
            if seg.program_date_time is not None:
                start = seg.program_date_time
                end = shift(start, seg.duration)
                mapping, unc = "program_date_time", self.timing.pdt_uncertainty_seconds
            else:
                if chain_ok and previous is not None:
                    start = previous.real_end
                else:
                    start = anchored[index][0]
                end = shift(start, seg.duration)
                mapping, unc = "receive_time", receive_unc
            buffered = BufferedSegment(
                seg.sequence, path, seg.duration, start, end, mapping, unc, len(data)
            )
            self._add(buffered)
            self.mapping_used.add(mapping)
            previous, chain_ok = buffered, mapping == "receive_time"
            self.last_sequence = seg.sequence
        self.status = "recording"
        return self.poll_interval

    def step(self) -> float:
        """폴링 한 번(오류 처리 포함). 다음 폴링까지 기다릴 초를 돌려준다."""
        try:
            delay = self.poll_once()
            self._errors = 0
            return delay
        except UnsupportedStreamError as exc:
            self.status, self.error = "unsupported", str(exc)
            log.info("%s: %s", self.camera_id, exc)
            return 0.0
        except (FetchError, PlaylistError) as exc:
            self._errors += 1
            self.error = str(exc)
            if isinstance(exc, FetchError) and exc.status in (401, 403, 404, 410):
                # 시간 제한 토큰이 만료됐을 수 있으므로 주소를 새로 받는다.
                self.media_url = None
                try:
                    self.url_provider(True)
                except Exception:
                    log.exception("%s: 스트림 주소 갱신 실패", self.camera_id)
            log.warning("%s: 스트림 읽기 실패(%d회): %s", self.camera_id, self._errors, exc)
        except Exception as exc:
            self._errors += 1
            self.error = type(exc).__name__
            log.exception("%s: 녹화 중 오류", self.camera_id)
        if self._errors >= self.max_consecutive_errors:
            self.status = "error"
        return min(30.0, 2.0**self._errors)

    def _run(self) -> None:
        while not self._stop.is_set() and self.alive:
            delay = self.step()
            if not self.alive:
                return
            self._stop.wait(delay)


class FfmpegRecorder(StreamRecorder):
    """HLS가 아닌 스트림(RTSP, 암호화 HLS 등)을 ffmpeg로 짧은 세그먼트로 받는다."""

    def __init__(
        self,
        camera_id: str,
        url_provider: Callable[[bool], str | None] | None,
        buffer_dir: Path,
        timing: StreamTiming,
        *,
        segment_seconds: float = 2.0,
        ffmpeg: str = "ffmpeg",
        input_args: list[str] | None = None,
        codec_args: list[str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(camera_id, buffer_dir, clock)
        if input_args is None and url_provider is None:
            raise UnsupportedStreamError("스트림 주소가 없습니다.")
        self.url_provider = url_provider
        self.input_args = input_args
        self.codec_args = codec_args or ["-c", "copy"]
        self.ffmpeg = ffmpeg
        self.timing = timing
        self.segment_seconds = segment_seconds
        self.process: subprocess.Popen | None = None
        self._seen: dict[str, datetime] = {}

    def _command(self) -> list[str]:
        input_args = self.input_args
        if input_args is None:
            url = self.url_provider(False) if self.url_provider else None
            if not url:
                raise UnsupportedStreamError("스트림 주소가 없습니다.")
            input_args = (["-rtsp_transport", "tcp"] if url.startswith("rtsp") else []) + [
                "-i",
                url,
            ]
        return [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *input_args,
            "-map", "0:v:0", *self.codec_args, "-f", "segment",
            "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
            str(self.buffer_dir / "seg_%08d.ts"),
        ]  # fmt: skip

    @staticmethod
    def available(ffmpeg: str = "ffmpeg") -> bool:
        return shutil.which(ffmpeg) is not None

    def _scan(self) -> None:
        now = self.clock()
        files = sorted(self.buffer_dir.glob("seg_*.ts"))
        if not files:
            return
        for path in files:
            self._seen.setdefault(path.name, now)
        known = {s.path.name for s in self.segments}
        finished = self.process is not None and self.process.poll() is not None
        # 다음 세그먼트 파일이 생겼으면 앞 세그먼트는 닫힌 것이다.
        completed: list[tuple[Path, datetime]] = []
        for current, nxt in zip(files, [*files[1:], None], strict=True):
            if current.name in known:
                continue
            if nxt is None and not finished:
                break
            completed.append((current, self._seen[nxt.name] if nxt is not None else now))
        if not completed:
            return
        # 끝 시각 = 닫힌 시각 - 스트림 지연. 접속 직후처럼 여러 세그먼트가 한꺼번에 닫히면(밀린 구간)
        # 가장 새 것을 기준으로 길이만큼 거꾸로 배치해 시간이 겹치지 않게 한다.
        lag = self.timing.delay_seconds + 0.25
        ends: list[datetime] = [now] * len(completed)
        starts: list[datetime] = [now] * len(completed)
        for i in reversed(range(len(completed))):
            end = shift(completed[i][1], -lag)
            if i + 1 < len(completed) and end > starts[i + 1]:
                end = starts[i + 1]
            ends[i], starts[i] = end, shift(end, -self.segment_seconds)
        with self._lock:
            previous = self.segments[-1] if self.segments else None
        prev_end = previous.real_end if previous is not None else None
        for i, (path, _closed) in enumerate(completed):
            if (
                prev_end is not None
                and abs(seconds_between(prev_end, starts[i])) < self.segment_seconds
            ):
                starts[i] = min(prev_end, ends[i])
            sequence = int(path.stem.split("_")[1])
            self._add(
                BufferedSegment(
                    sequence, path, seconds_between(starts[i], ends[i]), starts[i], ends[i],
                    "ffmpeg_segment", self.timing.delay_uncertainty_seconds + 0.5,
                    path.stat().st_size,
                )
            )  # fmt: skip
            prev_end = ends[i]
        self.status = "recording"

    def _run(self) -> None:
        try:
            command = self._command()
            self.process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        except UnsupportedStreamError as exc:
            self.status, self.error = "error", str(exc)
            return
        except OSError as exc:
            self.status, self.error = "error", f"ffmpeg 실행 실패: {exc}"
            return
        while not self._stop.is_set():
            self._scan()
            if self.process.poll() is not None:
                self._scan()
                self.status = "error" if self.process.returncode else "stopped"
                err = (
                    self.process.stderr.read().decode(errors="replace")
                    if self.process.stderr
                    else ""
                )
                self.error = err.strip().splitlines()[-1][:200] if err.strip() else None
                return
            self._stop.wait(0.25)

    def _on_stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._scan()


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    """테스트·종료 처리용 대기 도우미."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
