"""Dependency diagnostics and process failures remain explicit and testable."""

import importlib
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from clip_factory.media import ffmpeg
from clip_factory.media.ffmpeg import DependencyError, MediaError, doctor, run_media_command


def test_argument_list_never_uses_shell(monkeypatch):
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(ffmpeg.subprocess, "run", execute)
    command = ["ffprobe", "a file; $(not-a-command).mp4"]
    result = run_media_command(command, timeout=12)
    assert result.stdout == "ok"
    assert execute.call_args.args[0] == command
    assert execute.call_args.kwargs["shell"] is False
    assert execute.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert execute.call_args.kwargs["timeout"] == 12
    assert execute.call_args.kwargs["capture_output"] is True


@pytest.mark.parametrize("command", ["ffprobe file.mp4", [], b"ffprobe"])
def test_rejects_unstructured_commands(command):
    with pytest.raises(ValueError, match="lista de argumentos"):
        run_media_command(command)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        run_media_command(["ffprobe"], timeout=timeout)


@pytest.mark.parametrize("failure", [FileNotFoundError("missing"), PermissionError("blocked")])
def test_missing_or_nonexecutable_binary_is_dependency_error(monkeypatch, failure):
    monkeypatch.setattr(ffmpeg.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(DependencyError) as captured:
        run_media_command(["/custom tools/ffprobe", "-version"])
    assert captured.value.command[0] == "/custom tools/ffprobe"
    assert captured.value.__cause__ is failure


def test_timeout_retains_partial_error(monkeypatch):
    failure = subprocess.TimeoutExpired(["ffprobe"], 3, stderr=b"partial diagnostic")
    monkeypatch.setattr(ffmpeg.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(MediaError, match="timeout") as captured:
        run_media_command(["ffprobe"], timeout=3)
    assert captured.value.stderr == "partial diagnostic"


def test_nonzero_exit_retains_error_and_code(monkeypatch):
    monkeypatch.setattr(
        ffmpeg.subprocess, "run",
        Mock(return_value=subprocess.CompletedProcess([], 7, "", "corrupt video\n")),
    )
    with pytest.raises(MediaError, match="corrupt video") as captured:
        run_media_command(["ffprobe", "broken.mp4"])
    assert captured.value.returncode == 7
    assert captured.value.stderr == "corrupt video\n"


def test_zero_exit_keeps_normal_ffmpeg_stderr_without_classifying_it(monkeypatch):
    stderr = "frame=   25 fps=0.0 time=00:00:01.00\n"
    monkeypatch.setattr(
        ffmpeg.subprocess, "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "", stderr)),
    )
    result = run_media_command(["ffmpeg", "-version"])
    assert result.returncode == 0
    assert result.stderr == stderr


def _doctor_setup(monkeypatch):
    original_import = importlib.import_module

    def modules(name):
        if name == "yaml":
            return SimpleNamespace(__version__="6.0.2", safe_load=lambda _: {"clip_factory": True})
        return original_import(name)

    def execute(command, **kwargs):
        name = "ffprobe" if "ffprobe" in command[0] else "ffmpeg"
        return subprocess.CompletedProcess(command, 0, f"{name} version test\n", "")

    monkeypatch.setattr(ffmpeg.sys, "version_info", (3, 10, 0))
    monkeypatch.setattr(ffmpeg.importlib, "import_module", modules)
    monkeypatch.setattr(ffmpeg.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(ffmpeg, "run_media_command", execute)
    return execute


def test_doctor_passes_without_future_optional_modules(monkeypatch):
    _doctor_setup(monkeypatch)
    report = doctor(ffmpeg_bin="/custom tools/ffmpeg", ffprobe_bin="/custom tools/ffprobe")
    assert report["status"] == "PASS"
    assert report["errors"] == []
    assert report["checks"]["ffmpeg"]["executable"] == "/custom tools/ffmpeg"
    assert report["checks"]["sqlite3"]["available"] is True
    assert report["checks"]["yaml"]["available"] is True
    assert report["checks"]["faster_whisper"]["required"] is False
    assert report["checks"]["faster_whisper"]["status"] == "OPTIONAL"
    assert report["warnings"]


def test_doctor_reports_missing_binary_and_keeps_checking(monkeypatch):
    execute = _doctor_setup(monkeypatch)

    def missing_ffmpeg(command, **kwargs):
        if command[0] == "ffmpeg":
            raise DependencyError("No ffmpeg")
        return execute(command, **kwargs)

    monkeypatch.setattr(ffmpeg, "run_media_command", missing_ffmpeg)
    report = doctor()
    assert report["status"] == "FAIL"
    assert report["checks"]["ffmpeg"]["available"] is False
    assert report["checks"]["ffprobe"]["available"] is True
    assert report["errors"] == ["No ffmpeg"]


def test_doctor_reports_unsupported_python(monkeypatch):
    _doctor_setup(monkeypatch)
    monkeypatch.setattr(ffmpeg.sys, "version_info", (3, 9, 6))
    report = doctor()
    assert report["status"] == "FAIL"
    assert report["checks"]["python"]["minimum"] == "3.10"


@pytest.mark.parametrize("missing", ["yaml", "sqlite3"])
def test_doctor_reports_missing_python_modules(monkeypatch, missing):
    _doctor_setup(monkeypatch)
    import_module = ffmpeg.importlib.import_module

    def unavailable(name):
        if name == missing:
            raise ImportError(f"missing {name}")
        return import_module(name)

    monkeypatch.setattr(ffmpeg.importlib, "import_module", unavailable)
    report = doctor()
    assert report["status"] == "FAIL"
    assert report["checks"][missing]["available"] is False
    assert report["errors"]
