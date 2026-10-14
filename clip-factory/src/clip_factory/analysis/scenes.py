"""Local visual-cut detection. No CLI, JSON, persistence or media writes.

FFmpeg decodes one stream and runs scdet at native resolution. Python receives
only frame metadata. PTS and rational time_base are authoritative; neither
average FPS nor scdet's rounded time string is used for timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import re
from time import monotonic

from clip_factory.media.ffmpeg import MediaError, run_media_command, stream_media_command
from clip_factory.media.probe import MediaInfo, _local_file, probe_media


class SceneDetectionError(MediaError):
    """Invalid input, timestamps or detector output; never a successful empty run."""


@dataclass(frozen=True)
class SceneChange:
    """First frame of a detected cut, with its original integer PTS.

    timestamp_seconds is relative to the first decoded frame of this stream.
    source_timestamp_seconds retains the source's offset (possibly negative).
    score is scdet's percentage of maximum visual change, or None if absent.
    """

    pts: int
    timestamp_seconds: float
    source_timestamp_seconds: float
    score: float | None


@dataclass(frozen=True)
class SceneDetectionResult:
    source: str
    stream_index: int
    time_base: str
    start_pts: int
    changes: tuple[SceneChange, ...]
    frames_analyzed: int
    threshold: float
    min_scene_duration: float
    timeout: float
    threads: int
    engine_version: str
    elapsed_seconds: float
    engine: str = "ffmpeg.scdet"


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} debe ser un número finito.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} debe ser un número finito.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} debe ser un número finito.")
    return number


def _select_stream(info: MediaInfo, index: int | None) -> tuple[int, Fraction]:
    videos = [s for s in info.streams if s.get("codec_type") == "video"
              and not (isinstance(s.get("disposition"), dict)
                       and s["disposition"].get("attached_pic"))]
    if not videos:
        raise SceneDetectionError("El archivo no contiene una pista de video reproducible.")
    if index is None:
        stream = next((s for s in videos if isinstance(s.get("disposition"), dict)
                       and s["disposition"].get("default")), videos[0])
    else:
        stream = next((s for s in videos if s.get("index") == index), None)
        if stream is None:
            raise SceneDetectionError(f"La pista {index} no existe o no es video reproducible.")
    selected = stream.get("index")
    if type(selected) is not int or selected < 0:
        raise SceneDetectionError("FFprobe no proporcionó un índice de stream válido.")
    try:
        time_base = Fraction(str(stream.get("time_base")))
        if time_base <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError) as exc:
        raise SceneDetectionError("La pista no tiene una base de tiempo válida; no se inferirá del FPS.") from exc
    return selected, time_base


class _SceneParser:
    """Keep only one frame's scalar metadata plus accepted cuts."""

    def __init__(self, time_base: Fraction, min_scene_duration: float) -> None:
        self.time_base = time_base
        self.minimum = Fraction(str(min_scene_duration))
        self.start_pts: int | None = None
        self.pts: int | None = None
        self.score: float | None = None
        self.is_cut = False
        self.frames = 0
        self.changes: list[SceneChange] = []
        self.saw_scd = False

    def _finish_frame(self) -> None:
        if self.is_cut and self.pts is not None and self.start_pts is not None:
            # The first frame has no predecessor and cannot represent a cut.
            previous = self.changes[-1].pts if self.changes else self.start_pts
            if self.pts > previous and (self.pts - previous) * self.time_base >= self.minimum:
                self.changes.append(SceneChange(
                    pts=self.pts,
                    timestamp_seconds=float((self.pts - self.start_pts) * self.time_base),
                    source_timestamp_seconds=float(self.pts * self.time_base),
                    score=self.score,
                ))

    def feed(self, line: str) -> None:
        line = line.strip()
        if line.startswith("frame:"):
            self._finish_frame()
            header = re.fullmatch(r"frame:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:\S+", line)
            if header is None:
                raise SceneDetectionError(f"PTS de frame inválido o ausente: {line}")
            pts = int(header.group(1))
            if self.pts is not None and pts < self.pts:
                raise SceneDetectionError("La pista contiene PTS no monótonos; análisis temporal rechazado.")
            self.pts, self.score, self.is_cut = pts, None, False
            if self.start_pts is None:
                self.start_pts = pts
            self.frames += 1
        elif line.startswith("lavfi.scd."):
            if self.pts is None:
                raise SceneDetectionError("Metadata de escena sin PTS de frame.")
            self.saw_scd = True
            key, separator, value = line.partition("=")
            if key in {"lavfi.scd.score", "lavfi.scd.time"}:
                try:
                    numeric = float(value) if separator else math.nan
                except ValueError as exc:
                    raise SceneDetectionError(f"Metadata de escena inválida: {line}") from exc
                if not math.isfinite(numeric):
                    raise SceneDetectionError(f"Metadata de escena no finita: {line}")
                if key == "lavfi.scd.score":
                    if not 0 <= numeric <= 100:
                        raise SceneDetectionError(f"Score fuera de rango: {line}")
                    self.score = numeric
                else:
                    # Marker only: scdet time is rounded, PTS above is exact.
                    self.is_cut = True

    def finish(self) -> None:
        self._finish_frame()
        if self.start_pts is None or not self.saw_scd:
            raise SceneDetectionError("FFmpeg no devolvió frames con metadata scdet; no se asume ausencia de cortes.")


def detect_scenes(
    path: Path | str,
    *,
    threshold: float = 10.0,
    min_scene_duration: float = 0.0,
    timeout: float = 300.0,
    video_stream: int | None = None,
    threads: int = 2,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    media_info: MediaInfo | None = None,
) -> SceneDetectionResult:
    """Detect visual changes, returning an immutable result or a visible error.

    Lower threshold (0..100) means greater sensitivity. min_scene_duration is
    the minimum interval from the first frame/previous accepted cut, not a
    guarantee about the final scene's length. video_stream is the absolute
    ffprobe stream index; default video is preferred, excluding cover art.
    timeout covers detector probe (when needed), version lookup and decoding.
    Supplying the caller's MediaInfo avoids a duplicate probe of the same file.
    No scaling, frame sampling, CFR conversion or hardware decoding is applied.
    """
    threshold = _finite(threshold, "threshold")
    minimum = _finite(min_scene_duration, "min_scene_duration")
    timeout = _finite(timeout, "timeout")
    if not 0 <= threshold <= 100 or minimum < 0 or timeout <= 0:
        raise ValueError("Se requiere threshold entre 0 y 100, min_scene_duration >= 0 y timeout > 0.")
    if video_stream is not None and (type(video_stream) is not int or video_stream < 0):
        raise ValueError("video_stream debe ser un índice entero no negativo de FFprobe.")
    if type(threads) is not int or threads < 1:
        raise ValueError("threads debe ser un entero positivo.")
    started = monotonic()

    def remaining() -> float:
        value = timeout - (monotonic() - started)
        if value <= 0:
            raise SceneDetectionError(f"Scene Detection superó el timeout de {timeout:g} segundos.")
        return value

    source = _local_file(path)
    info = media_info or probe_media(source, ffprobe_bin=ffprobe_bin, timeout=remaining())
    if Path(info.path).resolve() != source:
        raise SceneDetectionError("La metadata proporcionada pertenece a otro archivo.")
    if info.status == "FAIL":
        raise SceneDetectionError("FFprobe informó errores; se rechaza el análisis de escenas.")
    index, time_base = _select_stream(info, video_stream)
    version = run_media_command([ffmpeg_bin, "-version"], timeout=remaining())
    version_lines = version.stdout.splitlines()
    if not version_lines or not version_lines[0].startswith("ffmpeg version "):
        raise SceneDetectionError("El ejecutable no devolvió una versión válida de FFmpeg.")
    parser = _SceneParser(time_base, minimum)
    base = f"{time_base.numerator}/{time_base.denominator}"
    command = [
        ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
        "-xerror", "-copyts", "-threads", str(threads), "-filter_threads", "1",
        "-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-noautorotate",
        "-i", str(source), "-map", f"0:{index}", "-an", "-sn", "-dn",
        "-vf", f"settb=expr={base},scdet=threshold={threshold!r}:sc_pass=0,metadata=mode=print:file='pipe\\:1':direct=1",
        "-fps_mode", "passthrough", "-threads", "1", "-f", "null", "-",
    ]
    result = stream_media_command(command, on_stdout_line=parser.feed, timeout=remaining())
    # At loglevel=error, successful stderr still means a decode/filter error.
    if result.stderr.strip():
        raise SceneDetectionError(
            f"FFmpeg informó errores durante Scene Detection: {result.stderr.strip()}",
            command=command, stderr=result.stderr, returncode=result.returncode,
        )
    parser.finish()
    return SceneDetectionResult(
        source=str(source), stream_index=index, time_base=base,
        start_pts=parser.start_pts, changes=tuple(parser.changes),
        frames_analyzed=parser.frames, threshold=threshold, min_scene_duration=minimum,
        timeout=timeout, threads=threads, engine_version=version_lines[0],
        elapsed_seconds=monotonic() - started,
    )
