"""Regresiones de propiedad, errores y aperturas seguras del registro."""

import errno
import json
import os
from pathlib import Path

import pytest

from clip_factory.utils import files
from clip_factory.utils.logging import close_logging, configure_logging, operation


def _records(root):
    return [
        json.loads(line)
        for line in (root / "logs/clip_factory.jsonl").read_text().splitlines()
    ]


class FailingStream:
    def write(self, message):
        raise OSError("disco lleno")

    def flush(self):
        pass


def test_each_execution_owns_its_handlers_and_closing_one_keeps_other_open(tmp_path):
    first = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    second = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    assert first is not second
    assert first.handlers[0] is not second.handlers[0]
    try:
        with operation(first, "primera"):
            pass
        close_logging(first)
        with operation(second, "segunda"):
            pass
        assert [row["operation"] for row in _records(tmp_path)] == ["primera", "segunda"]
    finally:
        close_logging(first)
        close_logging(second)


def test_close_is_idempotent_and_releases_owned_descriptor(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    stream = logger.handlers[0].stream
    descriptor = stream.fileno()
    close_logging(logger)
    close_logging(logger)
    assert stream.closed
    assert not logger.handlers
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


def test_closed_logger_rejects_operation_before_work_and_direct_messages(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    close_logging(logger)
    executed = False
    with pytest.raises(RuntimeError, match="cerrado"):
        with operation(logger, "tardía"):
            executed = True
    assert not executed
    with pytest.raises(RuntimeError, match="cerrado"):
        logger.info("tardío")


def test_primary_failure_survives_secondary_log_error(tmp_path, capsys):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    logger.handlers[0].stream = FailingStream()
    primary = ValueError("fallo principal del análisis")
    try:
        with pytest.raises(ValueError, match="fallo principal") as caught:
            with operation(logger, "analyze"):
                raise primary
        assert caught.value is primary
        assert isinstance(caught.value.__cause__, OSError)
        assert "disco lleno" in capsys.readouterr().err
    finally:
        close_logging(logger)


def test_failed_success_record_propagates_instead_of_reporting_success(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    logger.handlers[0].stream = FailingStream()
    try:
        with pytest.raises(OSError, match="disco lleno"):
            with operation(logger, "analyze"):
                pass
    finally:
        close_logging(logger)


def test_warning_outcome_records_diagnostics(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    try:
        with operation(logger, "probe") as outcome:
            outcome.result = "WARN"
            outcome.error = "FFprobe devolvió diagnósticos"
            outcome.warnings.append("header damaged")
    finally:
        close_logging(logger)
    record = _records(tmp_path)[0]
    assert record["result"] == "WARN"
    assert record["level"] == "WARNING"
    assert record["warnings"] == ["header damaged"]
    assert record["error"] == "FFprobe devolvió diagnósticos"


def test_logging_rejects_original_even_when_outside_input(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    original = logs_dir / "clip_factory.jsonl"
    original.write_bytes(b"fuente externa inmutable\n")
    with pytest.raises(files.FileSafetyError, match="original"):
        configure_logging(tmp_path, tmp_path / "data/input", logs_dir, originals=(original,))
    assert original.read_bytes() == b"fuente externa inmutable\n"


def test_invalid_outcome_is_visible_and_logged_as_failure(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    try:
        with pytest.raises(ValueError, match="inválido"):
            with operation(logger, "probe") as outcome:
                outcome.result = "IGNORADO"
    finally:
        close_logging(logger)
    assert _records(tmp_path)[0]["result"] == "FAIL"


def test_fdopen_failure_does_not_leak_the_open_descriptor(tmp_path, monkeypatch):
    opened = []
    real_open = files.open_safe_file

    def tracking_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def broken_fdopen(*args, **kwargs):
        raise OSError("fdopen falló")

    monkeypatch.setattr("clip_factory.utils.logging.open_safe_file", tracking_open)
    monkeypatch.setattr("clip_factory.utils.logging.os.fdopen", broken_fdopen)
    with pytest.raises(OSError, match="fdopen falló"):
        configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    assert len(opened) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(opened[0])
    assert caught.value.errno == errno.EBADF


def test_log_directory_swap_before_leaf_open_cannot_modify_original(tmp_path, monkeypatch):
    input_dir = tmp_path / "data/input"
    input_dir.mkdir(parents=True)
    original = input_dir / "clip_factory.jsonl"
    original.write_bytes(b"original inmutable\n")
    before = original.stat()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    real_open = os.open
    swapped = False

    def swap_before_leaf_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "clip_factory.jsonl" and dir_fd is not None and not swapped:
            logs_dir.rename(tmp_path / "logs-detached")
            logs_dir.symlink_to(input_dir, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(files.os, "open", swap_before_leaf_open)
    logger = None
    try:
        try:
            logger = configure_logging(tmp_path, input_dir, logs_dir)
        except (files.FileSafetyError, OSError):
            # Rechazar la sustitución o conservar el fd ya validado son seguros.
            pass
        else:
            with operation(logger, "probe"):
                pass
    finally:
        if logger is not None:
            close_logging(logger)
    assert swapped, "La apertura final debe ser relativa a un directorio abierto"
    assert original.read_bytes() == b"original inmutable\n"
    after = original.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns,
    )
