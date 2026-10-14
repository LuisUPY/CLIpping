"""Real lightweight child processes verify streaming and reliable cancellation."""

import subprocess
import sys
from time import monotonic

import pytest

from clip_factory.media import ffmpeg
from clip_factory.media.ffmpeg import DependencyError, MediaError, stream_media_command


def command(script):
    return [sys.executable, "-u", "-c", script]


@pytest.fixture
def children(monkeypatch):
    created = []
    real_popen = subprocess.Popen
    def tracked(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr(ffmpeg.subprocess, "Popen", tracked)
    return created


def test_incremental_delivery_before_process_exit(children, tmp_path):
    lines = []
    acknowledged = tmp_path / "consumed"
    def consume(line):
        lines.append(line)
        assert children[0].poll() is None  # Not capture_output followed by parsing.
        acknowledged.touch()
    script = ("import time; from pathlib import Path; print('first', flush=True)\n"
              f"while not Path({str(acknowledged)!r}).exists(): time.sleep(.01)")
    result = stream_media_command(command(script),
                                  on_stdout_line=consume, timeout=2)
    assert lines == ["first"] and result.stdout == "" and result.returncode == 0


def test_concurrent_stderr_drain_is_bounded_and_does_not_deadlock():
    lines = []
    result = stream_media_command(command(
        "import os; os.write(2,b'x'*200000); os.write(1,b'first\\nsecond'); os.write(2,b'TAIL')"),
        on_stdout_line=lines.append, timeout=3)
    assert lines == ["first", "second"]
    assert result.stderr.endswith("TAIL") and len(result.stderr) < 65700
    assert "truncado" in result.stderr


@pytest.mark.parametrize("script", [
    "import time; time.sleep(10)",
    "import os,time; os.close(1); os.close(2); time.sleep(10)",
    "import os;\nwhile True: os.write(2,b'x'*4096)",
])
def test_timeout_kills_and_reaps_even_when_pipes_are_idle_or_flooded(script, children):
    started = monotonic()
    with pytest.raises(MediaError, match="timeout"):
        stream_media_command(command(script), on_stdout_line=lambda _: None, timeout=.15)
    assert monotonic() - started < 2
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@pytest.mark.parametrize("error", [ValueError("invalid metadata"), KeyboardInterrupt()])
def test_parser_failure_or_cancellation_reaps_child(error, children):
    def fail(line):
        raise error
    with pytest.raises(type(error)):
        stream_media_command(command("import time; print('bad',flush=True); time.sleep(10)"),
                             on_stdout_line=fail, timeout=2)
    assert children[0].poll() is not None


def test_long_unterminated_line_is_rejected_and_reaped(children):
    with pytest.raises(MediaError, match="demasiado larga"):
        stream_media_command(command("import os,time; os.write(1,b'x'*200000); time.sleep(10)"),
                             on_stdout_line=lambda _: None, timeout=2)
    assert children[0].poll() is not None


def test_nonzero_exit_retains_diagnostics_and_returncode():
    with pytest.raises(MediaError) as error:
        stream_media_command(command("import sys; sys.stderr.write('decode error'); sys.exit(7)"),
                             on_stdout_line=lambda _: None)
    assert error.value.returncode == 7 and error.value.stderr == "decode error"


def test_missing_executable_is_dependency_error(tmp_path):
    with pytest.raises(DependencyError):
        stream_media_command([str(tmp_path / "absent")], on_stdout_line=lambda _: None)
