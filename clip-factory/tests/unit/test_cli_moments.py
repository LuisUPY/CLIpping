"""CLI application boundaries; analyzer/result doubles are explicit contracts."""

from dataclasses import dataclass, replace
import hashlib
import json

import pytest

from clip_factory.analysis.scenes import SceneChange, SceneDetectionResult
from clip_factory.cli import main as cli
from clip_factory.config.loader import ConfigError, load_config
from clip_factory.database.repository import Repository
from clip_factory.media.ffmpeg import MediaError
from clip_factory.media.probe import MediaInfo


@dataclass(frozen=True)
class CandidateDouble:
    start: float = 3.0
    end: float = 9.0
    duration: float = 6.0
    reasons: tuple = ("scene_change", "audio_peak")
    evidence: tuple = ()


@dataclass(frozen=True)
class MomentResultDouble:
    source: str
    moments: tuple = (CandidateDouble(),)
    warnings: tuple = ()
    status: str = "PASS"
    elapsed_seconds: float = 0.001


@dataclass(frozen=True)
class AudioDouble:
    status: str = "PASS"
    warnings: tuple = ()
    events: tuple = ()


@dataclass(frozen=True)
class TranscriptDouble:
    status: str = "SKIPPED"
    skip_reason: str = "model_unavailable"
    warnings: tuple = ("Sin modelo",)
    segments: tuple = ()


def scene(source, origin=0, cuts=(5, 15)):
    return SceneDetectionResult(str(source), 0, "1/1000", int(origin * 1000),
        tuple(SceneChange(int((origin + cut) * 1000), cut, origin + cut, 80) for cut in cuts),
        500, 10, 0, 300, 4, "ffmpeg test", 0.01)


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "data/input/video.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable original")
    info = MediaInfo(str(source), 20, 160, 90, 25, "25/1", True, "h264", (
        {"index": 0, "codec_type": "video", "time_base": "1/1000", "start_pts": 0, "duration_ts": 20000},
        {"index": 1, "codec_type": "audio"},
    ), format_name="mov,mp4")
    seen = []
    calls = {}
    monkeypatch.setattr(cli, "probe_media", lambda *a, **k: info)

    def scenes(*a, **k):
        seen.append("scenes"); calls["scenes"] = k
        return scene(source)

    def audio(*a, **k):
        seen.append("audio"); calls["audio"] = k
        return AudioDouble()

    def transcript(*a, **k):
        seen.append("transcription"); calls["transcription"] = k
        return TranscriptDouble()

    def moments(**k):
        seen.append("moments"); calls["moments"] = k
        return MomentResultDouble(str(source))

    for name, function in (("detect_scenes", scenes), ("analyze_audio", audio),
                           ("transcribe_media", transcript), ("detect_moments", moments)):
        monkeypatch.setattr(cli, name, function)
    return source, ["--root", str(tmp_path), "analyze", str(source)], seen, calls


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@pytest.mark.parametrize("extras", [[], ["--scenes", "--audio"]])
def test_moments_reuses_each_required_analyzer_once_and_preserves_original(context, tmp_path, capsys, extras):
    source, command, seen, calls = context
    before = signature(source)
    assert cli.main([*command, "--moments", *extras, "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen == ["scenes", "audio", "moments"]
    assert payload["schema_version"] == 5
    assert payload["scope"] == "metadata_and_scenes_and_audio_and_moments"
    assert payload["moments"][0]["start"] == 3 and payload["moments"][0]["duration"] == 6
    assert "moments" not in payload["moment_detection"]
    assert payload["moment_detection"]["duration_source"] == "selected_video_duration_ts"
    assert payload["analysis"]["moments"] == "completed"
    assert payload["analysis"]["transcription"] == "not_run"
    assert calls["moments"]["duration"] == 20 and calls["moments"]["origin_seconds"] == 0
    repo = Repository(tmp_path / "database/clip_factory.db", root=tmp_path, input_dir=source.parent)
    assert repo.list_probes()[0].metadata == payload and signature(source) == before


def test_transcription_optional_skip_is_passed_to_detector_without_retry(context, capsys):
    _, command, seen, calls = context
    assert cli.main([*command, "--moments", "--transcribe", "--output", "-"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert seen == ["scenes", "audio", "transcription", "moments"]
    assert calls["moments"]["transcript"].status == "SKIPPED"
    assert payload["analysis"]["transcription"] == "skipped_model_unavailable"
    assert payload["analysis"]["moments"] == "completed" and "Sin modelo" in captured.err


def test_detect_alias_uses_same_pipeline_and_logs_invoked_name(context, tmp_path, capsys):
    source, _, seen, _ = context
    assert cli.main(["--root", str(tmp_path), "detect", str(source), "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 5 and seen == ["scenes", "audio", "moments"]
    logs = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert logs[-1]["operation"] == "detect"


def test_modes_without_moments_keep_previous_json_and_do_not_call_detector(context, capsys):
    _, command, seen, _ = context
    assert cli.main([*command, "--scenes", "--audio", "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 3 and payload["analysis"]["moments"] == "not_run"
    assert "moments" not in payload and seen == ["scenes", "audio"]


@pytest.mark.parametrize("option,value", [("--moment-min-duration", "6"), ("--moment-min-sources", "3"),
                                          ("--moment-timeout", "1"), ("--moment-max-events", "500")])
def test_moment_options_require_opt_in(context, capsys, option, value):
    _, command, seen, _ = context
    assert cli.main([*command, option, value, "--output", "-"]) == 1
    assert "requieren --moments" in capsys.readouterr().err and seen == []


def test_yaml_and_cli_overrides_apply_to_rules_only(context, tmp_path, capsys):
    _, command, _, calls = context
    config = tmp_path / "config/default.yaml"; config.parent.mkdir()
    config.write_text("analysis:\n  moments:\n    min_duration: 6\n    max_duration: 30\n    cooldown: 15\n    min_sources: 3\n")
    assert cli.main([*command, "--moments", "--moment-min-duration", "7", "--moment-timeout", "2", "--output", "-"]) == 0
    capsys.readouterr()
    rule_config = calls["moments"]["config"]
    assert rule_config.min_duration == 7 and rule_config.max_duration == 30
    assert rule_config.cooldown == 15 and rule_config.min_sources == 3 and rule_config.timeout == 2
    assert calls["scenes"]["timeout"] == 300 and calls["audio"]["timeout"] == 300


@pytest.mark.parametrize("option,value", [("--moment-min-sources", "1"), ("--moment-min-duration", "0"),
                                          ("--moment-timeout", "nan"), ("--moment-max-candidates", "0")])
def test_invalid_options_fail_before_probe_or_decoders(context, monkeypatch, capsys, option, value):
    _, command, seen, _ = context
    monkeypatch.setattr(cli, "probe_media", lambda *a, **k: pytest.fail("Must validate before probe"))
    assert cli.main([*command, "--moments", option, value, "--output", "-"]) == 1
    assert capsys.readouterr().err and seen == []


@pytest.mark.parametrize("key,value", [("min_sources", "1"), ("context_before", "-1"),
                                       ("timeout", ".inf"), ("max_events", "true"), ("min_duration", "100")])
def test_invalid_moment_yaml_rejected_without_media_commands(tmp_path, key, value):
    config = tmp_path / "settings.yaml"
    config.write_text(f"analysis:\n  moments:\n    {key}: {value}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, config_path=config)


@pytest.mark.parametrize("error", ["Moment Detection timeout", "invalid timeline"])
def test_rule_failure_logs_error_and_does_not_publish_partial(context, tmp_path, monkeypatch, capsys, error):
    source, command, _, _ = context
    before = signature(source)

    def fail(**kwargs):
        raise MediaError(error)

    monkeypatch.setattr(cli, "detect_moments", fail)
    assert cli.main([*command, "--moments", "--output", "data/working/report.json"]) == 1
    assert error in capsys.readouterr().err
    assert not (tmp_path / "data/working/report.json").exists()
    assert not (tmp_path / "database/clip_factory.db").exists() and signature(source) == before
    logs = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert next(entry for entry in logs if entry["operation"] == "moment_detection")["result"] == "FAIL"


def test_invalid_video_metadata_blocks_moment_pipeline_before_decoders(context, tmp_path, monkeypatch, capsys):
    source, command, seen, _ = context
    monkeypatch.setattr(cli, "probe_media", lambda *a, **k: MediaInfo(
        str(source), None, None, None, None, None, False, None, ()))
    assert cli.main([*command, "--moments", "--output", "data/working/report.json"]) == 1
    assert "metadata de video válida" in capsys.readouterr().err and seen == []
    assert not (tmp_path / "data/working/report.json").exists()
    assert not (tmp_path / "database/clip_factory.db").exists()


def test_interrupted_publication_recovers_without_rerunning_analysis(context, tmp_path, monkeypatch, capsys):
    source, command, seen, _ = context
    original_write = cli.write_json_new

    def interrupted(destination, *a, **k):
        if destination.name == "report.json":
            raise SystemExit(73)
        return original_write(destination, *a, **k)

    monkeypatch.setattr(cli, "write_json_new", interrupted)
    with pytest.raises(SystemExit):
        cli.main([*command, "--moments", "--output", "data/working/report.json"])
    monkeypatch.setattr(cli, "write_json_new", original_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "data/working/report.json").read_text())
    assert payload["moments"] and seen == ["scenes", "audio", "moments"]
    assert list((tmp_path / "data/working/.publications").glob("*.json")) == []


@pytest.mark.parametrize("origin", [-0.5, 0, 5])
@pytest.mark.parametrize("container", ["mp4", "matroska"])
def test_video_bounds_use_correct_span_vs_end_timestamp(origin, container):
    detected = scene("source", origin)
    stream = {"index": 0, "time_base": "1/1000", "start_pts": int(origin * 1000)}
    if container == "mp4":
        stream.update(duration_ts=20000, duration="20")
    else:
        stream["tags"] = {"DURATION": f"00:00:{20 + origin:012.9f}"}
    # Global duration deliberately does not describe selected video.
    info = MediaInfo("source", 100, 160, 90, 25, "25/1", True, "h264", (stream,), format_name=container)
    start, duration, provenance, warnings = cli._moment_video_bounds(info, detected)
    assert start == origin and duration == 20 and warnings == ()
    assert provenance == ("selected_video_duration_ts" if container == "mp4" else "matroska_declared_absolute_end")


def test_bounds_select_video_not_earliest_audio_or_global_duration():
    detected = scene("source", 7, cuts=(1,))
    info = MediaInfo("source", 100, 160, 90, 25, "25/1", True, "h264", (
        {"index": 1, "codec_type": "audio", "start_time": "0", "duration": "100"},
        {"index": 0, "codec_type": "video", "start_time": "7", "duration": "3"},
    ), start_time=0)
    assert cli._moment_video_bounds(info, detected)[:2] == (7, 3)


def test_unknown_bounds_do_not_guess_from_fps_global_duration_or_last_cut():
    info = MediaInfo("source", 100, 160, 90, 25, "25/1", False, "h264", ({"index": 0},))
    origin, duration, provenance, warnings = cli._moment_video_bounds(info, scene("source"))
    assert origin == 0 and duration is None and provenance == "unknown" and warnings


@pytest.mark.parametrize("stream", [
    {"duration_ts": 10000, "duration": "20"},
    {"duration_ts": 5000},
    {"duration": "0"},
])
def test_contradictory_or_impossible_metadata_bounds_fail(stream):
    info = MediaInfo("source", 20, 160, 90, 25, "25/1", False, "h264", (
        {"index": 0, "time_base": "1/1000", "start_pts": 0, **stream},))
    with pytest.raises(MediaError):
        cli._moment_video_bounds(info, scene("source"))
