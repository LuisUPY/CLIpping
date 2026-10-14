"""Streaming acoustic measurements; no CLI, JSON, SQLite or media writes.

FFmpeg astats computes channel-combined RMS energy and maximum sample peak.
Only scalar metadata enters Python. Decoded-frame PTS are audited BEFORE
asetnsamples so a discontinuity hidden inside a window cannot appear valid.
These measurements describe the signal, not emotions, speech or interest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import math
from pathlib import Path
import re
from time import monotonic

from clip_factory.media.ffmpeg import MediaError, run_media_command, stream_media_command
from clip_factory.media.probe import MediaInfo, _local_file, probe_media


class AudioAnalysisError(MediaError):
    """Invalid stream, timestamps, measurements or resource limits."""


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} debe ser un número finito.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} debe ser un número finito.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} debe ser un número finito.")
    return result


@dataclass(frozen=True)
class AudioAnalysisConfig:
    window_seconds: float = 0.5
    peak_dbfs: float = -6.0
    silence_dbfs: float = -50.0
    change_db: float = 6.0
    min_silence_duration: float = 0.5
    energy_floor_dbfs: float = -120.0
    timeout: float = 300.0
    audio_stream: int | None = None
    max_windows: int = 100_000
    max_events: int = 20_000

    def __post_init__(self) -> None:
        for name in ("window_seconds", "peak_dbfs", "silence_dbfs", "change_db",
                     "min_silence_duration", "energy_floor_dbfs", "timeout"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if not 0.01 <= self.window_seconds <= 10:
            raise ValueError("window_seconds debe estar entre 0.01 y 10 segundos.")
        if not -120 <= self.silence_dbfs < self.peak_dbfs <= 0:
            raise ValueError("Se requiere -120 <= silence_dbfs < peak_dbfs <= 0 dBFS.")
        if not 0 < self.change_db <= 120:
            raise ValueError("change_db debe ser mayor que cero y como máximo 120 dB.")
        if not -300 <= self.energy_floor_dbfs <= -120:
            raise ValueError("energy_floor_dbfs debe estar entre -300 y -120 dBFS.")
        if self.min_silence_duration < 0 or self.timeout <= 0:
            raise ValueError("Se requiere min_silence_duration >= 0 y timeout > 0.")
        if self.audio_stream is not None and (type(self.audio_stream) is not int or self.audio_stream < 0):
            raise ValueError("audio_stream debe ser null o un índice absoluto no negativo de FFprobe.")
        for name, maximum in (("max_windows", 1_000_000), ("max_events", 100_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} debe ser un entero entre 1 y {maximum}.")


@dataclass(frozen=True, slots=True)
class AudioWindow:
    """An interval [pts, end_pts) in time_base=1/sample_rate.

    RMS combines channel powers without downmixing. None dBFS means negative
    infinity (digital zero), while rms remains zero. Peak is sample peak, not
    an oversampled true peak. Change compares only adjacent continuous windows.
    """
    pts: int
    end_pts: int
    samples: int
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    rms: float
    rms_dbfs: float | None
    peak_dbfs: float | None
    rms_change_db: float | None


@dataclass(frozen=True, slots=True)
class AudioEvent:
    """Consecutive qualifying windows, not a semantic interpretation.

    Peak and relative changes are independent and may overlap. The exact
    position of a sample peak within a window is not inferred from statistics.
    """
    kind: str
    start_pts: int
    end_pts: int
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    window_count: int
    peak_dbfs: float | None
    max_rms_dbfs: float | None
    change_db: float | None


@dataclass(frozen=True)
class AudioAnalysisResult:
    source: str
    status: str
    stream_index: int | None
    sample_rate: int | None
    channels: int | None
    source_time_base: str | None
    time_base: str | None
    start_pts: int | None
    window_samples: int | None
    timestamp_tolerance_samples: int | None
    windows: tuple[AudioWindow, ...]
    events: tuple[AudioEvent, ...]
    samples_analyzed: int
    rms: float | None
    rms_dbfs: float | None
    peak_dbfs: float | None
    config: AudioAnalysisConfig
    threads: int
    warnings: tuple[str, ...]
    engine_version: str | None
    elapsed_seconds: float
    engine: str = "ffmpeg.astats"


def _select_stream(info: MediaInfo, index: int | None) -> dict | None:
    streams = [s for s in info.streams if s.get("codec_type") == "audio"]
    if index is not None:
        stream = next((s for s in streams if s.get("index") == index), None)
        if stream is None:
            raise AudioAnalysisError(f"La pista {index} no existe o no es audio.")
        return stream
    return next((s for s in streams if isinstance(s.get("disposition"), dict)
                 and s["disposition"].get("default")), streams[0] if streams else None)


def _dbfs(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError as exc:
        raise AudioAnalysisError(f"Nivel acústico inválido: {value}") from exc
    if number == -math.inf:
        return None
    if not math.isfinite(number) or not -1000 <= number <= 1000:
        raise AudioAnalysisError(f"Nivel acústico no finito o fuera de rango: {value}")
    return number


def _higher(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


class _AudioParser:
    """Bounded pending metadata, aggregates, retained windows and grouped events."""
    def __init__(self, sample_rate: int, window_samples: int, tolerance: int,
                 config: AudioAnalysisConfig) -> None:
        self.rate, self.window_samples, self.tolerance, self.config = sample_rate, window_samples, tolerance, config
        self.pts: int | None = None
        self.fields: dict[str, str] = {}
        self.start_pts: int | None = None
        self.decoded_samples = 0
        self.samples = 0
        self.sum_squares = 0.0
        self.peak: float | None = None
        self.windows: list[AudioWindow] = []
        self.events: list[AudioEvent] = []
        self.active: dict[str, AudioEvent] = {}
        self.active_samples: dict[str, int] = {}

    def _seconds(self, pts: int, *, relative: bool = True) -> float:
        origin = self.start_pts if relative else 0
        return float(Fraction(pts - origin, self.rate))

    def _flush_event(self, kind: str) -> None:
        event = self.active.pop(kind)
        samples = self.active_samples.pop(kind)
        if kind == "silence" and Fraction(samples, self.rate) < Fraction(str(self.config.min_silence_duration)):
            return
        if len(self.events) >= self.config.max_events:
            raise AudioAnalysisError("Se alcanzó max_events; aumenta el límite o ajusta los umbrales.")
        self.events.append(event)

    def _events_for(self, window: AudioWindow) -> None:
        kinds = set()
        if window.peak_dbfs is not None and window.peak_dbfs >= self.config.peak_dbfs:
            kinds.add("peak")
        if window.rms_dbfs is None or window.rms_dbfs <= self.config.silence_dbfs:
            kinds.add("silence")
        if window.rms_change_db is not None and abs(window.rms_change_db) >= self.config.change_db:
            kinds.add("energy_rise" if window.rms_change_db > 0 else "energy_drop")
        for kind in tuple(self.active):
            if kind not in kinds or abs(window.pts - self.active[kind].end_pts) > self.tolerance:
                self._flush_event(kind)
        for kind in sorted(kinds):
            change = window.rms_change_db if kind.startswith("energy_") else None
            if kind in self.active:
                old = self.active[kind]
                self.active_samples[kind] += window.samples
                strongest = change if change is not None and (old.change_db is None or abs(change) > abs(old.change_db)) else old.change_db
                self.active[kind] = replace(
                    old, end_pts=window.end_pts, end_seconds=window.end_seconds,
                    source_end_seconds=window.source_end_seconds, window_count=old.window_count + 1,
                    peak_dbfs=_higher(old.peak_dbfs, window.peak_dbfs),
                    max_rms_dbfs=_higher(old.max_rms_dbfs, window.rms_dbfs), change_db=strongest,
                )
            else:
                self.active_samples[kind] = window.samples
                self.active[kind] = AudioEvent(
                    kind, window.pts, window.end_pts, window.start_seconds, window.end_seconds,
                    window.source_start_seconds, window.source_end_seconds, 1,
                    window.peak_dbfs, window.rms_dbfs, change,
                )

    def _finish_record(self) -> None:
        if self.pts is None:
            return
        if "Number_of_samples" not in self.fields:
            raise AudioAnalysisError("FFmpeg no proporcionó Number_of_samples para el frame de audio.")
        try:
            count = float(self.fields["Number_of_samples"])
        except ValueError as exc:
            raise AudioAnalysisError("Número de muestras inválido.") from exc
        if not math.isfinite(count) or not count.is_integer() or count <= 0:
            raise AudioAnalysisError("Número de muestras no finito o inválido.")
        samples = int(count)
        is_window = "RMS_level" in self.fields or "Peak_level" in self.fields
        if not is_window:
            # Input audit uses key-filtered ametadata, so only this field prints.
            if self.start_pts is None:
                self.start_pts = self.pts
            expected = self.start_pts + self.decoded_samples
            if abs(self.pts - expected) > self.tolerance:
                raise AudioAnalysisError("Discontinuidad de PTS en audio (hueco o solapamiento); no se inventará silencio ni sincronización.")
            self.decoded_samples += samples
            return
        if self.start_pts is None:
            raise AudioAnalysisError("Ventana sin auditoría previa del origen temporal de audio.")
        if not {"RMS_level", "Peak_level"}.issubset(self.fields):
            raise AudioAnalysisError("FFmpeg devolvió métricas RMS/Peak incompletas.")
        if samples > self.window_samples or self.samples + samples > self.decoded_samples:
            raise AudioAnalysisError("Conteo de muestras de ventana inconsistente.")
        if abs(self.pts - (self.start_pts + self.samples)) > self.tolerance:
            raise AudioAnalysisError("PTS de ventana inconsistente con las muestras decodificadas.")
        if len(self.windows) >= self.config.max_windows:
            raise AudioAnalysisError("Se alcanzó max_windows; usa ventanas mayores o aumenta el límite.")
        if self.windows and self.windows[-1].samples != self.window_samples:
            raise AudioAnalysisError("FFmpeg devolvió una ventana parcial intermedia; sólo la última puede ser parcial.")
        rms_dbfs, peak = _dbfs(self.fields["RMS_level"]), _dbfs(self.fields["Peak_level"])
        if (rms_dbfs is None) != (peak is None) or (rms_dbfs is not None and rms_dbfs > peak + 0.00001):
            raise AudioAnalysisError("Métricas RMS y pico inconsistentes.")
        rms = 0.0 if rms_dbfs is None else 10 ** (rms_dbfs / 20)
        delta = None
        if self.windows:
            previous = self.windows[-1]
            if abs(self.pts - previous.end_pts) <= self.tolerance:
                floor = self.config.energy_floor_dbfs
                delta = max(rms_dbfs if rms_dbfs is not None else floor, floor) - max(
                    previous.rms_dbfs if previous.rms_dbfs is not None else floor, floor)
        end = self.pts + samples
        window = AudioWindow(
            self.pts, end, samples, self._seconds(self.pts), self._seconds(end),
            self._seconds(self.pts, relative=False), self._seconds(end, relative=False),
            rms, rms_dbfs, peak, delta,
        )
        self._events_for(window)
        self.windows.append(window)
        self.samples += samples
        self.sum_squares += rms * rms * samples
        self.peak = _higher(self.peak, peak)

    def feed(self, line: str) -> None:
        line = line.strip()
        if line.startswith("frame:"):
            self._finish_record()
            header = re.fullmatch(r"frame:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:\S+", line)
            if header is None:
                raise AudioAnalysisError(f"PTS de audio inválido o ausente: {line}")
            self.pts, self.fields = int(header.group(1)), {}
        elif line.startswith("lavfi.astats.Overall."):
            if self.pts is None:
                raise AudioAnalysisError("Métricas acústicas sin PTS de frame.")
            key, separator, value = line.removeprefix("lavfi.astats.Overall.").partition("=")
            if key in {"RMS_level", "Peak_level", "Number_of_samples"}:
                if not separator or key in self.fields:
                    raise AudioAnalysisError("Metadata acústica ausente o duplicada.")
                self.fields[key] = value

    def finish(self) -> None:
        self._finish_record()
        if not self.windows or self.samples != self.decoded_samples:
            raise AudioAnalysisError("FFmpeg no devolvió todas las ventanas/muestras de audio; no se asume silencio.")
        for kind in tuple(self.active):
            self._flush_event(kind)
        self.events.sort(key=lambda event: (event.start_pts, event.kind))


def analyze_audio(
    path: Path | str, *, window_seconds: float = 0.5, peak_dbfs: float = -6.0,
    silence_dbfs: float = -50.0, change_db: float = 6.0,
    min_silence_duration: float = 0.5, energy_floor_dbfs: float = -120.0,
    timeout: float = 300.0, audio_stream: int | None = None,
    max_windows: int = 100_000, max_events: int = 20_000,
    threads: int = 2, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe",
    media_info: MediaInfo | None = None,
) -> AudioAnalysisResult:
    """Measure one local audio stream, with explicit SKIPPED for absent audio.

    Window duration rounds to the nearest positive sample; the final window is
    unpadded. Original source time_base determines PTS quantization tolerance;
    larger discontinuities are rejected before windowing, never filled in.
    The deadline includes internal probe/version/decode; retention limits fail
    visibly rather than return truncated success. No samples are stored in Python.
    """
    config = AudioAnalysisConfig(
        window_seconds=window_seconds, peak_dbfs=peak_dbfs, silence_dbfs=silence_dbfs,
        change_db=change_db, min_silence_duration=min_silence_duration,
        energy_floor_dbfs=energy_floor_dbfs, timeout=timeout, audio_stream=audio_stream,
        max_windows=max_windows, max_events=max_events,
    )
    if type(threads) is not int or threads < 1:
        raise ValueError("threads debe ser un entero positivo.")
    started = monotonic()
    def remaining() -> float:
        value = config.timeout - (monotonic() - started)
        if value <= 0:
            raise AudioAnalysisError(f"Audio Analysis superó el timeout de {config.timeout:g} segundos.")
        return value
    source = _local_file(path)
    info = media_info or probe_media(source, ffprobe_bin=ffprobe_bin, timeout=remaining())
    if Path(info.path).resolve() != source:
        raise AudioAnalysisError("La metadata proporcionada pertenece a otro archivo.")
    if info.status == "FAIL":
        raise AudioAnalysisError("FFprobe informó errores; se rechaza Audio Analysis.")
    stream = _select_stream(info, config.audio_stream)
    if stream is None:
        return AudioAnalysisResult(
            source=str(source), status="SKIPPED", stream_index=None, sample_rate=None,
            channels=None, source_time_base=None, time_base=None, start_pts=None,
            window_samples=None, timestamp_tolerance_samples=None, windows=(), events=(),
            samples_analyzed=0, rms=None, rms_dbfs=None, peak_dbfs=None, config=config,
            threads=threads, warnings=("El archivo no contiene una pista de audio; análisis acústico omitido.",),
            engine_version=None, elapsed_seconds=monotonic() - started,
        )
    index, channels = stream.get("index"), stream.get("channels")
    try:
        raw_rate = stream.get("sample_rate", "")
        if type(raw_rate) is not int and not (isinstance(raw_rate, str) and re.fullmatch(r"[0-9]+", raw_rate)):
            raise ValueError
        sample_rate = int(raw_rate)
        base = Fraction(str(stream.get("time_base")))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise AudioAnalysisError("La pista no tiene sample_rate/time_base válidos.") from exc
    if type(index) is not int or index < 0 or type(channels) is not int or not 1 <= channels <= 64 or not 1 <= sample_rate <= 768000 or base <= 0:
        raise AudioAnalysisError("Índice, canales, frecuencia o base temporal de audio inválidos.")
    window_samples = max(1, round(config.window_seconds * sample_rate))
    # Bound the filter's float window buffer to 32 MiB even for unusual layouts.
    if window_samples * channels * 4 > 32 * 1024 * 1024:
        raise AudioAnalysisError("La ventana acústica supera 32 MiB; reduce window_seconds.")
    tolerance = max(1, math.ceil(base * sample_rate))
    version = run_media_command([ffmpeg_bin, "-version"], timeout=remaining())
    lines = version.stdout.splitlines()
    if not lines or not lines[0].startswith("ffmpeg version "):
        raise AudioAnalysisError("El ejecutable no devolvió una versión válida de FFmpeg.")
    parser = _AudioParser(sample_rate, window_samples, tolerance, config)
    # Print decoded-frame samples first; key filtering prevents inherited window
    # metadata from making an audit record look like a measured window.
    filters = (
        f"aformat=sample_fmts=fltp,asettb=expr=1/{sample_rate},"
        "astats=metadata=1:reset=1:length=0:measure_perchannel=none:measure_overall=Number_of_samples,"
        "ametadata=mode=print:key=lavfi.astats.Overall.Number_of_samples:file='pipe\\:1':direct=1,"
        f"asetnsamples=n={window_samples}:p=0,"
        "astats=metadata=1:reset=1:length=0:measure_perchannel=none:measure_overall=RMS_level+Peak_level+Number_of_samples,"
        "ametadata=mode=print:file='pipe\\:1':direct=1"
    )
    command = [
        ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
        "-xerror", "-copyts", "-threads", str(threads), "-filter_threads", "1",
        "-protocol_whitelist", "file,pipe", "-err_detect", "explode",
        "-i", str(source), "-map", f"0:{index}", "-vn", "-sn", "-dn",
        "-af", filters, "-c:a", "pcm_f32le", "-threads", "1", "-f", "null", "-",
    ]
    process = stream_media_command(command, on_stdout_line=parser.feed, timeout=remaining())
    if process.stderr.strip():
        raise AudioAnalysisError(
            f"FFmpeg informó errores durante Audio Analysis: {process.stderr.strip()}",
            command=command, stderr=process.stderr, returncode=process.returncode,
        )
    parser.finish()
    remaining()
    rms = math.sqrt(parser.sum_squares / parser.samples)
    warnings = list(entry["message"] for entry in info.diagnostics if entry["severity"].upper() in {"WARN", "WARNING"})
    if parser.peak is not None and parser.peak > 0:
        warnings.append("La señal supera 0 dBFS; el pico medido puede indicar sobreescala digital.")
    return AudioAnalysisResult(
        source=str(source), status="PASS", stream_index=index, sample_rate=sample_rate,
        channels=channels, source_time_base=str(base), time_base=f"1/{sample_rate}",
        start_pts=parser.start_pts, window_samples=window_samples,
        timestamp_tolerance_samples=tolerance, windows=tuple(parser.windows), events=tuple(parser.events),
        samples_analyzed=parser.samples, rms=rms, rms_dbfs=20 * math.log10(rms) if rms else None,
        peak_dbfs=parser.peak, config=config, threads=threads, warnings=tuple(warnings),
        engine_version=lines[0], elapsed_seconds=monotonic() - started,
    )
