"""Installed CLI exercises independent engines, persistence and immutable media."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from clip_factory.media.ffmpeg import run_media_command

pytestmark = pytest.mark.integration


@pytest.fixture
def tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Audio CLI omitida: requiere FFmpeg y FFprobe locales; no se instalan automáticamente.")
    return ffmpeg, ffprobe


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def configure(root, tools):
    folder = root / "config"
    folder.mkdir(parents=True)
    (folder / "default.yaml").write_text(
        f"media:\n  ffmpeg_bin: {json.dumps(tools[0])}\n  ffprobe_bin: {json.dumps(tools[1])}\n",
        encoding="utf-8",
    )


def synthesize(root, tools, *, with_audio=True, multiple=False):
    source = root / "data/input/original acústico.mkv"
    source.parent.mkdir(parents=True)
    command = [
        tools[0], "-nostdin", "-hide_banner", "-v", "error", "-n", "-copyts",
        "-f", "lavfi", "-i",
        "color=black:s=160x90:r=10:d=3,drawbox=color=white:t=fill:enable='gte(t,1)*lt(t,2)'",
    ]
    if multiple:
        command += ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=3"]
    if with_audio:
        command += [
            "-f", "lavfi", "-i",
            "aevalsrc='if(lt(t,1),0.02,if(lt(t,2),0.8,0))*sin(2*PI*440*t)':s=8000:d=3",
        ]
    command += ["-map", "0:v"]
    if multiple:
        command += ["-map", "1:a", "-map", "2:a", "-disposition:a:0", "0", "-disposition:a:1", "default"]
    elif with_audio:
        command += ["-map", "1:a"]
    command += ["-c:v", "ffv1", "-c:a", "pcm_s16le", "-threads", "1", str(source)]
    run_media_command(command, timeout=30)
    configure(root, tools)
    return source


def run_cli(root, source, *options, cwd):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "clip_factory", "--root", str(root), "analyze", str(source), *options],
        cwd=cwd, env=environment, shell=False, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.parametrize("options,output,schema,scope", [
    ([], "-", 1, "metadata_only"),
    (["--scenes"], "data/working/scenes.json", 2, "metadata_and_scenes"),
    (["--audio"], None, 3, "metadata_and_audio"),
    (["--scenes", "--audio"], "data/working/both.json", 3, "metadata_and_scenes_and_audio"),
])
def test_four_cli_modes_publish_structured_results_sqlite_logs_and_preserve_original(
    tmp_path, tools, options, output, schema, scope,
):
    root = tmp_path / "proyecto local con espacios"
    source = synthesize(root, tools)
    before = signature(source)
    arguments = list(options)
    if "--audio" in options:
        arguments += ["--audio-window", ".5", "--audio-timeout", "15", "--audio-stream", "1"]
    if output is not None:
        arguments += ["--output", output]
    result = run_cli(root, source, *arguments, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    if output == "-":
        payload = json.loads(result.stdout)
    else:
        destination = root / output if output else next((root / "data/working").glob("*.analyze.json"))
        payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema_version"] == schema and payload["scope"] == scope
    assert payload["validation"]["status"] == "PASS"
    assert payload["analysis"] == {
        "scenes": "completed" if "--scenes" in options else "not_run",
        "audio": "completed" if "--audio" in options else "not_run",
        "transcription": "not_run", "moments": "not_run",
    }
    if "--scenes" in options:
        assert [change["timestamp_seconds"] for change in payload["scene_detection"]["changes"]] == [1, 2]
    else:
        assert "scene_detection" not in payload
    if "--audio" in options:
        audio = payload["audio_analysis"]
        assert audio["status"] == "PASS" and audio["engine"] == "ffmpeg.astats"
        assert audio["stream_index"] == 1 and audio["sample_rate"] == 8000 and audio["channels"] == 1
        assert audio["config"]["window_seconds"] == .5 and audio["config"]["timeout"] == 15
        assert audio["samples_analyzed"] == 24000 and len(audio["windows"]) == 6
        assert [window["start_seconds"] for window in audio["windows"]] == [0, .5, 1, 1.5, 2, 2.5]
        assert audio["windows"][0]["rms_dbfs"] == pytest.approx(-36.99, abs=.05)
        assert audio["windows"][2]["rms_dbfs"] == pytest.approx(-4.95, abs=.05)
        assert audio["windows"][-1]["rms_dbfs"] is None
        assert audio["peak_dbfs"] == pytest.approx(-1.94, abs=.05)
        assert audio["dbfs_silence"] == "null_means_negative_infinity"
        assert audio["timestamp_reference"] == "first_decoded_frame_of_selected_stream"
        assert audio["events"] and all("start_pts" in event and "end_pts" in event for event in audio["events"])
        assert all("emotion" not in event and "reaction" not in event for event in audio["events"])
    else:
        assert "audio_analysis" not in payload
    # Serialization must use standard JSON even for a mathematically silent window.
    json.dumps(payload, allow_nan=False)
    with closing(sqlite3.connect(root / "database/clip_factory.db")) as connection:
        rows = connection.execute("SELECT source_path, metadata_json FROM probes").fetchall()
    assert len(rows) == 1 and rows[0][0] == str(source.resolve())
    assert json.loads(rows[0][1]) == payload
    logs = [json.loads(line) for line in (root / "logs/clip_factory.jsonl").read_text().splitlines()]
    operations = {entry["operation"]: entry for entry in logs}
    assert ("audio_analysis" in operations) == ("--audio" in options)
    assert ("scene_detection" in operations) == ("--scenes" in options)
    for entry in logs:
        assert entry["result"] == "PASS" and entry["error"] is None
        assert entry["file"] == str(source.resolve()) and entry["duration_seconds"] >= 0
        assert datetime.fromisoformat(entry["date"]).tzinfo == timezone.utc
    assert signature(source) == before


@pytest.mark.parametrize("options", [["--audio"], ["--scenes", "--audio"]])
def test_cli_no_audio_skips_acoustics_and_preserves_visual_result(tmp_path, tools, options):
    root = tmp_path / "silent video"
    source = synthesize(root, tools, with_audio=False)
    before = signature(source)
    result = run_cli(root, source, *options, "--output", "-", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 3 and payload["analysis"]["audio"] == "skipped_no_audio"
    audio = payload["audio_analysis"]
    assert audio["status"] == "SKIPPED" and audio["warnings"]
    assert audio["windows"] == [] and audio["events"] == []
    assert audio["stream_index"] is None and audio["rms_dbfs"] is None and audio["samples_analyzed"] == 0
    assert "Advertencia" in result.stderr
    if "--scenes" in options:
        assert payload["analysis"]["scenes"] == "completed"
        assert [change["timestamp_seconds"] for change in payload["scene_detection"]["changes"]] == [1, 2]
    logs = [json.loads(line) for line in (root / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert next(entry for entry in logs if entry["operation"] == "audio_analysis")["result"] == "WARN"
    assert next(entry for entry in logs if entry["operation"] == "analyze")["result"] == "WARN"
    assert signature(source) == before


def test_cli_audio_selects_absolute_track_index(tmp_path, tools):
    root = tmp_path / "multi pista"
    source = synthesize(root, tools, multiple=True)
    before = signature(source)
    for index in (1, 2):
        result = run_cli(root, source, "--audio", "--audio-stream", str(index), "--output", "-", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["analysis"]["scenes"] == "not_run" and "scene_detection" not in payload
        audio = payload["audio_analysis"]
        assert audio["stream_index"] == index
        assert (audio["rms_dbfs"] is None) == (index == 1)
        assert (audio["peak_dbfs"] is None) if index == 1 else (audio["peak_dbfs"] > -3)
    default = run_cli(root, source, "--audio", "--output", "-", cwd=tmp_path)
    assert default.returncode == 0, default.stderr
    assert json.loads(default.stdout)["audio_analysis"]["stream_index"] == 2
    assert signature(source) == before


@pytest.mark.parametrize("options,expected", [
    (["--audio-stream", "0"], "pista"),
    (["--audio-window", "0"], "window"),
    (["--audio-timeout", "0.000001"], "timeout"),
])
def test_cli_audio_error_and_timeout_do_not_publish_partial_results(tmp_path, tools, options, expected):
    root = tmp_path / "failing audio"
    source = synthesize(root, tools)
    before = signature(source)
    destination = root / "data/working/result.json"
    result = run_cli(root, source, "--audio", "--scenes", *options, "--output", str(destination), cwd=tmp_path)
    assert result.returncode == 1 and expected in result.stderr.lower()
    assert result.stdout == "" and not destination.exists()
    assert not (root / "database/clip_factory.db").exists()
    assert signature(source) == before
