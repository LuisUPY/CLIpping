"""Registro JSONL de operaciones, incluyendo fallos y duración real."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Iterator

from clip_factory.utils.files import safe_destination


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "date": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "file": getattr(record, "source_file", None),
            "operation": getattr(record, "operation", record.getMessage()),
            "duration_seconds": round(getattr(record, "duration_seconds", 0.0), 6),
            "result": getattr(record, "result", "INFO"),
            "error": getattr(record, "error", None),
            "level": record.levelname,
        }, ensure_ascii=False, allow_nan=False)


class StrictStreamHandler(logging.StreamHandler):
    """No convertir fallos de disco en una operación aparentemente exitosa."""

    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        if error is not None:
            raise error
        raise OSError("No se pudo escribir el registro de operación")


def configure_logging(root: Path, input_dir: Path, logs_dir: Path, level: str = "INFO") -> logging.Logger:
    target = safe_destination(logs_dir / "clip_factory.jsonl", root=root, input_dir=input_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    stream = os.fdopen(descriptor, "a", encoding="utf-8")
    logger = logging.getLogger(f"clip_factory.{root.resolve()}")
    close_logging(logger)
    handler = StrictStreamHandler(stream)
    # StreamHandler.close no cierra streams externos; mantener propiedad explícita.
    handler._clip_factory_stream = stream
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    # Las operaciones importantes siempre se registran, incluso con WARNING.
    logger.setLevel(logging.DEBUG if level == "DEBUG" else logging.INFO)
    logger.propagate = False
    return logger


def close_logging(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        stream = getattr(handler, "_clip_factory_stream", None)
        try:
            handler.flush()
        finally:
            try:
                handler.close()
                if stream is not None:
                    stream.close()
            finally:
                logger.removeHandler(handler)


@contextmanager
def operation(logger: logging.Logger, name: str, source: Path | None = None) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    except Exception as error:
        logger.error(name, extra={
            "source_file": str(source) if source is not None else None,
            "operation": name, "duration_seconds": perf_counter() - started,
            "result": "FAIL", "error": f"{type(error).__name__}: {error}",
        })
        raise
    else:
        logger.info(name, extra={
            "source_file": str(source) if source is not None else None,
            "operation": name, "duration_seconds": perf_counter() - started,
            "result": "PASS", "error": None,
        })
