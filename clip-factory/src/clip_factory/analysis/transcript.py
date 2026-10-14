"""Optional, offline whisper.cpp adapter with bounded PCM chunks.

One FFmpeg decoder feeds 16 kHz mono PCM. Only a <=30 second chunk is retained
in Python or written to a private working directory; inference never receives
an original. Input and resampled frame PTS are audited before joining chunks.
The module owns no CLI, logging policy or database. No downloads are performed.
"""

from __future__ import annotations

from array import array
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
from time import monotonic
from typing import Callable, Iterator, Sequence
from uuid import uuid4
import wave

from clip_factory.media.ffmpeg import DependencyError, MediaError
from clip_factory.media.probe import MediaInfo, _local_file, probe_media
from clip_factory.utils.files import secure_directory, temporary_lock

_RATE = 16000
_PIPE_LIMIT = 65536
_JSON_LIMIT = 8 * 1024 * 1024
_TEXT_LIMIT = 32 * 1024 * 1024


class TranscriptError(MediaError):
    """Invalid media, engine output, temporal mapping or execution limits."""


def _finite(value: object, name: str) -> float:
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
class TranscriptConfig:
    engine: str = "whisper.cpp"
    executable: str = "whisper-cli"
    model: str | Path | None = None
    language: str = "auto"
    timeout: float = 300.0
    audio_stream: int | None = None
    chunk_seconds: float = 30.0
    max_segments: int = 100_000
    silence_dbfs: float = -50.0

    def __post_init__(self) -> None:
        if self.engine != "whisper.cpp":
            raise ValueError("El único motor implementado es whisper.cpp.")
        executable = os.fspath(self.executable) if isinstance(self.executable, (str, Path)) else ""
        if not executable.strip() or "\x00" in executable:
            raise ValueError("executable debe identificar un ejecutable local.")
        object.__setattr__(self, "executable", executable)
        if self.model is not None:
            if not isinstance(self.model, (str, Path)) or not os.fspath(self.model).strip() or "\x00" in os.fspath(self.model):
                raise ValueError("model debe ser una ruta local no vacía o null.")
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", os.fspath(self.model)):
                raise ValueError("model debe ser una ruta local, no una URL.")
            object.__setattr__(self, "model", os.fspath(self.model))
        if not isinstance(self.language, str) or self.language not in {"auto", "es", "en"}:
            raise ValueError("language debe ser auto, es o en.")
        for name in ("timeout", "chunk_seconds", "silence_dbfs"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero.")
        if not 1 <= self.chunk_seconds <= 30:
            raise ValueError("chunk_seconds debe estar entre 1 y 30 segundos.")
        if not -120 <= self.silence_dbfs <= -20:
            raise ValueError("silence_dbfs debe estar entre -120 y -20 dBFS.")
        if self.audio_stream is not None and (type(self.audio_stream) is not int or self.audio_stream < 0):
            raise ValueError("audio_stream debe ser null o un índice absoluto no negativo.")
        if type(self.max_segments) is not int or not 1 <= self.max_segments <= 1_000_000:
            raise ValueError("max_segments debe ser un entero entre 1 y 1000000.")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float


@dataclass(frozen=True)
class TranscriptResult:
    source: str
    status: str
    skip_reason: str | None
    engine: str
    engine_version: str | None
    model: str | None
    language: str | None
    stream_index: int | None
    segments: tuple[TranscriptSegment, ...]
    warnings: tuple[str, ...]
    config: TranscriptConfig
    elapsed_seconds: float
    audio_processing_seconds: float
    inference_seconds: float
    start_pts: int | None
    time_base: str | None
    source_time_base: str | None
    source_start_pts: int | None
    source_sample_rate: int | None
    timestamp_tolerance_samples: int | None
    samples_analyzed: int
    threads: int
    sample_rate: int | None = None
    timestamp_resolution_seconds: float = 0.01


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TranscriptError("Transcription superó el timeout configurado.")
    return remaining


def _spawn(command: Sequence[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, PermissionError) as exc:
        raise DependencyError(f"No se pudo iniciar el ejecutable local {command[0]}: {exc}", command=command) from exc
    except OSError as exc:
        raise TranscriptError(f"No se pudo iniciar {command[0]}: {exc}", command=command) from exc


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()
    process.stdout.close()
    process.stderr.close()


def _run_bounded(command: Sequence[str], deadline: float,
                 guard: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
    """Drain both pipes concurrently; keep at most 64 KiB from either pipe."""
    process = _spawn(command)
    tails = {"stdout": b"", "stderr": b""}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = _remaining(deadline)
                if guard is not None:
                    guard()
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fd, _PIPE_LIMIT)
                    if block:
                        tails[key.data] = (tails[key.data] + block)[-_PIPE_LIMIT:]
                    else:
                        selector.unregister(key.fileobj)
            process.wait(timeout=_remaining(deadline))
        if guard is not None:
            guard()
        stdout, stderr = (tails[key].decode("utf-8", errors="replace") for key in ("stdout", "stderr"))
        if process.returncode:
            raise TranscriptError(f"{command[0]} terminó con código {process.returncode}: {stderr.strip()}",
                                  command=command, stderr=stderr, returncode=process.returncode)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        raise TranscriptError("Transcription superó el timeout configurado.", command=command,
                              stderr=tails["stderr"].decode("utf-8", errors="replace")) from exc
    except OSError as exc:
        raise TranscriptError(f"Error leyendo el motor: {exc}", command=command) from exc
    finally:
        _reap(process)


class _Timeline:
    """Audit source frames before resampling and output frames before chunking."""
    def __init__(self, rate: int, tolerance: int, output_tolerance: int) -> None:
        self.rate, self.tolerance, self.output_tolerance = rate, tolerance, output_tolerance
        self.source_start: int | None = None
        self.start: int | None = None
        self.source_samples = 0
        self.samples = 0
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def feed(self, line: str) -> None:
        tag = "source" if "ashowinfo@source " in line else "pcm" if "ashowinfo@pcm " in line else None
        if tag is not None:
            fields = {key: value for key, value in re.findall(r"\b(pts|rate|channels|nb_samples):([^\s]+)", line)}
            try:
                pts, rate, channels, count = (int(fields[key]) for key in ("pts", "rate", "channels", "nb_samples"))
            except (ValueError, KeyError) as exc:
                raise TranscriptError("FFmpeg no proporcionó PTS/muestras de audio válidos.") from exc
            if not 1 <= count <= 768000 * 30 or not 1 <= channels <= 64:
                raise TranscriptError("Conteo de muestras/canales de audio inválido.")
            if tag == "source":
                if rate != self.rate:
                    raise TranscriptError("La frecuencia de audio cambió durante la decodificación.")
                if self.source_start is None:
                    self.source_start = pts
                if abs(pts - (self.source_start + self.source_samples)) > self.tolerance:
                    raise TranscriptError("Discontinuidad de PTS en audio; no se inventará silencio ni sincronización.")
                self.source_samples += count
            else:
                if rate != _RATE or channels != 1:
                    raise TranscriptError("FFmpeg no produjo PCM mono de 16 kHz.")
                if self.source_start is None:
                    raise TranscriptError("PCM sin auditoría previa del origen temporal.")
                if self.start is None:
                    self.start = pts
                    expected = Fraction(self.source_start * _RATE, self.rate)
                    if abs(Fraction(pts) - expected) > self.output_tolerance:
                        raise TranscriptError("El resampling cambió el origen temporal del audio.")
                if abs(pts - (self.start + self.samples)) > self.output_tolerance:
                    raise TranscriptError("Discontinuidad de PTS en audio resampleado.")
                self.samples += count
        elif "[error]" in line or "[fatal]" in line:
            if len(self.errors) < 8:
                self.errors.append(line[-2048:])
        elif "[warning]" in line and len(self.warnings) < 8:
            self.warnings.append(line[-2048:])

    def finish(self, bytes_received: int) -> None:
        if self.errors:
            raise TranscriptError("FFmpeg informó errores durante la extracción de audio: " + "\n".join(self.errors))
        if self.start is None or self.source_start is None or not self.samples or bytes_received != self.samples * 2:
            raise TranscriptError("FFmpeg no devolvió todas las muestras/PTS de audio; no se asume silencio.")
        expected = Fraction(self.source_samples * _RATE, self.rate)
        if abs(expected - self.samples) > 2:
            raise TranscriptError("El resampling devolvió un número inconsistente de muestras.")


def _decode_chunks(command: Sequence[str], timeline: _Timeline, chunk_samples: int,
                   deadline: float, consume: Callable[[bytes, int], None]) -> None:
    """Pause decoder pipe reads during inference; OS pipes supply backpressure."""
    process = _spawn(command)
    pending = b""
    diagnostic = b""
    pcm = bytearray()
    received = consumed = 0
    chunk_bytes = chunk_samples * 2
    stdout_open = True
    stdout_registered = True
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                for key, _ in selector.select(min(_remaining(deadline), 0.1)):
                    block = os.read(key.fd, _PIPE_LIMIT)
                    if not block:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout":
                            stdout_open = stdout_registered = False
                        continue
                    if key.data == "stdout":
                        received += len(block)
                        pcm.extend(block)
                        if len(pcm) >= chunk_bytes:
                            selector.unregister(process.stdout)
                            stdout_registered = False
                    else:
                        diagnostic = (diagnostic + block)[-_PIPE_LIMIT:]
                        lines = (pending + block).split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            if len(line) > _PIPE_LIMIT:
                                raise TranscriptError("FFmpeg devolvió una línea de metadata demasiado larga.")
                            timeline.feed(line.decode("utf-8", errors="replace"))
                        if len(pending) > _PIPE_LIMIT:
                            raise TranscriptError("FFmpeg devolvió una línea de metadata demasiado larga.")
                while len(pcm) >= chunk_bytes and timeline.samples * 2 >= consumed + chunk_bytes:
                    block = bytes(pcm[:chunk_bytes])
                    del pcm[:chunk_bytes]
                    consume(block, consumed // 2)
                    consumed += chunk_bytes
                if stdout_open and not stdout_registered and len(pcm) < chunk_bytes:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    stdout_registered = True
                # EOF stderr plus unaudited PCM is malformed, not a deadlock.
                if stdout_open and not stdout_registered and not selector.get_map():
                    raise TranscriptError("PCM sin metadata temporal suficiente.")
            if pending:
                timeline.feed(pending.decode("utf-8", errors="replace"))
            process.wait(timeout=_remaining(deadline))
        stderr = diagnostic.decode("utf-8", errors="replace")
        if process.returncode:
            raise TranscriptError(f"FFmpeg terminó con código {process.returncode}: {stderr.strip()}",
                                  command=command, stderr=stderr, returncode=process.returncode)
        timeline.finish(received)
        if pcm:
            consume(bytes(pcm), consumed // 2)
    except subprocess.TimeoutExpired as exc:
        raise TranscriptError("Transcription superó el timeout configurado.", command=command,
                              stderr=diagnostic.decode("utf-8", errors="replace")) from exc
    except OSError as exc:
        raise TranscriptError(f"Error decodificando audio: {exc}", command=command) from exc
    finally:
        _reap(process)


@contextmanager
def _chunk_workspace(root: Path, input_dir: Path, working_dir: Path) -> Iterator[Path]:
    """Create an exclusive private directory through existing anchored guards."""
    name = ".transcript-" + uuid4().hex
    with temporary_lock(root, input_dir, blocking=True), secure_directory(
            working_dir, root=root, input_dir=input_dir, create=True) as parent:
        os.mkdir(name, 0o700, dir_fd=parent.fd)
        identity = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    path = working_dir / name
    try:
        with secure_directory(path, root=root, input_dir=input_dir):
            yield path
    finally:
        # Only these two exclusively owned files may be removed; never recurse
        # or follow a replacement directory or symlink during cleanup.
        failed = sys.exc_info()[0] is not None
        try:
            with temporary_lock(root, input_dir, blocking=True), secure_directory(
                    working_dir, root=root, input_dir=input_dir) as parent:
                current = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino) or not stat.S_ISDIR(current.st_mode):
                    raise TranscriptError("El directorio temporal cambió; no se eliminará.")
                with secure_directory(path, root=root, input_dir=input_dir) as temporary:
                    for leaf in ("chunk.wav", "result.json"):
                        expected = temporary.stat(leaf)
                        if expected is not None:
                            temporary.unlink(leaf, expected=expected)
                os.rmdir(name, dir_fd=parent.fd)
        except (OSError, ValueError, TranscriptError) as exc:
            if not failed:
                raise
            print(f"Error adicional al retirar temporal de transcripción: {exc}", file=sys.stderr)


def _pcm_values(pcm: bytes) -> array:
    if len(pcm) % 2:
        raise TranscriptError("PCM incompleto; falta media muestra.")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _quiet(samples: Sequence[int], threshold_dbfs: float) -> bool:
    """Gate on sample peak so short speech is not diluted by a long silent chunk."""
    limit = 32768 * 10 ** (threshold_dbfs / 20)
    return not samples or max(abs(sample) for sample in samples) <= limit


def _clean_text(text: str) -> str:
    text = re.sub(r"<\|[^>]*\|>|\[_[A-Z0-9_]+_\]", "", text).strip()
    noise = text.strip("[]() ").casefold().replace("_", " ")
    wrapped = len(text) >= 2 and (text[0], text[-1]) in {("[", "]"), ("(", ")")}
    if wrapped and noise in {"blank audio", "silence", "silencio", "music", "musica", "música", "noise", "ruido",
                 "laughter", "risas", "applause", "no speech", "inaudible", "unintelligible", "ininteligible"}:
        return ""
    return " ".join(text.split())


def _engine_reported_error(stderr: str) -> bool:
    """Recognize diagnostic prefixes, never words embedded in file paths."""
    return any(re.match(
        r"^(?:(?:\[)?(?:error|fatal)(?:\])?\s*[:\s]|failed\s+to\b|unknown\s+(?:argument|option)\b|"
        r"(?:whisper_\w+|read_audio_data|main)\s*:\s*(?:error|failed|cannot|could not)\b)",
        line.strip(), re.I,
    ) for line in stderr.splitlines())


def _parse_output(payload: object, *, pcm: bytes, offset_samples: int, origin: int,
                  config: TranscriptConfig) -> tuple[list[TranscriptSegment], str | None, list[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
        raise TranscriptError("whisper.cpp no devolvió un JSON de transcripción válido.")
    result = payload.get("result")
    language = result.get("language") if isinstance(result, dict) else None
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)):
        raise TranscriptError("whisper.cpp devolvió un idioma inválido.")
    if language is not None and config.language != "auto" and language != config.language:
        raise TranscriptError("whisper.cpp informó un idioma distinto al solicitado.")
    warnings: list[str] = []
    model_metadata = payload.get("model")
    multilingual = model_metadata.get("multilingual") if isinstance(model_metadata, dict) else None
    if multilingual is not None and type(multilingual) is not bool:
        raise TranscriptError("whisper.cpp devolvió metadata multilingual inválida.")
    if multilingual is False and config.language != "en":
        raise TranscriptError("El modelo instalado sólo admite inglés; configura language=en o usa uno multilingüe.")
    filename_english = config.model is not None and re.search(r"\.en(?:-[\w]+)?\.bin$", os.fspath(config.model))
    eot = 50256 if multilingual is False or (multilingual is None and filename_english) else 50257
    values = _pcm_values(pcm)
    duration_ms = len(values) * 1000 / _RATE
    segments: list[TranscriptSegment] = []
    previous_end = 0.0
    for item in payload["transcription"]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise TranscriptError("whisper.cpp devolvió un segmento sin texto válido.")
        text = _clean_text(item["text"])
        if not text:
            continue
        offsets = item.get("offsets")
        if not isinstance(offsets, dict):
            raise TranscriptError("whisper.cpp devolvió un segmento sin offsets en milisegundos.")
        start, end = offsets.get("from"), offsets.get("to")
        if type(start) is not int or type(end) is not int or start < 0 or start >= end:
            raise TranscriptError("whisper.cpp devolvió timestamps inválidos.")
        if end > duration_ms + 50 or start > duration_ms or start < previous_end - 20:
            raise TranscriptError("whisper.cpp devolvió timestamps fuera del chunk o solapados.")
        if end > duration_ms or start < previous_end:
            warnings.append("Timestamps del motor ajustados <=50 ms al límite del audio/chunk.")
            end, start = min(end, duration_ms), max(start, previous_end)
        previous_end = end
        first = max(0, int(start * _RATE / 1000))
        last = min(len(values), math.ceil(end * _RATE / 1000))
        if first >= last or _quiet(values[first:last], config.silence_dbfs):
            warnings.append("Se descartó texto situado sobre audio silencioso.")
            continue
        tokens = item.get("tokens", [])
        if not isinstance(tokens, list):
            raise TranscriptError("whisper.cpp devolvió tokens inválidos.")
        probabilities = []
        missing_confidence = False
        for token in tokens:
            if not isinstance(token, dict):
                raise TranscriptError("whisper.cpp devolvió un token inválido.")
            identifier = token.get("id")
            if type(identifier) is not int or identifier < 0:
                raise TranscriptError("whisper.cpp devolvió un ID de token inválido.")
            if identifier >= eot:
                continue
            if "p" in token:
                probability = token["p"]
                if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise TranscriptError("whisper.cpp devolvió una probabilidad inválida.")
                probabilities.append(math.log(max(probability, 1e-30)))
            else:
                missing_confidence = True
        if not probabilities or missing_confidence:
            warnings.append("Se descartó texto sin tokens léxicos/probabilidades verificables.")
            continue
        if sum(probabilities) / len(probabilities) < -1.0:
            warnings.append("Se descartó un segmento con baja confianza de tokens; no se reconstruyó texto.")
            continue
        base = Fraction(offset_samples, _RATE)
        absolute = Fraction(origin + offset_samples, _RATE)
        segments.append(TranscriptSegment(text, float(base + Fraction(str(start)) / 1000),
                                         float(base + Fraction(str(end)) / 1000),
                                         float(absolute + Fraction(str(start)) / 1000),
                                         float(absolute + Fraction(str(end)) / 1000)))
        if len(segments) > config.max_segments:
            raise TranscriptError("Se alcanzó max_segments; aumenta el límite o reduce el medio.")
    if segments and language is None:
        if config.language == "auto":
            raise TranscriptError("whisper.cpp omitió el idioma detectado en modo auto.")
        language = config.language
    return segments, language if segments else None, warnings


def transcribe_media(
    path: Path | str, *, config: TranscriptConfig | None = None,
    media_info: MediaInfo | None = None, ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe", threads: int = 4,
    root: Path, input_dir: Path, working_dir: Path,
) -> TranscriptResult:
    """Transcribe local audio, or explicitly skip absent audio/engine/model.

    Silence gating and confidence/special-token filters reduce hallucinations;
    they are not a speech classifier. Chunk boundaries can split words. Model
    weights are loaded separately per chunk to bound memory without a server.
    All probe, decode, compatibility and inference work shares one deadline.
    """
    config = config or TranscriptConfig()
    if not isinstance(config, TranscriptConfig):
        raise ValueError("config debe ser TranscriptConfig.")
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError("threads debe ser un entero entre 1 y 64.")
    started = monotonic()
    deadline = started + config.timeout
    source = _local_file(path)
    info = media_info or probe_media(source, ffprobe_bin=ffprobe_bin, timeout=_remaining(deadline))
    if Path(info.path).resolve() != source or info.status == "FAIL":
        raise TranscriptError("La metadata pertenece a otro archivo o FFprobe informó errores.")
    streams = [stream for stream in info.streams if stream.get("codec_type") == "audio"]
    if config.audio_stream is not None:
        stream = next((item for item in streams if item.get("index") == config.audio_stream), None)
        if stream is None:
            raise TranscriptError(f"La pista {config.audio_stream} no existe o no es audio.")
    else:
        stream = next((item for item in streams if isinstance(item.get("disposition"), dict)
                       and item["disposition"].get("default")), streams[0] if streams else None)
    warnings = [entry["message"] for entry in info.diagnostics if entry["severity"].upper() in {"WARN", "WARNING"}]
    model: Path | None = None
    engine_version = None

    def skipped(reason: str, warning: str) -> TranscriptResult:
        return TranscriptResult(
            source=str(source), status="SKIPPED", skip_reason=reason, engine=config.engine,
            engine_version=engine_version, model=str(model) if model else config.model,
            language=None, stream_index=stream.get("index") if stream else None, segments=(),
            warnings=tuple(warnings + [warning]), config=config, elapsed_seconds=monotonic() - started,
            audio_processing_seconds=0.0, inference_seconds=0.0, start_pts=None, time_base=None,
            source_time_base=None, source_start_pts=None, source_sample_rate=None,
            timestamp_tolerance_samples=None, samples_analyzed=0, threads=threads,
        )

    if stream is None:
        return skipped("no_audio", "El archivo no contiene una pista de audio; transcripción omitida.")
    executable = shutil.which(config.executable)
    if executable is None:
        return skipped("engine_unavailable", "whisper.cpp no está instalado o no es ejecutable; transcripción omitida.")
    if config.model is None:
        return skipped("model_unavailable", "No se configuró un modelo local; transcripción omitida sin descargar.")
    try:
        model = Path(config.model).expanduser().resolve(strict=True)
        if not model.is_file() or model.stat().st_size == 0:
            return skipped("model_unavailable", "El modelo local no existe, está vacío o no es un archivo regular; transcripción omitida.")
        with model.open("rb") as handle:
            handle.read(1)
    except (OSError, RuntimeError, ValueError):
        return skipped("model_unavailable", "No se puede leer el modelo local; transcripción omitida sin descargar.")
    if re.search(r"\.en(?:-[\w]+)?\.bin$", model.name) and config.language != "en":
        raise TranscriptError("Un modelo .en sólo admite language=en; usa un modelo multilingüe para español/auto.")
    help_result = _run_bounded([executable, "--help"], deadline)
    help_text = help_result.stdout + help_result.stderr
    required = ("--model", "--output-json-full", "--language", "--no-gpu", "--no-fallback", "--suppress-nst")
    if any(flag not in help_text for flag in required) or re.search(r"unknown (argument|option)", help_text, re.I):
        raise TranscriptError("El ejecutable no ofrece el contrato requerido de whisper-cli (--output-json-full/CPU/filtros).")
    version_match = re.search(r"\bwhisper(?:\.cpp|-cli)?\s+(?:version\s+|v)(\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?)", help_text, re.I)
    if version_match:
        engine_version = version_match.group(0)
    else:
        warnings.append("El ejecutable no publica su versión en --help; engine_version es null.")
    raw_rate, channels, index = stream.get("sample_rate"), stream.get("channels"), stream.get("index")
    try:
        if type(raw_rate) is not int and not (isinstance(raw_rate, str) and re.fullmatch(r"[0-9]+", raw_rate)):
            raise ValueError
        rate = int(raw_rate)
        base = Fraction(str(stream.get("time_base")))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise TranscriptError("La pista no tiene sample_rate/time_base válidos.") from exc
    if not 1 <= rate <= 768000 or base <= 0 or type(channels) is not int or not 1 <= channels <= 64 or type(index) is not int or index < 0:
        raise TranscriptError("Índice, canales, frecuencia o base temporal de audio inválidos.")
    timeline = _Timeline(rate, max(1, math.ceil(base * rate)), max(1, math.ceil(base * _RATE)))
    segments: list[TranscriptSegment] = []
    languages: set[str] = set()
    inference_seconds = audio_seconds = 0.0
    chunks_inferred = 0
    text_bytes = 0
    warnings.append("ASR local: ruido/música pueden producir texto erróneo; filtros de silencio/confianza no garantizan ausencia de alucinaciones. La confianza de tokens es un proxy no calibrado de habla.")
    if channels > 1:
        warnings.append("Transcripción usa downmix mono; canales en antifase pueden cancelar voz.")

    def append_warning(message: str) -> None:
        if message not in warnings and len(warnings) < 64:
            warnings.append(message)

    with _chunk_workspace(Path(root), Path(input_dir), Path(working_dir)) as temporary_path:
        def consume(pcm: bytes, offset: int) -> None:
            nonlocal inference_seconds, audio_seconds, chunks_inferred, text_bytes
            _remaining(deadline)
            began = monotonic()
            values = _pcm_values(pcm)
            if _quiet(values, config.silence_dbfs):
                audio_seconds += monotonic() - began
                return
            with secure_directory(temporary_path, root=root, input_dir=input_dir) as temporary:
                wav_fd = temporary.open("chunk.wav", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(wav_fd, "wb") as handle:
                    with wave.open(handle, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(_RATE)
                        wav.writeframes(pcm)
                json_fd = temporary.open("result.json", os.O_RDWR | os.O_CREAT | os.O_EXCL)
                output_identity = os.fstat(json_fd)
                audio_seconds += monotonic() - began
                command = [executable, "-m", str(model), "-f", str(temporary_path / "chunk.wav"),
                           "-l", config.language, "-t", str(threads), "-ojf", "-of", str(temporary_path / "result"),
                           "--no-gpu", "--no-fallback", "--suppress-nst"]
                def guard() -> None:
                    temporary.check_identity()
                    current = temporary.stat("result.json")
                    if current is None or (current.st_dev, current.st_ino) != (output_identity.st_dev, output_identity.st_ino):
                        raise TranscriptError("El motor reemplazó el archivo JSON temporal.")
                    if current.st_size > _JSON_LIMIT:
                        raise TranscriptError("La salida JSON de un chunk supera 8 MiB.")
                began = monotonic()
                try:
                    result = _run_bounded(command, deadline, guard)
                    inference_seconds += monotonic() - began
                    chunks_inferred += 1
                    guard()
                    with os.fdopen(os.dup(json_fd), "r", encoding="utf-8") as handle:
                        try:
                            payload = json.load(handle)
                        except (ValueError, UnicodeError, RecursionError) as exc:
                            raise TranscriptError("El motor terminó sin JSON válido; no se asumirá una transcripción vacía.",
                                                  command=command, stderr=result.stderr) from exc
                    if isinstance(payload, dict) and isinstance(payload.get("params"), dict) and payload["params"].get("model"):
                        reported = payload["params"]["model"]
                        if not isinstance(reported, str) or Path(reported).expanduser().resolve() != model:
                            raise TranscriptError("El motor informó un modelo distinto al solicitado.")
                    parsed, language, notes = _parse_output(payload, pcm=pcm, offset_samples=offset,
                                                             origin=timeline.start, config=config)
                    if len(segments) + len(parsed) > config.max_segments:
                        raise TranscriptError("Se alcanzó max_segments; aumenta el límite o reduce el medio.")
                    incoming_bytes = sum(len(segment.text.encode("utf-8")) for segment in parsed)
                    if text_bytes + incoming_bytes > _TEXT_LIMIT:
                        raise TranscriptError("El texto transcrito acumulado supera el límite de 32 MiB UTF-8; reduce el medio.")
                    text_bytes += incoming_bytes
                    segments.extend(parsed)
                    if language:
                        languages.add(language)
                    for note in notes:
                        append_warning(note)
                    if _engine_reported_error(result.stderr):
                        raise TranscriptError("El motor informó un error pese a devolver salida 0.", command=command, stderr=result.stderr)
                finally:
                    os.close(json_fd)
                    failed = sys.exc_info()[0] is not None
                    try:
                        for leaf in ("chunk.wav", "result.json"):
                            expected = temporary.stat(leaf)
                            if expected is not None:
                                temporary.unlink(leaf, expected=expected)
                    except (OSError, ValueError) as exc:
                        if not failed:
                            raise
                        print(f"Error adicional al retirar chunk de transcripción: {exc}", file=sys.stderr)
        filters = (f"asettb=expr=1/{rate},ashowinfo@source,"
                   "aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000,"
                   "asettb=expr=1/16000,ashowinfo@pcm")
        command = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "level+info", "-nostats", "-xerror", "-copyts",
                   "-threads", str(threads), "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
                   "-err_detect", "explode", "-i", str(source), "-map", f"0:{index}", "-vn", "-sn", "-dn",
                   "-af", filters, "-c:a", "pcm_s16le", "-threads", "1", "-f", "s16le", "pipe:1"]
        decode_began = monotonic()
        _decode_chunks(command, timeline, round(config.chunk_seconds * _RATE), deadline, consume)
        # Wall time draining/decoding plus chunk preparation, excluding serial
        # inference; OS decoder may run ahead within bounded pipe capacity.
        audio_seconds = max(audio_seconds, monotonic() - decode_began - inference_seconds)
    _remaining(deadline)
    for note in timeline.warnings:
        append_warning("FFmpeg: " + note)
    if len(languages) > 1:
        append_warning("Se detectaron varios idiomas entre chunks; language es null y los segmentos conservan el texto original.")
    if not segments:
        append_warning("No se obtuvo voz reconocible; no se generaron segmentos.")
    if chunks_inferred > 1:
        append_warning("La inferencia por chunks independientes puede dividir palabras en sus límites y recarga el modelo por chunk.")
    return TranscriptResult(
        source=str(source), status="PASS", skip_reason=None, engine=config.engine, engine_version=engine_version,
        model=str(model), language=next(iter(languages)) if len(languages) == 1 else None, stream_index=index,
        segments=tuple(segments), warnings=tuple(warnings), config=config, elapsed_seconds=monotonic() - started,
        audio_processing_seconds=audio_seconds, inference_seconds=inference_seconds, start_pts=timeline.start,
        time_base=f"1/{_RATE}", source_time_base=f"{base.numerator}/{base.denominator}",
        source_start_pts=timeline.source_start, source_sample_rate=rate,
        timestamp_tolerance_samples=timeline.output_tolerance, samples_analyzed=timeline.samples,
        sample_rate=_RATE, threads=threads,
    )
