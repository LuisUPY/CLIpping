"""Registro JSONL de operaciones, incluyendo fallos y duración real."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from time import perf_counter
from threading import RLock
from typing import Iterable, Iterator

from clip_factory.utils.files import open_safe_file


def _raise_close_errors(errors: list[Exception]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        raise OSError(f"Fallos al cerrar los registros: {details}") from errors[0]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "date": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "file": getattr(record, "source_file", None),
            "operation": getattr(record, "operation", record.getMessage()),
            "duration_seconds": round(getattr(record, "duration_seconds", 0.0), 6),
            "result": getattr(record, "result", "INFO"),
            "error": getattr(record, "error", None),
            "warnings": getattr(record, "warnings", []),
            "level": record.levelname,
        }, ensure_ascii=False, allow_nan=False)


class StrictStreamHandler(logging.StreamHandler):
    """No convertir fallos de disco en una operación aparentemente exitosa."""

    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        if error is not None:
            raise error
        raise OSError("No se pudo escribir el registro de operación")

    def __init__(self, stream) -> None:
        super().__init__(stream)
        self._owned_stream = stream
        self._owned_stream_closed = False

    def close(self) -> None:
        """StreamHandler no cierra por sí mismo los streams que recibe."""
        with self.lock:
            if self._owned_stream_closed:
                return
            self._owned_stream_closed = True
            errors = []
            try:
                self.flush()
            except Exception as error:
                errors.append(error)
            try:
                self._owned_stream.close()
            except Exception as error:
                errors.append(error)
            finally:
                super().close()
            _raise_close_errors(errors)


class OperationLogger(logging.Logger):
    """Logger de una ejecución; no se registra en el singleton de logging."""

    def __init__(self) -> None:
        super().__init__("clip_factory.operation")
        self.lifecycle_lock = RLock()
        self.closed = False

    def ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("El logger de esta ejecución está cerrado")

    def isEnabledFor(self, level: int) -> bool:
        with self.lifecycle_lock:
            self.ensure_open()
            return super().isEnabledFor(level)

    def handle(self, record: logging.LogRecord) -> None:
        with self.lifecycle_lock:
            self.ensure_open()
            super().handle(record)


@dataclass
class OperationResult:
    """Resultado mutable para operaciones que finalizan con advertencias."""

    result: str = "PASS"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def configure_logging(root: Path, input_dir: Path, logs_dir: Path, level: str = "INFO", *,
                      originals: Iterable[Path] = ()) -> OperationLogger:
    descriptor = open_safe_file(
        logs_dir / "clip_factory.jsonl", root=root, input_dir=input_dir,
        flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND, originals=originals,
    )
    try:
        stream = os.fdopen(descriptor, "a", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    try:
        logger = OperationLogger()
        handler = StrictStreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        # Las operaciones importantes siempre se registran, incluso con WARNING.
        logger.setLevel(logging.DEBUG if level == "DEBUG" else logging.INFO)
        logger.propagate = False
    except Exception:
        stream.close()
        raise
    return logger


def close_logging(logger: OperationLogger) -> None:
    with logger.lifecycle_lock:
        if logger.closed:
            return
        logger.closed = True
        errors = []
        for handler in logger.handlers[:]:
            try:
                handler.close()
            except Exception as error:
                errors.append(error)
            finally:
                logger.removeHandler(handler)
        _raise_close_errors(errors)


@contextmanager
def operation(logger: OperationLogger, name: str, source: Path | None = None) -> Iterator[OperationResult]:
    logger.ensure_open()
    started = perf_counter()
    outcome = OperationResult()
    try:
        yield outcome
        if outcome.result not in {"PASS", "WARN", "FAIL"}:
            raise ValueError(f"Resultado de operación inválido: {outcome.result}")
    except Exception as error:
        try:
            logger.error(name, extra={
                "source_file": str(source) if source is not None else None,
                "operation": name, "duration_seconds": perf_counter() - started,
                "result": "FAIL", "error": f"{type(error).__name__}: {error}",
                "warnings": outcome.warnings,
            })
        except Exception as logging_error:
            try:
                print(
                    "Advertencia: también falló el registro de la operación "
                    f"{name}: {type(logging_error).__name__}: {logging_error}",
                    file=sys.stderr,
                )
            except Exception as stderr_error:
                raise error from stderr_error
            raise error from logging_error
        raise
    else:
        level = {"PASS": logging.INFO, "WARN": logging.WARNING, "FAIL": logging.ERROR}[outcome.result]
        logger.log(level, name, extra={
            "source_file": str(source) if source is not None else None,
            "operation": name, "duration_seconds": perf_counter() - started,
            "result": outcome.result, "error": outcome.error,
            "warnings": outcome.warnings,
        })
