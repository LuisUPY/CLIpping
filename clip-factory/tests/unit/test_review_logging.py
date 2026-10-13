"""An operation must not report success after its audit record failed to write."""

import logging

import pytest

from clip_factory.utils.logging import close_logging, configure_logging, operation


class UnwritableStream:
    def write(self, message):
        raise OSError("No space left on device")

    def flush(self):
        pass


def test_log_write_failures_are_not_silently_ignored(tmp_path, monkeypatch):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    logger.handlers[0].stream = UnwritableStream()
    monkeypatch.setattr(logging, "raiseExceptions", False)
    try:
        with pytest.raises(OSError, match="No space left"):
            with operation(logger, "analyze"):
                pass
    finally:
        close_logging(logger)
