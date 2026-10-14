"""Local synthetic cuts, fractional/VFR PTS, offsets, tracks and CLI persistence."""

from contextlib import closing
from fractions import Fraction
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from time import monotonic

import pytest

from clip_factory.analysis.scenes import SceneDetectionError, detect_scenes
from clip_factory.media.ffmpeg import MediaError, run_media_command
from clip_factory.media.probe import probe_media

pytestmark = pytest.mark.integration


@pytest.fixture
def tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Scene Detection omitido: requiere FFmpeg/FFprobe locales; no se instalan automáticamente.")
    return ffmpeg, ffprobe


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def synthesize(path, tools, *, rate="25", duration="3", enable="gte(t,1)*lt(t,2)", filters="", extra=()):
    ffmpeg, _ = tools
    path.parent.mkdir(parents=True, exist_ok=True)
    chain = f"color=black:s=160x90:r={rate}:d={duration}"
    if enable:
        chain += f",drawbox=color=white:t=fill:enable='{enable}'"
    if filters:
        chain += "," + filters
    run_media_command([
        ffmpeg, "-nostdin", "-v", "error", "-n", "-copyts", "-f", "lavfi", "-i", chain,
        "-c:v", "mpeg4" if path.suffix == ".mp4" else "ffv1", "-threads", "1",
        "-fps_mode", "passthrough", *extra, str(path),
    ])
    return path


def detect(path, tools, **kwargs):
    return detect_scenes(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1], **kwargs)


@pytest.mark.parametrize("threshold,minimum,expected", [(10, 0, [1, 2]), (100, 0, []), (10, 1.5, [2])])
def test_known_cuts_sensitivity_minimum_and_original_preservation(tmp_path, tools, threshold, minimum, expected):
    path = synthesize(tmp_path / "input con espacios/original.mkv", tools)
    before = signature(path)
    result = detect(path, tools, threshold=threshold, min_scene_duration=minimum)
    assert [s.timestamp_seconds for s in result.changes] == expected
    assert all(s.score is not None and 0 <= s.score <= 100 for s in result.changes)
    assert result.frames_analyzed == 75 and result.engine == "ffmpeg.scdet"
    assert signature(path) == before


def test_constant_video_is_successful_without_changes(tmp_path, tools):
    path = synthesize(tmp_path / "constant.mkv", tools, enable=None, duration="1")
    result = detect(path, tools)
    assert result.changes == () and result.frames_analyzed == 25


def test_fractional_pts_are_more_precise_than_scd_time(tmp_path, tools):
    path = synthesize(tmp_path / "fractional.mp4", tools, rate="30000/1001", duration="1.1",
                      enable="gte(n,12)*lt(n,20)", extra=("-video_track_timescale", "30000"))
    before = signature(path)
    result = detect(path, tools)
    assert result.time_base == "1/30000"
    assert [cut.pts for cut in result.changes] == [12012, 20020]
    assert [cut.timestamp_seconds for cut in result.changes] == [float(Fraction(12012, 30000)), float(Fraction(20020, 30000))]
    assert result.changes[0].timestamp_seconds != 0.400  # scdet prints only milliseconds.
    assert signature(path) == before


@pytest.mark.parametrize("offset", [-0.5, 0, 5])
def test_vfr_timestamps_and_shifted_origin(tmp_path, tools, offset):
    path = synthesize(tmp_path / "variable.mkv", tools, rate="10", duration="2",
                      enable="gte(n,5)*lt(n,12)",
                      filters=f"setpts='if(lt(N,10),N,10+2*(N-10))/(10*TB)+({offset})/TB'",
                      extra=("-avoid_negative_ts", "disabled"))
    before = signature(path)
    result = detect(path, tools)
    frames = json.loads(run_media_command([
        tools[1], "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=pts", "-of", "json", str(path),
    ]).stdout)["frames"]
    pts = [frame["pts"] for frame in frames]
    assert len(set(b-a for a, b in zip(pts, pts[1:]))) == 2  # Actually VFR.
    assert result.start_pts == offset * 1000
    assert [c.pts for c in result.changes] == [pts[5], pts[12]]
    assert [c.timestamp_seconds for c in result.changes] == [0.5, 1.4]
    assert [c.source_timestamp_seconds for c in result.changes] == [
        float(Fraction(str(offset)) + Fraction(1, 2)),
        float(Fraction(str(offset)) + Fraction(14, 10)),
    ]
    assert result.frames_analyzed == 20 and signature(path) == before


def test_multiple_tracks_default_selection_and_absolute_index(tmp_path, tools):
    path = tmp_path / "tracks.mkv"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-copyts",
        "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=3",
        "-f", "lavfi", "-i", "color=black:s=160x90:r=10:d=3",
        "-f", "lavfi", "-i", "color=black:s=160x90:r=10:d=3,drawbox=color=white:t=fill:enable='gte(t,1)*lt(t,2)',setpts=PTS+7/TB",
        "-map", "0:a", "-map", "1:v", "-map", "2:v", "-c:a", "pcm_s16le",
        "-c:v", "ffv1", "-threads", "1", "-disposition:v:0", "0", "-disposition:v:1", "default", str(path),
    ])
    before = signature(path)
    default = detect(path, tools)
    assert default.stream_index == 2 and default.start_pts == 7000
    assert [s.timestamp_seconds for s in default.changes] == [1, 2]
    assert detect(path, tools, video_stream=1).changes == ()
    for index in (0, 10):
        with pytest.raises(SceneDetectionError, match="pista"):
            detect(path, tools, video_stream=index)
    assert signature(path) == before


def test_audio_only_and_corrupt_inputs_fail_visibly(tmp_path, tools):
    audio = tmp_path / "audio.wav"
    run_media_command([tools[0], "-v", "error", "-n", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=0.2", str(audio)])
    before = signature(audio)
    with pytest.raises(SceneDetectionError, match="video"):
        detect(audio, tools)
    assert signature(audio) == before
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"this is not video")
    before = signature(corrupt)
    with pytest.raises(MediaError):
        detect(corrupt, tools)
    assert signature(corrupt) == before


def test_decoder_error_after_valid_probe_is_not_partial_success(tmp_path, tools):
    path = synthesize(tmp_path / "damaged.mp4", tools)
    info = probe_media(path, ffprobe_bin=tools[1])
    packets = json.loads(run_media_command([
        tools[1], "-v", "error", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=pos,size", "-of", "json", str(path),
    ]).stdout)["packets"]
    packet = packets[-3]
    # Damage an encoded frame, retaining a readable container and early frames.
    with path.open("r+b") as handle:
        handle.seek(int(packet["pos"]))
        handle.write(b'\x00' * int(packet["size"]))
    before = signature(path)
    with pytest.raises(MediaError) as caught:
        detect(path, tools, media_info=info)
    assert caught.value.stderr and signature(path) == before


def test_real_ffmpeg_timeout_preserves_source_and_reaps_process(tmp_path, tools, monkeypatch):
    path = synthesize(tmp_path / "timeout.mkv", tools, duration="1")
    info = probe_media(path, ffprobe_bin=tools[1])
    before = signature(path)
    from clip_factory.analysis import scenes
    original = scenes.stream_media_command
    # Throttle real FFmpeg input, making the timeout independent of machine speed.
    def realtime(command, **kwargs):
        command = list(command)
        command[command.index("-i"):command.index("-i")] = ["-readrate", "0.1"]
        return original(command, **kwargs)
    monkeypatch.setattr(scenes, "stream_media_command", realtime)
    started = monotonic()
    with pytest.raises(MediaError, match="timeout"):
        detect(path, tools, media_info=info, timeout=.4)
    assert monotonic() - started < 2 and signature(path) == before


def cli(root, video, *options, cwd):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run([
        sys.executable, "-m", "clip_factory", "--root", str(root), "analyze", str(video), *options,
    ], cwd=cwd, env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("output", ["-", "data/working/scenes.json", None])
def test_cli_structured_scenes_json_sqlite_logging_and_unchanged_metadata_mode(tmp_path, tools, output):
    root = tmp_path / "proyecto"
    path = synthesize(root / "data/input/original.mkv", tools)
    (root / "config").mkdir()
    (root / "config/default.yaml").write_text(
        f"media:\n  ffmpeg_bin: {json.dumps(tools[0])}\n  ffprobe_bin: {json.dumps(tools[1])}\n"
        "analysis:\n  scenes:\n    threshold: 100\n", encoding="utf-8")
    before = signature(path)
    options = ["--scenes", "--scene-threshold", "10", "--scene-timeout", "15", "--video-stream", "0"]
    if output is not None:
        options += ["--output", output]
    result = cli(root, path, *options, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    if output == "-":
        payload = json.loads(result.stdout)
    else:
        destination = root / output if output else next((root / "data/working").glob("*.analyze.json"))
        payload = json.loads(destination.read_text())
    assert payload["schema_version"] == 2 and payload["scope"] == "metadata_and_scenes"
    assert payload["analysis"] == {"scenes": "completed", "audio": "not_run", "transcription": "not_run", "moments": "not_run"}
    detection = payload["scene_detection"]
    assert detection["status"] == "PASS" and detection["threshold"] == 10 and detection["timeout"] == 15
    assert detection["time_base"] == "1/1000" and detection["start_pts"] == 0
    assert [cut["pts"] for cut in detection["changes"]] == [1000, 2000]
    assert [cut["timestamp_seconds"] for cut in detection["changes"]] == [1, 2]
    with closing(sqlite3.connect(root / "database/clip_factory.db")) as db:
        assert json.loads(db.execute("SELECT metadata_json FROM probes").fetchone()[0]) == payload
    logs = [json.loads(line) for line in (root / "logs/clip_factory.jsonl").read_text().splitlines()]
    scene_logs = [line for line in logs if line["operation"] == "scene_detection"]
    assert len(scene_logs) == 1 and scene_logs[0]["result"] == "PASS" and scene_logs[0]["duration_seconds"] > 0
    metadata = cli(root, path, "--output", "-", cwd=tmp_path)
    assert metadata.returncode == 0, metadata.stderr
    legacy = json.loads(metadata.stdout)
    assert legacy["schema_version"] == 1 and legacy["scope"] == "metadata_only"
    assert legacy["analysis"]["scenes"] == "not_run" and "scene_detection" not in legacy
    assert signature(path) == before
