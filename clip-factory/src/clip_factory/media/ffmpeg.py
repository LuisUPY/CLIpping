"""Small, replaceable local-process adapter and dependency diagnostics.

No command uses a shell, installs software, downloads models or writes media.
The wrapper is deliberately separate from the metadata parser.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from typing import Any


class MediaError(RuntimeError):
    """A media operation failed, retaining diagnostic information for callers."""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] = (),
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.stderr = stderr
        self.returncode = returncode


class DependencyError(MediaError):
    """A required local executable could not be started."""


def _diagnostic(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_media_command(
    command: Sequence[str | os.PathLike[str]], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    """Run a trusted local argument list, with bounded time and captured output.

    This low-level adapter does not decide output paths. Transformation modules
    must enforce original-file protection before using it. Phase 1 only calls
    read-only executable diagnostics and ffprobe through this adapter.
    """
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("El comando debe ser una lista de argumentos no vacía.")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("El timeout debe ser un número finito mayor que cero.")
    arguments = [os.fspath(argument) for argument in command]
    try:
        result = subprocess.run(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DependencyError(
            f"No se encontró el ejecutable local: {arguments[0]}", command=arguments
        ) from exc
    except PermissionError as exc:
        raise DependencyError(
            f"Sin permiso para ejecutar: {arguments[0]}", command=arguments
        ) from exc
    except subprocess.TimeoutExpired as exc:
        diagnostic = _diagnostic(exc.stderr)
        raise MediaError(
            f"{arguments[0]} superó el timeout de {timeout:g} segundos.",
            command=arguments,
            stderr=diagnostic,
        ) from exc
    except OSError as exc:
        raise MediaError(
            f"No se pudo ejecutar {arguments[0]}: {exc}", command=arguments
        ) from exc
    if result.returncode != 0:
        diagnostic = result.stderr.strip()
        detail = f": {diagnostic}" if diagnostic else ""
        raise MediaError(
            f"{arguments[0]} terminó con código {result.returncode}{detail}",
            command=arguments,
            stderr=result.stderr,
            returncode=result.returncode,
        )
    return result


def _check(available: bool, *, required: bool, **details: Any) -> dict[str, Any]:
    return {
        "available": available,
        "required": required,
        "status": "PASS" if available else ("FAIL" if required else "OPTIONAL"),
        **details,
    }


def doctor(
    ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe", timeout: float = 10.0
) -> dict[str, Any]:
    """Inspect required local tools and future optional modules without installing.

    ``checks`` contains structured, JSON-compatible records. Missing optional
    packages never fail phase 1. Detection of an optional package is not proof
    that its engine, model or hardware acceleration works.
    """
    checks: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []
    supported_python = sys.version_info >= (3, 10)
    checks["python"] = _check(
        supported_python,
        required=True,
        version=platform.python_version(),
        minimum="3.10",
        executable=sys.executable,
    )
    if not supported_python:
        errors.append("Se requiere Python 3.10 o posterior.")
    checks["platform"] = _check(
        True,
        required=False,
        system=platform.system(),
        machine=platform.machine(),
        detail="CPU como base; Apple Silicon es compatible, CUDA no es necesaria.",
    )
    for name, executable in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
        try:
            result = run_media_command([executable, "-version"], timeout=timeout)
            version_lines = (result.stdout or result.stderr).splitlines()
            if not version_lines or not version_lines[0].startswith(f"{name} version "):
                raise MediaError(f"{executable} no devolvió una versión válida de {name}.")
            checks[name] = _check(
                True, required=True, executable=executable, version=version_lines[0]
            )
        except (MediaError, ValueError) as exc:
            checks[name] = _check(False, required=True, executable=executable, error=str(exc))
            errors.append(str(exc))
    try:
        sqlite = importlib.import_module("sqlite3")
    except ImportError as exc:
        checks["sqlite3"] = _check(False, required=True, error=str(exc))
        errors.append(f"SQLite no está disponible: {exc}")
    else:
        try:
            with sqlite.connect(":memory:") as connection:
                version = connection.execute("SELECT sqlite_version()").fetchone()[0]
            checks["sqlite3"] = _check(True, required=True, version=version)
        except sqlite.Error as exc:
            checks["sqlite3"] = _check(False, required=True, error=str(exc))
            errors.append(f"SQLite no está disponible: {exc}")
    try:
        yaml = importlib.import_module("yaml")
        if yaml.safe_load("clip_factory: true") != {"clip_factory": True}:
            raise ValueError("La lectura básica de YAML no produjo el resultado esperado.")
        checks["yaml"] = _check(
            True, required=True, version=getattr(yaml, "__version__", "unknown")
        )
    except (ImportError, AttributeError, ValueError) as exc:
        checks["yaml"] = _check(False, required=True, error=str(exc))
        errors.append(f"PyYAML no está disponible o no funciona: {exc}")
    for name in ("whisper", "faster_whisper", "cv2", "librosa", "scenedetect", "yt_dlp"):
        try:
            available = importlib.util.find_spec(name) is not None
            checks[name] = _check(
                available,
                required=False,
                detail="Reservado para fases futuras; motor y modelos no validados.",
            )
            if not available:
                warnings.append(f"Opcional, no necesario en fase 1: {name}.")
        except (ImportError, ValueError) as exc:
            checks[name] = _check(False, required=False, error=str(exc))
            warnings.append(f"No se pudo comprobar el módulo opcional {name}: {exc}")
    return {
        "status": "FAIL" if errors else "PASS",
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }
