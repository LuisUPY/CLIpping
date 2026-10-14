"""CLI delgada: configuración, diagnóstico y análisis local de medios."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import re
import sys
from typing import Sequence
from uuid import UUID, uuid4

from clip_factory import __version__
from clip_factory.analysis.audio import AudioAnalysisConfig, AudioAnalysisResult, analyze_audio
from clip_factory.analysis.scenes import SceneDetectionResult, detect_scenes
from clip_factory.analysis.moments import MomentDetectionConfig, MomentDetectionResult, detect_moments
from clip_factory.analysis.transcript import TranscriptConfig, TranscriptResult, transcribe_media
from clip_factory.config.loader import ConfigError, load_config
from clip_factory.media.ffmpeg import MediaError, doctor
from clip_factory.media.probe import probe_media
from clip_factory.media.validation import validate_metadata
from clip_factory.utils.files import is_within, read_json_safe, recover_json_links, safe_destination, secure_directory, unlink_safe, workspace_lock, write_json_new
from clip_factory.utils.logging import close_logging, configure_logging, operation

try:
    import sqlite3
except ImportError:
    # Doctor debe poder diagnosticar también intérpretes sin _sqlite3.
    sqlite3 = None

_DATABASE_ERROR = sqlite3.Error if sqlite3 is not None else MediaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipping", description="CLIpping — análisis local de metadata, escenas, audio, voz opcional y candidatos")
    parser.add_argument("--version", action="version", version=f"CLIpping {__version__}")
    parser.add_argument("--root", type=Path, help="Directorio del proyecto (también CLIP_FACTORY_ROOT)")
    parser.add_argument("--config", type=Path, help="YAML adicional, relativo al root o absoluto")
    parser.add_argument("--profile", help="Perfil de configuración, por ejemplo mac_m1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crear carpetas y esquema SQLite, sin instalar dependencias")
    diagnostic = commands.add_parser("doctor", help="Comprobar herramientas locales y compatibilidad")
    diagnostic.add_argument("--json", action="store_true", help="Diagnóstico estructurado")
    commands.add_parser("config", help="Mostrar configuración efectiva en JSON")
    commands.add_parser("recover", help="Recuperar publicaciones JSON/SQLite interrumpidas")
    for name in ("probe", "analyze", "detect"):
        command = commands.add_parser(name, help="Leer metadata" if name == "probe" else
                                      "Detectar candidatos (alias de analyze --moments)" if name == "detect" else
                                      "Analizar escenas, audio, voz opcional y candidatos (--moments)")
        command.add_argument("video", type=Path, help="Archivo local, incluyendo rutas externas a input")
        command.add_argument("--output", help="Nuevo JSON dentro del proyecto; '-' imprime en stdout")
        if name != "probe":
            command.add_argument("--scenes", action="store_true", help="Ejecutar Scene Detection local mediante FFmpeg scdet")
            command.add_argument("--scene-threshold", type=float, help="Umbral scdet 0..100; menor = más sensible (YAML: 10)")
            command.add_argument("--scene-timeout", type=float, help="Timeout de escenas en segundos, separado del probe (YAML: 300)")
            command.add_argument("--video-stream", type=int, help="Índice absoluto de la pista de video de FFprobe")
            command.add_argument("--min-scene-duration", type=float, help="Intervalo mínimo desde inicio/corte aceptado en segundos (YAML: 0)")
            command.add_argument("--audio", action="store_true", help="Medir intensidad, picos, cambios de energía y silencios con FFmpeg astats")
            command.add_argument("--audio-window", type=float, help="Duración de ventanas acústicas, 0.01..10 segundos (YAML: 0.5)")
            command.add_argument("--audio-peak-threshold", type=float, help="Umbral de pico de muestra en dBFS, -120..0 (YAML: -6)")
            command.add_argument("--audio-silence-threshold", type=float, help="Umbral RMS de silencio en dBFS, menor al umbral de pico (YAML: -50)")
            command.add_argument("--audio-change-threshold", type=float, help="Cambio absoluto de RMS entre ventanas en dB, >0..120 (YAML: 6)")
            command.add_argument("--audio-min-silence", type=float, help="Duración mínima de un intervalo de silencio en segundos (YAML: 0.5)")
            command.add_argument("--audio-timeout", type=float, help="Timeout de audio en segundos, separado de metadata y escenas (YAML: 300)")
            command.add_argument("--audio-stream", type=int, help="Índice absoluto de la pista de audio de FFprobe")
            command.add_argument("--audio-max-windows", type=int, help="Máximo de ventanas retenidas, 1..1000000 (YAML: 100000)")
            command.add_argument("--audio-max-events", type=int, help="Máximo de eventos acústicos retenidos, 1..100000 (YAML: 20000)")
            command.add_argument("--audio-energy-floor", type=float, help="Piso para cambios relativos de energía en dBFS, -300..-120 (YAML: -120)")
            command.add_argument("--transcribe", action="store_true", help="Transcribir voz local con un motor y modelo instalados; sin descargas")
            command.add_argument("--transcript-engine", help="Motor local (implementado: whisper.cpp)")
            command.add_argument("--transcript-executable", help="Ejecutable whisper-cli en PATH o ruta local")
            command.add_argument("--transcript-model", help="Modelo GGML local; ruta absoluta o relativa a la raíz del proyecto")
            command.add_argument("--transcript-language", help="Idioma: auto, es o en (YAML: auto)")
            command.add_argument("--transcript-timeout", type=float, help="Timeout total de transcripción en segundos (YAML: 300)")
            command.add_argument("--transcript-audio-stream", type=int, help="Índice absoluto FFprobe de audio para voz; independiente de --audio-stream")
            command.add_argument("--transcript-chunk-seconds", type=float, help="Duración del bloque de audio de transcripción (YAML: 30)")
            command.add_argument("--transcript-max-segments", type=int, help="Límite de segmentos retenidos (YAML: 100000)")
            command.add_argument("--transcript-silence-threshold", type=float, help="Umbral de silencio por pico de muestras en dBFS (YAML: -50)")
            command.add_argument("--moments", action="store_true", default=name == "detect",
                                 help="Combinar escenas y audio una vez; voz sólo con --transcribe")
            for flag, description, value_type in (
                ("min-duration", "Duración mínima del candidato, segundos (YAML: 5)", float),
                ("max-duration", "Duración máxima del candidato, segundos (YAML: 45)", float),
                ("context-before", "Contexto anterior, segundos (YAML: 2)", float),
                ("context-after", "Contexto posterior, segundos (YAML: 3)", float),
                ("coincidence-window", "Proximidad temporal entre fuentes, segundos (YAML: 1)", float),
                ("merge-gap", "Separación máxima para fusionar, segundos (YAML: 1)", float),
                ("cooldown", "Separación mínima de candidatos finales, segundos (YAML: 10)", float),
                ("min-sources", "Familias distintas requeridas: 2 o 3 (YAML: 2)", int),
                ("min-speech-duration", "Duración mínima de un segmento de voz, segundos (YAML: 2)", float),
                ("min-speech-words", "Palabras mínimas por segmento de voz (YAML: 6)", int),
                ("max-audio-event-duration", "Duración máxima de transitorios acústicos, segundos (YAML: 4)", float),
                ("max-candidates", "Máximo de candidatos finales (YAML: 20)", int),
                ("max-events", "Límite de señales de entrada (YAML: 100000)", int),
                ("timeout", "Timeout sólo de reglas, segundos (YAML: 10)", float),
            ):
                command.add_argument("--moment-" + flag, type=value_type, help=description)
    for name in ("clip", "process", "qc"):
        command = commands.add_parser(name, help="Pendiente de una fase posterior")
        if name != "qc":
            command.add_argument("video", nargs="?", type=Path)
    return parser


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def _scene_payload(result: SceneDetectionResult) -> dict:
    """Serialize at the application boundary; the detector returns plain models."""
    return {
        **asdict(result), "status": "PASS",
        "timestamp_reference": "first_decoded_frame_of_selected_stream",
        "score_range": [0, 100],
    }


def _audio_payload(result: AudioAnalysisResult) -> dict:
    """Keep serialization and its temporal conventions outside the acoustic engine."""
    return {
        **asdict(result),
        "timestamp_reference": "first_decoded_frame_of_selected_stream",
        "dbfs_silence": "null_means_negative_infinity",
    }


def _transcript_payload(result: TranscriptResult) -> dict:
    return {
        **asdict(result),
        "timestamp_reference": "first_decoded_frame_of_selected_stream",
        "timestamp_precision_seconds": 0.01,
        "timestamp_note": "Estimaciones del modelo; source_* conserva el offset de la pista.",
    }


def _moment_payload(result: MomentDetectionResult, bounds_source: str) -> dict:
    payload = asdict(result)
    payload.pop("moments")
    return {
        **payload, "timestamp_reference": "first_decoded_frame_of_selected_video_stream",
        "duration_source": bounds_source,
        "scope": "rule_based_candidates_requiring_editorial_review",
    }


def _moment_video_bounds(info, scenes: SceneDetectionResult) -> tuple[float, float | None, str, tuple[str, ...]]:
    """Use selected-video PTS, never global format duration or average FPS.

    AVStream durations are spans from the declared stream start. FFmpeg's
    Matroska DURATION tag is an absolute end timestamp, including offsets.
    Metadata bounds do not certify the last decoded frame; unknown stays null.
    """
    origin = Fraction(scenes.start_pts) * Fraction(scenes.time_base)
    stream = next((item for item in info.streams if item.get("index") == scenes.stream_index), None)
    unknown = ("No se conoce el fin de la pista de video seleccionada; el contexto posterior no tiene límite verificado.",)
    if stream is None:
        return float(origin), None, "unknown", unknown

    def number(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            result = Fraction(str(value))
            return result if math.isfinite(float(result)) else None
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

    base = number(stream.get("time_base"))
    start_pts = stream.get("start_pts")
    start = start_pts * base if type(start_pts) is int and base is not None and base > 0 else number(stream.get("start_time"))
    ticks = stream.get("duration_ts")
    span = number(stream.get("duration"))
    end = None
    provenance = "unknown"
    if type(ticks) is int and ticks >= 0 and base is not None and base > 0 and start is not None:
        end, provenance = start + ticks * base, "selected_video_duration_ts"
        if span is not None and span >= 0 and abs(span - ticks * base) > max(base, Fraction(1, 1_000_000)):
            raise MediaError("Las duraciones declaradas de la pista de video son contradictorias.")
    elif span is not None and span >= 0 and start is not None:
        end, provenance = start + span, "selected_video_duration"
    elif set((info.format_name or "").split(",")) & {"matroska", "webm"}:
        tags = stream.get("tags")
        raw = next((value for key, value in tags.items() if key.upper() == "DURATION"), None) if isinstance(tags, dict) else None
        match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", raw) if isinstance(raw, str) else None
        if match:
            end = int(match[1]) * 3600 + int(match[2]) * 60 + Fraction(match[3])
            provenance = "matroska_declared_absolute_end"
    if end is None:
        return float(origin), None, provenance, unknown
    last = max((Fraction(change.pts) * Fraction(scenes.time_base) for change in scenes.changes), default=origin)
    if end <= origin or end <= last:
        raise MediaError("El fin declarado del video no es posterior a los frames/cortes observados.")
    return float(origin), float(end - origin), provenance, ()


def _local_transcript_settings(settings: dict, root: Path) -> dict:
    """Resolve local model/executable paths without accessing a model registry."""
    values = dict(settings)
    for key in ("model", "executable"):
        value = values[key]
        if value is not None and (key == "model" or "/" in value):
            path = Path(value).expanduser()
            values[key] = str(path if path.is_absolute() else root / path)
    return values


def _transcript_diagnostics(report: dict, settings: dict, root: Path) -> None:
    """Optional, read-only checks; availability never certifies real inference."""
    values = _local_transcript_settings(settings, root)
    executable = shutil.which(values["executable"])
    model = values["model"]
    available_model = model is not None and Path(model).is_file()
    for name, available, detail in (
        ("transcription_engine", executable is not None, values["executable"]),
        ("transcription_model", available_model, model or "Sin modelo configurado"),
    ):
        report["checks"][name] = {
            "status": "PASS" if available else "OPTIONAL", "available": available,
            "required": False, "detail": detail,
            "inference_validated": False,
        }
        if not available:
            report["warnings"].append(f"Opcional: {name}: {detail}; no se instala ni descarga.")


def _doctor_output(report: dict, as_json: bool) -> None:
    if as_json:
        _print_json(report)
        return
    print(f"CLIpping doctor: {report['status']}")
    for name, check in report["checks"].items():
        detail = check.get("error") or check.get("version") or check.get("detail", "")
        print(f"  {name}: {check['status']} {detail}")
    if report["errors"]:
        print("Instala o configura las dependencias indicadas; consulta README.md.")


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _same_payload(destination: Path, payload: dict, options: dict, *, allow_hardlinks: bool = False) -> bool:
    try:
        saved = read_json_safe(destination, **options, allow_hardlinks=allow_hardlinks)
    except (json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(saved, dict):
        return False
    try:
        return _payload_hash(saved) == _payload_hash(payload)
    except ValueError:
        return False


def _publish_probe(repository, source: Path, payload: dict, destination: Path, config):
    """Journal durable → commit SQLite → JSON exclusivo; compensación sin borrar outputs."""
    options = {"root": config.root, "input_dir": config.paths["input"]}
    safe_destination(destination, **options, originals=(source,))
    if destination.exists():
        raise FileExistsError(f"El destino ya existe: {destination}")
    # Un esquema incompatible falla antes de publicar incluso el journal.
    repository.initialize()
    record_id = uuid4().hex
    journal = config.paths["working"] / ".publications" / f"{record_id}.json"
    entry = {"version": 1, "record_id": record_id, "source": str(source),
             "destination": str(destination.relative_to(config.root)),
             "metadata": payload, "sha256": _payload_hash(payload)}
    write_json_new(journal, entry, **options, originals=(source,))
    try:
        record = repository.add_probe(source, payload, record_id=record_id)
        write_json_new(destination, payload, **options, originals=(source,))
    except Exception:
        try:
            # Una publicación pudo completar link() antes de fallar fsync/cleanup.
            published = destination.exists() and _same_payload(destination, payload, options, allow_hardlinks=True)
            saved = repository.get_probe(record_id)
            if published and saved is not None and _payload_hash(saved.metadata) == _payload_hash(payload):
                print("Publicación pendiente de confirmar; ejecuta 'python -m clip_factory recover'.", file=sys.stderr)
            else:
                repository.delete_probe(record_id)
                unlink_safe(journal, **options)
        except Exception as recovery_error:
            print(f"Recuperación pendiente ({recovery_error}); ejecuta 'python -m clip_factory recover'.", file=sys.stderr)
        raise
    warnings = []
    try:
        unlink_safe(journal, **options)
    except (OSError, ValueError) as error:
        warnings.append(f"JSON y SQLite confirmados; journal pendiente de retirar: {error}. Ejecuta recover.")
    return record, warnings


def _recover_publications(repository, config) -> list[str]:
    """Completa únicamente journals válidos; nunca sustituye un archivo en conflicto."""
    folder = config.paths["working"] / ".publications"
    if not folder.exists():
        return []
    options = {"root": config.root, "input_dir": config.paths["input"]}
    recovered = []
    with secure_directory(folder, **options) as directory:
        journals = sorted(name for name in os.listdir(directory.fd) if name.endswith(".json"))
        for name in journals:
            journal = folder / name
            entry = read_json_safe(journal, **options, allow_hardlinks=True)
            if not isinstance(entry, dict) or type(entry.get("version")) is not int or entry["version"] != 1:
                raise ValueError(f"Journal inválido o versión no admitida: {journal}")
            if not all(key in entry for key in ("record_id", "source", "destination", "metadata", "sha256")):
                raise ValueError(f"Journal incompleto: {journal}")
            if not isinstance(entry["source"], str) or not Path(entry["source"]).is_absolute() or not isinstance(entry["destination"], str):
                raise ValueError(f"Rutas de journal inválidas: {journal}")
            record_id = entry.get("record_id")
            if not isinstance(record_id, str) or UUID(record_id).hex != record_id or name != f"{record_id}.json":
                raise ValueError(f"Identificador de journal inválido: {journal}")
            relative = Path(entry["destination"])
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".json":
                raise ValueError(f"Destino de journal inválido: {journal}")
            source = Path(entry["source"])
            destination = config.root / relative
            payload = entry["metadata"]
            if not isinstance(payload, dict) or _payload_hash(payload) != entry.get("sha256"):
                raise ValueError(f"Contenido de journal inválido: {journal}")
            if is_within(destination.resolve(), folder.resolve()):
                raise ValueError("El directorio de recuperación está reservado")
            safe_destination(destination, **options, originals=(source,), allow_hardlinks=True)
            # Resolver conflictos antes de insertar; no eliminar ni sustituir outputs.
            if destination.exists() and not _same_payload(destination, payload, options, allow_hardlinks=True):
                raise FileExistsError(f"Conflicto de recuperación; archivo preservado: {destination}")
            recover_json_links(journal, **options)
            if destination.exists():
                recover_json_links(destination, **options)
            try:
                record = repository.get_probe(record_id)
            except FileNotFoundError:
                record = None
            if record is not None and (_payload_hash(record.metadata) != _payload_hash(payload) or record.source_path != str(source.resolve())):
                raise ValueError(f"Conflicto de registro SQLite: {record_id}")
            if record is None:
                repository.add_probe(source, payload, record_id=record_id)
            if not destination.exists():
                write_json_new(destination, payload, **options, originals=(source,))
            unlink_safe(journal, **options)
            recovered.append(str(destination))
    return recovered


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    invoked_command = arguments.command
    if arguments.command == "detect":
        arguments.command = "analyze"
    if arguments.command in {"clip", "process", "qc"}:
        print(f"'{arguments.command}' está pendiente de una fase posterior. Disponibles: init, doctor, config, probe, analyze, detect.", file=sys.stderr)
        return 2
    logger = None
    try:
        try:
            config = load_config(root=arguments.root, config_path=arguments.config, profile=arguments.profile)
        except ConfigError as error:
            if arguments.command != "doctor":
                raise
            # Doctor sigue siendo útil si precisamente falta el lector de YAML.
            report = doctor()
            report["status"] = "FAIL"
            report["checks"]["config"] = {"status": "FAIL", "available": False, "required": True, "error": str(error)}
            report["errors"].append(str(error))
            _doctor_output(report, arguments.json)
            return 1

        moments_requested = arguments.command == "analyze" and arguments.moments
        scenes_requested = arguments.command == "analyze" and (arguments.scenes or moments_requested)
        audio_requested = arguments.command == "analyze" and (arguments.audio or moments_requested)
        transcription_requested = arguments.command == "analyze" and arguments.transcribe
        moment_fields = tuple(config.data["analysis"]["moments"])
        if arguments.command == "analyze" and not moments_requested and any(
            getattr(arguments, "moment_" + name) is not None for name in moment_fields
        ):
            raise ValueError("Las opciones de momentos requieren --moments o el comando detect.")
        if arguments.command == "analyze" and not scenes_requested and any(
            getattr(arguments, name) is not None
            for name in ("scene_threshold", "scene_timeout", "video_stream", "min_scene_duration")
        ):
            raise ValueError("Las opciones de escenas requieren --scenes.")
        if arguments.command == "analyze" and not audio_requested and any(
            getattr(arguments, name) is not None
            for name in ("audio_window", "audio_peak_threshold", "audio_silence_threshold",
                         "audio_change_threshold", "audio_min_silence", "audio_timeout",
                         "audio_stream", "audio_max_windows", "audio_max_events", "audio_energy_floor")
        ):
            raise ValueError("Las opciones de audio requieren --audio.")
        if arguments.command == "analyze" and not transcription_requested and any(
            getattr(arguments, name) is not None
            for name in ("transcript_engine", "transcript_executable", "transcript_model",
                         "transcript_language", "transcript_timeout", "transcript_audio_stream",
                         "transcript_chunk_seconds", "transcript_max_segments", "transcript_silence_threshold")
        ):
            raise ValueError("Las opciones de transcripción requieren --transcribe.")
        try:
            source = arguments.video.expanduser().resolve() if hasattr(arguments, "video") else None
        except RuntimeError as error:
            raise ValueError(f"No se puede resolver el archivo local: {error}") from error
        originals = (source,) if source is not None else ()
        # Verificar destinos antes de abrir siquiera el log o SQLite.
        database = config.paths["database"]
        sidecars = tuple(Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm", ".lock"))
        for target in (config.paths["logs"] / "clip_factory.jsonl", database, *sidecars):
            safe_destination(target, root=config.root, input_dir=config.paths["input"], originals=originals)
        logger = configure_logging(config.root, config.paths["input"], config.paths["logs"], config.log_level, originals=originals)
        with operation(logger, invoked_command, source) as outcome:
            if arguments.command == "doctor":
                report = doctor(config.ffmpeg_bin, config.ffprobe_bin)
                _transcript_diagnostics(report, config.data["analysis"]["transcription"], config.root)
                _doctor_output(report, arguments.json)
                if report["status"] != "PASS":
                    raise MediaError("El diagnóstico detectó dependencias ausentes o no válidas.")
                return 0
            if arguments.command == "config":
                _print_json(config.to_dict())
                return 0
            if sqlite3 is None:
                raise MediaError("SQLite no está disponible en este intérprete de Python. Ejecuta doctor.")
            from clip_factory.database.repository import Repository

            repository = Repository(config.paths["database"], root=config.root, input_dir=config.paths["input"])
            if arguments.command == "recover":
                with workspace_lock(config.root, config.paths["input"], config.paths["working"]):
                    recovered = _recover_publications(repository, config)
                _print_json({"status": "PASS", "recovered": recovered})
                return 0
            if arguments.command == "init":
                for name, path in config.paths.items():
                    if name == "input":
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        with secure_directory(path.parent if name == "database" else path,
                                              root=config.root, input_dir=config.paths["input"], create=True):
                            pass
                repository.initialize()
                print(f"CLIpping inicializado en {config.root}. Originales preservados.")
                return 0

            destination = None
            if arguments.output != "-":
                if arguments.output:
                    candidate = Path(arguments.output).expanduser()
                    destination = candidate if candidate.is_absolute() else config.root / candidate
                else:
                    suffix = "analyze" if scenes_requested or audio_requested or transcription_requested or moments_requested else "probe"
                    destination = config.paths["working"] / f"{suffix}-{uuid4().hex}.{suffix}.json"
                destination = Path(os.path.abspath(destination))
                if is_within(destination.resolve(), (config.paths["working"] / ".publications").resolve()):
                    raise ValueError("El directorio .publications está reservado para recuperación")
                safe_destination(destination, root=config.root, input_dir=config.paths["input"], originals=originals)
                if destination.exists():
                    raise FileExistsError(f"El destino ya existe: {destination}")

            audio_settings = {}
            if audio_requested:
                settings = config.data["analysis"]["audio"]
                overrides = {
                    "window_seconds": arguments.audio_window,
                    "peak_dbfs": arguments.audio_peak_threshold,
                    "silence_dbfs": arguments.audio_silence_threshold,
                    "change_db": arguments.audio_change_threshold,
                    "min_silence_duration": arguments.audio_min_silence,
                    "timeout": arguments.audio_timeout,
                    "audio_stream": arguments.audio_stream,
                    "max_windows": arguments.audio_max_windows,
                    "max_events": arguments.audio_max_events,
                    "energy_floor_dbfs": arguments.audio_energy_floor,
                }
                settings = {**settings, **{key: value for key, value in overrides.items() if value is not None}}
                audio_settings = asdict(AudioAnalysisConfig(**settings))

            transcript_config = None
            if transcription_requested:
                settings = config.data["analysis"]["transcription"]
                overrides = {
                    "engine": arguments.transcript_engine,
                    "executable": arguments.transcript_executable,
                    "model": arguments.transcript_model,
                    "language": arguments.transcript_language,
                    "timeout": arguments.transcript_timeout,
                    "audio_stream": arguments.transcript_audio_stream,
                    "chunk_seconds": arguments.transcript_chunk_seconds,
                    "max_segments": arguments.transcript_max_segments,
                    "silence_dbfs": arguments.transcript_silence_threshold,
                }
                settings = {**settings, **{key: value for key, value in overrides.items() if value is not None}}
                settings = asdict(TranscriptConfig(**settings))
                transcript_config = TranscriptConfig(**_local_transcript_settings(settings, config.root))

            moment_config = None
            if moments_requested:
                settings = config.data["analysis"]["moments"]
                overrides = {name: getattr(arguments, "moment_" + name) for name in moment_fields}
                moment_config = MomentDetectionConfig(**{
                    **settings, **{key: value for key, value in overrides.items() if value is not None},
                })

            # Probe y motores sólo leen; la publicación permanece en la CLI.
            scenes = audio = transcript = None
            info = probe_media(arguments.video, ffprobe_bin=config.ffprobe_bin, timeout=config.probe_timeout)
            payload = info.to_dict()
            payload.update({
                "schema_version": 1, "scope": "metadata_only",
                "analysis": {"audio": "not_run", "scenes": "not_run", "transcription": "not_run", "moments": "not_run"},
                "validation": validate_metadata(info),
            })
            if moments_requested and payload["validation"]["status"] == "FAIL":
                raise MediaError("Moment Detection requiere metadata de video válida: " +
                                 "; ".join(payload["validation"]["errors"]))
            if scenes_requested:
                settings = config.data["analysis"]["scenes"]
                overrides = {
                    "threshold": arguments.scene_threshold,
                    "timeout": arguments.scene_timeout,
                    "video_stream": arguments.video_stream,
                    "min_scene_duration": arguments.min_scene_duration,
                }
                settings = {**settings, **{key: value for key, value in overrides.items() if value is not None}}
                with operation(logger, "scene_detection", source):
                    scenes = detect_scenes(
                        arguments.video, **settings, media_info=info,
                        ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                        threads=config.data["runtime"]["threads"],
                    )
                payload["schema_version"] = 2
                payload["scope"] = "metadata_and_scenes"
                payload["analysis"]["scenes"] = "completed"
                payload["scene_detection"] = _scene_payload(scenes)
            if audio_requested:
                # Sequential stages keep only one decoder active on machines with limited RAM.
                with operation(logger, "audio_analysis", source) as audio_outcome:
                    audio = analyze_audio(
                        arguments.video, **audio_settings, media_info=info,
                        ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                        threads=config.data["runtime"]["threads"],
                    )
                    if audio.status == "SKIPPED" or audio.warnings:
                        audio_outcome.result = "WARN"
                        audio_outcome.warnings.extend(audio.warnings)
                        outcome.result = "WARN"
                    outcome.warnings.extend(audio.warnings)
                payload["schema_version"] = 3
                payload["scope"] = "metadata_and_scenes_and_audio" if scenes_requested else "metadata_and_audio"
                payload["analysis"]["audio"] = "skipped_no_audio" if audio.status == "SKIPPED" else "completed"
                payload["audio_analysis"] = _audio_payload(audio)
            if transcription_requested:
                with operation(logger, "transcription", source) as transcript_outcome:
                    transcript = transcribe_media(
                        arguments.video, config=transcript_config, media_info=info,
                        ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                        threads=config.data["runtime"]["threads"], root=config.root,
                        input_dir=config.paths["input"], working_dir=config.paths["working"],
                    )
                    if transcript.status == "SKIPPED" or transcript.warnings:
                        transcript_outcome.result = "WARN"
                        transcript_outcome.warnings.extend(transcript.warnings)
                        outcome.result = "WARN"
                    outcome.warnings.extend(transcript.warnings)
                payload["schema_version"] = 4
                stages = ["metadata"]
                if scenes_requested:
                    stages.append("scenes")
                if audio_requested:
                    stages.append("audio")
                stages.append("transcription")
                payload["scope"] = "_and_".join(stages)
                payload["analysis"]["transcription"] = (
                    f"skipped_{transcript.skip_reason}" if transcript.status == "SKIPPED" else "completed"
                )
                payload["transcription"] = _transcript_payload(transcript)
            if moments_requested:
                with operation(logger, "moment_detection", source) as moment_outcome:
                    origin, video_duration, bounds_source, bounds_warnings = _moment_video_bounds(info, scenes)
                    moments = detect_moments(
                        source=str(source), origin_seconds=origin, duration=video_duration,
                        scenes=scenes, audio=audio, transcript=transcript, config=moment_config,
                    )
                    moment_outcome.warnings.extend((*bounds_warnings, *moments.warnings))
                    if moment_outcome.warnings:
                        moment_outcome.result = "WARN"
                        outcome.result = "WARN"
                    outcome.warnings.extend(moment_outcome.warnings)
                payload["schema_version"] = 5
                payload["scope"] = "metadata_and_scenes_and_audio" + (
                    "_and_transcription" if transcription_requested else ""
                ) + "_and_moments"
                payload["analysis"]["moments"] = "completed"
                payload["moments"] = [asdict(candidate) for candidate in moments.moments]
                payload["moment_detection"] = _moment_payload(moments, bounds_source)
                payload["moment_detection"]["warnings"] = list(dict.fromkeys((*bounds_warnings, *moments.warnings)))
            with workspace_lock(config.root, config.paths["input"], config.paths["working"]):
                if destination is not None:
                    record, warnings = _publish_probe(repository, source, payload, destination, config)
                    outcome.warnings.extend(warnings)
                else:
                    record = repository.add_probe(source, payload)
            if destination is None:
                _print_json(payload)
            else:
                if scenes_requested or audio_requested or transcription_requested:
                    completed = ["metadata"]
                    if scenes_requested:
                        completed.append(f"escenas (cambios visuales: {len(scenes.changes)})")
                    if audio_requested:
                        completed.append("audio omitido: sin pista" if audio.status == "SKIPPED" else f"audio (eventos acústicos: {len(audio.events)})")
                    if transcription_requested:
                        completed.append(f"transcripción omitida: {transcript.skip_reason}" if transcript.status == "SKIPPED"
                                         else f"transcripción (segmentos: {len(transcript.segments)})")
                    if moments_requested:
                        completed.append(f"candidatos para revisión: {len(moments.moments)}")
                    scope = " y ".join(completed)
                else:
                    scope = "metadata; detección y edición pendientes"
                print(f"Metadata: {destination}\nRegistro SQLite: {record.id}\nAlcance: {scope}.")
            for diagnostic in payload.get("diagnostics", []):
                if diagnostic.get("severity") in {"WARNING", "WARN"}:
                    outcome.warnings.append(diagnostic["message"])
            outcome.warnings[:] = list(dict.fromkeys(outcome.warnings))
            if outcome.warnings or payload["validation"]["status"] == "WARN":
                outcome.result = "WARN"
                for warning in outcome.warnings:
                    print(f"Advertencia: {warning}", file=sys.stderr)
            if payload["validation"]["status"] == "FAIL":
                raise MediaError("; ".join(payload["validation"]["errors"]))
            return 0
    except (ConfigError, MediaError, OSError, ValueError, _DATABASE_ERROR) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if logger is not None:
            try:
                close_logging(logger)
            except OSError as error:
                print(f"Error al cerrar el log: {error}", file=sys.stderr)
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
