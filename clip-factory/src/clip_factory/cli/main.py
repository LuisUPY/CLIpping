"""CLI delgada: configuración, diagnóstico e inspección local de metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence
from uuid import uuid4

from clip_factory import __version__
from clip_factory.config.loader import ConfigError, load_config
from clip_factory.media.ffmpeg import MediaError, doctor
from clip_factory.media.probe import probe_media
from clip_factory.media.validation import validate_metadata
from clip_factory.utils.files import safe_destination, write_json_new
from clip_factory.utils.logging import close_logging, configure_logging, operation

try:
    import sqlite3
except ImportError:
    # Doctor debe poder diagnosticar también intérpretes sin _sqlite3.
    sqlite3 = None

_DATABASE_ERROR = sqlite3.Error if sqlite3 is not None else MediaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipping", description="CLIpping — Clip Factory local, fase 1")
    parser.add_argument("--version", action="version", version=f"CLIpping {__version__}")
    parser.add_argument("--root", type=Path, help="Directorio del proyecto (también CLIP_FACTORY_ROOT)")
    parser.add_argument("--config", type=Path, help="YAML adicional, relativo al root o absoluto")
    parser.add_argument("--profile", help="Perfil de configuración, por ejemplo mac_m1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Crear carpetas y esquema SQLite, sin instalar dependencias")
    diagnostic = commands.add_parser("doctor", help="Comprobar herramientas locales y compatibilidad")
    diagnostic.add_argument("--json", action="store_true", help="Diagnóstico estructurado")
    commands.add_parser("config", help="Mostrar configuración efectiva en JSON")
    for name in ("probe", "analyze"):
        command = commands.add_parser(name, help="Leer metadata; sin detección ni edición en fase 1")
        command.add_argument("video", type=Path, help="Archivo local, incluyendo rutas externas a input")
        command.add_argument("--output", help="Nuevo JSON dentro del proyecto; '-' imprime en stdout")
    for name in ("detect", "clip", "process", "qc"):
        command = commands.add_parser(name, help="Pendiente de una fase posterior")
        if name != "qc":
            command.add_argument("video", nargs="?", type=Path)
    return parser


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command in {"detect", "clip", "process", "qc"}:
        print(f"'{arguments.command}' está pendiente de una fase posterior. Fase 1: init, doctor, config, probe, analyze.", file=sys.stderr)
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

        try:
            source = arguments.video.expanduser().resolve() if hasattr(arguments, "video") else None
        except RuntimeError as error:
            raise ValueError(f"No se puede resolver el archivo local: {error}") from error
        originals = (source,) if source is not None else ()
        # Verificar destinos antes de abrir siquiera el log o SQLite.
        database = config.paths["database"]
        sidecars = tuple(Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm"))
        for target in (config.paths["logs"] / "clip_factory.jsonl", database, *sidecars):
            safe_destination(target, root=config.root, input_dir=config.paths["input"], originals=originals)
        logger = configure_logging(config.root, config.paths["input"], config.paths["logs"], config.log_level)
        with operation(logger, arguments.command, source):
            if arguments.command == "doctor":
                report = doctor(config.ffmpeg_bin, config.ffprobe_bin)
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
            if arguments.command == "init":
                for name, path in config.paths.items():
                    (path.parent if name == "database" else path).mkdir(parents=True, exist_ok=True)
                repository.initialize()
                print(f"CLIpping inicializado en {config.root}. Originales preservados.")
                return 0

            destination = None
            if arguments.output != "-":
                if arguments.output:
                    candidate = Path(arguments.output).expanduser()
                    destination = candidate if candidate.is_absolute() else config.root / candidate
                else:
                    destination = config.paths["working"] / f"{source.stem}-{uuid4().hex}.probe.json"
                safe_destination(destination, root=config.root, input_dir=config.paths["input"], originals=originals)
                if destination.exists():
                    raise FileExistsError(f"El destino ya existe: {destination}")

            # El adaptador recibe sólo lectura; no existe paso de edición en fase 1.
            info = probe_media(arguments.video, ffprobe_bin=config.ffprobe_bin, timeout=config.probe_timeout)
            payload = info.to_dict()
            payload.update({
                "schema_version": 1, "scope": "metadata_only",
                "analysis": {"audio": "not_run", "scenes": "not_run", "transcription": "not_run", "moments": "not_run"},
                "validation": validate_metadata(info),
            })
            if destination is not None:
                output = write_json_new(destination, payload, root=config.root, input_dir=config.paths["input"], originals=originals)
            record = repository.add_probe(source, payload)
            if destination is None:
                _print_json(payload)
            else:
                print(f"Metadata: {output}\nRegistro SQLite: {record.id}\nAlcance: metadata; detección y edición pendientes.")
            if payload["validation"]["status"] != "PASS":
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
