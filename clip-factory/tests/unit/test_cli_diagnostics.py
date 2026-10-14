"""Diagnósticos del probe conservados hasta CLI, JSON, SQLite y logging."""

from dataclasses import replace
import json
import subprocess
from unittest.mock import Mock

import pytest

from clip_factory.cli import main as cli
from clip_factory.database.repository import Repository
from clip_factory.media import probe
from clip_factory.media.ffmpeg import MediaError
from clip_factory.media.probe import MediaInfo


@pytest.fixture
def source(tmp_path):
    video = tmp_path / "data/input/original.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"original immutable")
    return video


def _info(source):
    return MediaInfo(
        str(source), 2.5, 1920, 1080, 30.0, "30/1", True, "h264", (),
    )


def _records(root):
    repository = Repository(
        root / "database/clip_factory.db", root=root, input_dir=root / "data/input",
    )
    return repository.list_probes()


def _last_log(root):
    return json.loads((root / "logs/clip_factory.jsonl").read_text().splitlines()[-1])


@pytest.mark.parametrize("severity", ["WARNING", "WARN"])
def test_warning_is_successful_but_visible_in_stdout_sqlite_log_and_stderr(
    source, tmp_path, monkeypatch, capsys, severity,
):
    diagnostic = {"severity": severity, "message": "duración estimada por el proveedor"}
    info = replace(_info(source), diagnostics=(diagnostic,))
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: info)
    before = source.stat()
    assert cli.main([
        "--root", str(tmp_path), "analyze", str(source), "--output", "-",
    ]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "WARN"
    assert payload["validation"]["status"] == "WARN"
    assert payload["diagnostics"] == [diagnostic]
    assert payload["validation"]["diagnostics"] == [diagnostic]
    assert f"Advertencia: {diagnostic['message']}" in captured.err
    records = _records(tmp_path)
    assert len(records) == 1 and records[0].metadata == payload
    log = _last_log(tmp_path)
    assert log["result"] == "WARN" and log["level"] == "WARNING"
    assert diagnostic["message"] in log["warnings"]
    assert log["error"] is None
    assert source.read_bytes() == b"original immutable"
    assert source.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("output", ["-", "data/working/report.json"])
def test_ffprobe_error_with_zero_exit_is_persisted_and_fails_cli(
    source, tmp_path, monkeypatch, capsys, output,
):
    diagnostic = "[h264 @ 0x123] header damaged"
    metadata = {
        "format": {"duration": "2.5", "format_name": "mov,mp4"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
    }
    execute = Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps(metadata), diagnostic + "\n",
    ))
    monkeypatch.setattr(probe, "run_media_command", execute)
    before = source.stat()
    assert cli.main([
        "--root", str(tmp_path), "probe", str(source), "--output", output,
    ]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if output == "-" else json.loads(
        (tmp_path / output).read_text(),
    )
    assert payload["status"] == "FAIL"
    assert payload["validation"]["status"] == "FAIL"
    assert all(payload["validation"]["checks"].values())
    assert payload["diagnostics"] == [{"severity": "ERROR", "message": diagnostic}]
    assert diagnostic in payload["validation"]["errors"]
    assert f"Error: {diagnostic}" in captured.err
    records = _records(tmp_path)
    assert len(records) == 1 and records[0].metadata == payload
    log = _last_log(tmp_path)
    assert log["result"] == "FAIL" and log["level"] == "ERROR"
    assert diagnostic in log["error"]
    assert execute.call_args.args[0][1:3] == ["-v", "error"]
    assert source.read_bytes() == b"original immutable"
    assert source.stat().st_mtime_ns == before.st_mtime_ns


def test_main_preserves_functional_error_when_failure_record_cannot_be_written(
    source, tmp_path, monkeypatch, capsys,
):
    class UnwritableStream:
        def write(self, message):
            raise OSError("disco lleno en el log")

        def flush(self):
            pass

    original_configure = cli.configure_logging
    created = []

    def configure_with_failed_stream(*args, **kwargs):
        logger = original_configure(*args, **kwargs)
        owned_stream = logger.handlers[0].stream
        logger.handlers[0].stream = UnwritableStream()
        created.append((logger, owned_stream))
        return logger

    def failed_probe(*args, **kwargs):
        raise MediaError("fallo funcional del probe")

    monkeypatch.setattr(cli, "configure_logging", configure_with_failed_stream)
    monkeypatch.setattr(cli, "probe_media", failed_probe)
    assert cli.main(["--root", str(tmp_path), "probe", str(source)]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Error: fallo funcional del probe" in captured.err.splitlines()
    assert "también falló el registro de la operación probe" in captured.err
    assert "disco lleno en el log" in captured.err
    assert "Error: disco lleno en el log" not in captured.err.splitlines()
    assert len(created) == 1
    assert created[0][0].closed and created[0][1].closed
    assert not (tmp_path / "database/clip_factory.db").exists()
    assert source.read_bytes() == b"original immutable"
