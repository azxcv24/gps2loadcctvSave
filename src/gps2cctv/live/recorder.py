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


class SegmentFetchError(FetchError):
    """세그먼트 하나를 받지 못함(재생목록 주소 자체는 유효할 수 있음)."""


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
    # PROGRAM-DATE-TIME 이 이 범위를 벗어나면(미래이거나 너무 과거) 인코더 시계가 틀린 것으로 보고 버린다.
    max_latency_seconds: float = 120.0


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
        self._file_counter = 0
        buffer_dir.mkdir(parents=True, exist_ok=True)

    def _new_path(self, sequence: int, ext: str) -> Path:
        """재생목록 번호가 초기화돼도 기존 파일을 덮어쓰지 않도록 고유한 파일 이름."""
        self._file_counter += 1
        return self.buffer_dir / f"{self._file_counter:08d}_{sequence}.{ext}"

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
            self.remove_files()

    def remove_files(self) -> None:
        shutil.rmtree(self.buffer_dir, ignore_errors=True)
        try:  # 카메라별 상위 폴더가 비었으면 함께 정리
            self.buffer_dir.parent.rmdir()
        except OSError:
            pass

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
        self.pdt_rejected = False
        self.pdt_skew_seconds: float | None = None

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
            and abs(seconds_between(previous.real_end, anchored[0][0])) <= receive_unc
        )
        use_pdt = self._pdt_plausible(new, fetched_at, anchored_end)

        for index, seg in enumerate(new):
            try:
                data = self.fetch(seg.uri, self.timing.request_timeout_seconds, MAX_SEGMENT_BYTES)
            except FetchError as exc:
                raise SegmentFetchError(str(exc), exc.status) from exc
            path = self._new_path(seg.sequence, "m4s" if playlist.is_fmp4 else "ts")
            path.write_bytes(data)
            if use_pdt and seg.program_date_time is not None:
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

    def _pdt_plausible(self, new: list, fetched_at: datetime, anchored_end: datetime) -> bool:
        """시각 태그가 받은 시각과 앞뒤가 맞는지 확인한다(인코더 시계 오류·시간대 누락 대비)."""
        tagged = [s for s in new if s.program_date_time is not None]
        if not tagged:
            return False
        newest_end = shift(tagged[-1].program_date_time, tagged[-1].duration)
        latency = seconds_between(newest_end, fetched_at)
        if -2.0 <= latency <= self.timing.max_latency_seconds:
            return True
        skew = seconds_between(anchored_end, newest_end)
        if not self.pdt_rejected:
            log.warning(
                "%s: 스트림 시각 태그가 실제 시각과 %.0f초 어긋나 수신 시각으로 추정합니다.",
                self.camera_id, skew,
            )  # fmt: skip
        self.pdt_rejected, self.pdt_skew_seconds = True, skew
        return False

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
            if (
                isinstance(exc, FetchError)
                and not isinstance(exc, SegmentFetchError)
                and exc.status in (401, 403, 404, 410)
            ):
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
    """HLS가 아닌 스트림(RTSP, 암호화 HLS 등)을 ffmpeg로 짧은 세그먼트로 받는다.

    세그먼트 길이는 키프레임 위치에 따라 달라지므로, ffmpeg가 세그먼트를 닫을 때마다 남기는
    목록(CSV: 파일, 스트림 시작 시각, 끝 시각)으로 길이를 정확히 알고, 실제 시각은
    "목록에 나타난 시각 - 스트림 시각"의 최솟값(가장 덜 늦게 받은 세그먼트)을 기준으로 맞춘다.
    """

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
        self.list_path = buffer_dir / "segments.csv"
        self.log_path = buffer_dir / "ffmpeg.log"
        self._rows_seen = 0
        self._epoch = 0
        self._bases: dict[int, datetime] = {}
        # (epoch, sequence, path, stream_start, stream_end, size)
        self._raw: list[tuple[int, int, Path, float, float, int]] = []

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
            "-segment_list", str(self.list_path), "-segment_list_type", "csv",
            str(self.buffer_dir / "seg_%08d.ts"),
        ]  # fmt: skip

    @staticmethod
    def available(ffmpeg: str = "ffmpeg") -> bool:
        return shutil.which(ffmpeg) is not None

    def _scan(self) -> None:
        now = self.clock()
        try:
            lines = self.list_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return
        complete = [line for line in lines if line.count(",") >= 2]
        new_rows = complete[self._rows_seen :]
        if not new_rows:
            return
        lag = self.timing.delay_seconds + 0.25
        with self._lock:
            for line in new_rows:
                name, start_text, end_text = line.rsplit(",", 2)
                try:
                    start, end = float(start_text), float(end_text)
                except ValueError:
                    continue
                path = self.buffer_dir / name
                if self._raw and start < self._raw[-1][3] - 1.0:  # 스트림 시각이 되돌아감
                    self._epoch += 1
                candidate = shift(now, -(lag + end))
                base = self._bases.get(self._epoch)
                if base is None or candidate < base:
                    self._bases[self._epoch] = candidate
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    continue
                sequence = int(path.stem.split("_")[1]) if "_" in path.stem else len(self._raw)
                self._raw.append((self._epoch, sequence, path, start, end, size))
            self._rows_seen = len(complete)
            self._rebuild()
        self.status = "recording"

    def _rebuild(self) -> None:
        unc = self.timing.delay_uncertainty_seconds + 0.5
        kept = []
        segments = []
        for epoch, sequence, path, start, end, size in self._raw:
            if not path.exists():
                continue  # prune 로 지워짐
            kept.append((epoch, sequence, path, start, end, size))
            base = self._bases[epoch]
            segments.append(
                BufferedSegment(
                    sequence, path, end - start, shift(base, start), shift(base, end),
                    "ffmpeg_segment", unc, size,
                )
            )  # fmt: skip
        self._raw = kept
        self.segments = sorted(segments, key=lambda s: (s.real_start, s.sequence))

    def _run(self) -> None:
        try:
            command = self._command()
            with self.log_path.open("wb") as log_file:
                self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log_file)
        except UnsupportedStreamError as exc:
            self.status, self.error = "error", str(exc)
            return
        except OSError as exc:
            self.status, self.error = "error", f"ffmpeg 실행 실패: {exc}"
            return
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:
                log.exception("%s: 세그먼트 확인 중 오류", self.camera_id)
            if self.process.poll() is not None:
                self._scan()
                # 실시간 스트림이 스스로 끝났다면 더는 받을 수 없으므로 오류로 본다.
                self.status = "error"
                self.error = self._last_log_line() or f"ffmpeg 종료({self.process.returncode})"
                return
            self._stop.wait(0.25)

    def _last_log_line(self) -> str | None:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        return text.splitlines()[-1][:200] if text else None

    def _on_stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        try:
            self._scan()
        except Exception:
            log.exception("%s: 마지막 세그먼트 확인 중 오류", self.camera_id)


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    """테스트·종료 처리용 대기 도우미."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
