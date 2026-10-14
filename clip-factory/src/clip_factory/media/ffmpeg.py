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
import selectors
import subprocess
import sys
from collections.abc import Callable, Sequence
from time import monotonic
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
    Successful commands retain stderr unchanged; FFmpeg uses it for ordinary
    progress, so each caller owns its diagnostic severity policy.
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


def stream_media_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    on_stdout_line: Callable[[str], None],
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    """Stream text metadata, draining stderr concurrently through POSIX pipes.

    Python receives no frames. Lines and the diagnostic tail are bounded to
    64 KiB; stdout is never accumulated. Parser failure, timeout or interruption
    kills and reaps the child. Supported on macOS (including ARM64) and Linux.
    """
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("El comando debe ser una lista de argumentos no vacía.")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("El timeout debe ser un número finito mayor que cero.")
    arguments = [os.fspath(argument) for argument in command]
    deadline = monotonic() + timeout
    try:
        process = subprocess.Popen(
            arguments, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise DependencyError(
            f"No se pudo iniciar el ejecutable local {arguments[0]}: {exc}",
            command=arguments,
        ) from exc
    except OSError as exc:
        raise MediaError(f"No se pudo ejecutar {arguments[0]}: {exc}", command=arguments) from exc
    pending = b""
    diagnostic = b""
    truncated = False
    limit = 65536
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, limit)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        truncated |= len(diagnostic) + len(chunk) > limit
                        diagnostic = (diagnostic + chunk)[-limit:]
                        continue
                    lines = (pending + chunk).split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        if len(line) > limit:
                            raise MediaError("FFmpeg devolvió una línea de metadata demasiado larga.", command=arguments)
                        on_stdout_line(line.decode("utf-8", errors="replace"))
                    if len(pending) > limit:
                        raise MediaError("FFmpeg devolvió una línea de metadata demasiado larga.", command=arguments)
            if pending:
                on_stdout_line(pending.decode("utf-8", errors="replace"))
            process.wait(timeout=max(0.0, deadline - monotonic()))
        stderr = diagnostic.decode("utf-8", errors="replace")
        if truncated:
            stderr = "[stderr truncado a los últimos 64 KiB]\n" + stderr
        if process.returncode != 0:
            raise MediaError(
                f"{arguments[0]} terminó con código {process.returncode}: {stderr.strip()}",
                command=arguments, stderr=stderr, returncode=process.returncode,
            )
        return subprocess.CompletedProcess(arguments, process.returncode, "", stderr)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            f"{arguments[0]} superó el timeout de {timeout:g} segundos.",
            command=arguments, stderr=diagnostic.decode("utf-8", errors="replace"),
        ) from exc
    except OSError as exc:
        raise MediaError(f"Error leyendo {arguments[0]}: {exc}", command=arguments) from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


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
