"""Detector contracts: exact PTS, stream choice, missing data and failure paths."""

from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
import subprocess

import pytest

from clip_factory.analysis import scenes
from clip_factory.media.ffmpeg import DependencyError, MediaError
from clip_factory.media.probe import MediaInfo


def video(index=0, *, default=0, attached=0, time_base="1/90000"):
    return {"index": index, "codec_type": "video", "time_base": time_base,
            "disposition": {"default": default, "attached_pic": attached}}


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "original con espacios.mp4"
    source.write_bytes(b"immutable")
    info = MediaInfo(str(source), 3, 160, 90, 999, "999/1", False, "h264", (video(),))
    monkeypatch.setattr(scenes, "probe_media", lambda *args, **kwargs: info)
    monkeypatch.setattr(scenes, "run_media_command", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, "ffmpeg version test-build\n", ""))
    return source, info


def feed(parser, pts, *, score="45.123", cut=False):
    parser.feed(f"frame:0 pts:{pts} pts_time:999.999")
    parser.feed("lavfi.scd.mafd=45.123")
    if score is not None:
        parser.feed(f"lavfi.scd.score={score}")
    if cut:
        parser.feed("lavfi.scd.time=999.999")


def test_exact_pts_ignores_rounded_times_and_fps(context, monkeypatch):
    source, info = context
    seen = []
    def stream(command, *, on_stdout_line, timeout):
        seen.append(command)
        for pts, cut in ((-90000, False), (-59997, True), (-10000, False), (9001, True)):
            on_stdout_line(f"frame:0 pts:{pts} pts_time:0")
            on_stdout_line("lavfi.scd.score=42.123")
            if cut:
                on_stdout_line("lavfi.scd.time=0.000")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(scenes, "stream_media_command", stream)
    before = source.stat().st_mtime_ns
    result = scenes.detect_scenes(source, media_info=info, threads=1)
    assert result.start_pts == -90000 and result.time_base == "1/90000"
    assert result.changes[0].timestamp_seconds == float(Fraction(30003, 90000))
    assert result.changes[0].source_timestamp_seconds == float(Fraction(-59997, 90000))
    assert result.changes[1].pts == 9001 and result.frames_analyzed == 4
    assert result.changes[0].score == 42.123
    assert result.engine_version == "ffmpeg version test-build"
    assert source.read_bytes() == b"immutable" and source.stat().st_mtime_ns == before
    with pytest.raises(FrozenInstanceError):
        result.start_pts = 0
    # This is a read-only FFmpeg invocation retaining source timestamps.
    command = seen[0]
    assert command[command.index("-map") + 1] == "0:0"
    assert "-copyts" in command and "-start_at_zero" not in command
    assert "-r" not in command and command[-3:] == ["-f", "null", "-"]


def test_missing_score_is_none_and_final_frame_cut_is_kept():
    parser = scenes._SceneParser(Fraction(1, 1000), 0)
    feed(parser, 5000, cut=True)
    feed(parser, 5400, score=None, cut=True)
    parser.finish()
    assert len(parser.changes) == 1
    assert parser.changes[0].score is None
    assert parser.changes[0].timestamp_seconds == 0.4


def test_no_cuts_is_successful_empty_sequence():
    parser = scenes._SceneParser(Fraction(1, 1000), 0)
    feed(parser, 0, score="0")
    feed(parser, 40, score="0")
    parser.finish()
    assert parser.frames == 2 and parser.changes == []


def test_minimum_interval_uses_pts_and_previously_accepted_cut():
    parser = scenes._SceneParser(Fraction(1, 1000), 1)
    for pts in (5000, 5500, 6000, 6300, 7000):
        feed(parser, pts, cut=True)
    parser.finish()
    assert [cut.pts for cut in parser.changes] == [6000, 7000]


@pytest.mark.parametrize("line", [
    "frame:0 pts:NOPTS pts_time:NOPTS", "frame:0 pts:1.2 pts_time:0.0",
    "frame:0 pts:NaN pts_time:0", "frame:0 pts:1", "lavfi.scd.score=42",
])
def test_parser_rejects_missing_or_malformed_pts(line):
    with pytest.raises(scenes.SceneDetectionError):
        scenes._SceneParser(Fraction(1, 1000), 0).feed(line)


@pytest.mark.parametrize("line", [
    "lavfi.scd.score=nan", "lavfi.scd.score=inf", "lavfi.scd.score=-1",
    "lavfi.scd.score=101", "lavfi.scd.score=garbage", "lavfi.scd.time=nan",
])
def test_parser_rejects_invalid_metadata(line):
    parser = scenes._SceneParser(Fraction(1, 1000), 0)
    feed(parser, 0)
    with pytest.raises(scenes.SceneDetectionError):
        parser.feed(line)


def test_nonmonotonic_pts_are_visible_error():
    parser = scenes._SceneParser(Fraction(1, 1000), 0)
    feed(parser, 100)
    with pytest.raises(scenes.SceneDetectionError, match="monótonos"):
        feed(parser, 99)


@pytest.mark.parametrize("headers", [[], ["frame:0 pts:0 pts_time:0"]])
def test_empty_or_missing_scd_output_is_not_zero_cuts(headers):
    parser = scenes._SceneParser(Fraction(1, 1000), 0)
    for line in headers:
        parser.feed(line)
    with pytest.raises(scenes.SceneDetectionError):
        parser.finish()


def test_default_and_explicit_track_selection_excludes_cover_art(context):
    _, info = context
    info = replace(info, streams=(video(0, default=1, attached=1),
        {"index": 1, "codec_type": "audio"}, video(2), video(3, default=1)))
    assert scenes._select_stream(info, None)[0] == 3
    assert scenes._select_stream(info, 2)[0] == 2
    for index in (0, 1, 9):
        with pytest.raises(scenes.SceneDetectionError):
            scenes._select_stream(info, index)
    assert scenes._select_stream(replace(info, streams=(video(2), video(3))), None)[0] == 2


@pytest.mark.parametrize("streams", [(), ({"index": 0, "codec_type": "audio"},), (video(attached=1),)])
def test_no_playable_video(context, streams):
    with pytest.raises(scenes.SceneDetectionError, match="video"):
        scenes._select_stream(replace(context[1], streams=streams), None)


@pytest.mark.parametrize("base", [None, "0/1", "1/0", "-1/1000", "N/A"])
def test_missing_time_base_is_not_inferred_from_fps(context, base):
    with pytest.raises(scenes.SceneDetectionError, match="base de tiempo"):
        scenes._select_stream(replace(context[1], streams=(video(time_base=base),)), None)


@pytest.mark.parametrize("kwargs", [
    {"threshold": -1}, {"threshold": 101}, {"threshold": True}, {"threshold": float("nan")},
    {"threshold": "10"}, {"timeout": 0}, {"timeout": float("inf")}, {"timeout": True},
    {"min_scene_duration": -0.1}, {"min_scene_duration": float("nan")},
    {"video_stream": -1}, {"video_stream": True}, {"video_stream": 0.5},
    {"threads": 0}, {"threads": True},
])
def test_invalid_settings_fail_before_process_start(context, kwargs):
    with pytest.raises(ValueError):
        scenes.detect_scenes(context[0], **kwargs)


@pytest.mark.parametrize("path", ["https://example.com/video.mp4", "pipe:0", "file:/tmp/a.mp4", "-"])
def test_only_local_paths_are_accepted(path):
    with pytest.raises(MediaError):
        scenes.detect_scenes(path)


def test_metadata_of_another_source_is_rejected(context):
    source, info = context
    with pytest.raises(scenes.SceneDetectionError, match="otro archivo"):
        scenes.detect_scenes(source, media_info=replace(info, path=str(source.with_name("other.mp4"))))


def test_probe_error_diagnostics_are_not_ignored(context):
    source, info = context
    with pytest.raises(scenes.SceneDetectionError, match="FFprobe"):
        scenes.detect_scenes(source, media_info=replace(info, diagnostics=({"severity": "ERROR", "message": "bad"},)))


@pytest.mark.parametrize("error", [DependencyError("missing ffmpeg"), MediaError("timeout"), MediaError("No such filter: scdet")])
def test_process_failures_are_not_empty_success(context, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(scenes, "stream_media_command", fail)
    with pytest.raises(MediaError, match=str(error)):
        scenes.detect_scenes(context[0])


def test_successful_exit_with_decoder_stderr_is_failure(context, monkeypatch):
    monkeypatch.setattr(scenes, "stream_media_command", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, "", "corrupt frame"))
    with pytest.raises(scenes.SceneDetectionError, match="corrupt frame"):
        scenes.detect_scenes(context[0])


def test_timeout_is_shared_across_preparation_and_decode(context, monkeypatch):
    ticks = iter((0, 0, 0.2, 1.1))
    monkeypatch.setattr(scenes, "monotonic", lambda: next(ticks))
    with pytest.raises(scenes.SceneDetectionError, match="timeout"):
        scenes.detect_scenes(context[0], timeout=1)
