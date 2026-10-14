"""Scene CLI stays opt-in and uses the existing safe publication lifecycle."""

import json

import pytest

from clip_factory.analysis.scenes import SceneChange, SceneDetectionResult
from clip_factory.cli import main as cli
from clip_factory.config.loader import ConfigError, load_config
from clip_factory.database.repository import Repository
from clip_factory.media.ffmpeg import MediaError
from clip_factory.media.probe import MediaInfo


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "data/input/original.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable")
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: MediaInfo(
        str(source), 3, 160, 90, 25, "25/1", False, "h264", ()))
    detection = SceneDetectionResult(
        str(source), 2, "1/1000", 5000, (SceneChange(6000, 1, 6, 42.1),),
        75, 10, 0, 300, 4, "ffmpeg version test", .25)
    calls = []
    def detect(*args, **kwargs):
        calls.append(kwargs)
        return detection
    monkeypatch.setattr(cli, "detect_scenes", detect)
    command = ["--root", str(tmp_path), "analyze", str(source)]
    return source, command, calls


def test_scene_options_override_yaml_without_changing_other_stages(context, tmp_path, capsys):
    source, command, calls = context
    config = tmp_path / "config/default.yaml"
    config.parent.mkdir()
    config.write_text("analysis:\n  scenes:\n    threshold: 30\n    timeout: 45\n    min_scene_duration: 0.4\n    video_stream: 2\n")
    assert cli.main([*command, "--scenes", "--scene-threshold", "8", "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls[0]["threshold"] == 8 and calls[0]["timeout"] == 45
    assert calls[0]["video_stream"] == 2 and calls[0]["min_scene_duration"] == .4
    assert calls[0]["media_info"].path == str(source) and calls[0]["threads"] == 4
    assert payload["analysis"]["audio"] == "not_run" and payload["scene_detection"]["status"] == "PASS"


@pytest.mark.parametrize("option,value", [("--scene-threshold", "8"), ("--scene-timeout", "2"), ("--video-stream", "0"), ("--min-scene-duration", "1")])
def test_scene_configuration_without_opt_in_is_rejected(context, capsys, option, value):
    _, command, calls = context
    assert cli.main([*command, option, value]) == 1
    assert "requieren --scenes" in capsys.readouterr().err
    assert calls == []


def test_metadata_mode_never_launches_detector(context, capsys):
    _, command, calls = context
    assert cli.main([*command, "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "metadata_only" and payload["schema_version"] == 1
    assert payload["analysis"]["scenes"] == "not_run" and calls == []


def test_failed_detector_does_not_publish_partial_json_or_database_record(context, tmp_path, monkeypatch, capsys):
    source, command, _ = context
    before = source.stat().st_mtime_ns
    def fail(*args, **kwargs):
        raise MediaError("Scene Detection timeout")
    monkeypatch.setattr(cli, "detect_scenes", fail)
    assert cli.main([*command, "--scenes", "--output", "data/working/report.json"]) == 1
    captured = capsys.readouterr()
    assert "timeout" in captured.err and captured.out == ""
    assert not (tmp_path / "data/working/report.json").exists()
    assert not (tmp_path / "database/clip_factory.db").exists()
    logs = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert [line["operation"] for line in logs] == ["scene_detection", "analyze"]
    assert all(line["result"] == "FAIL" for line in logs)
    assert source.read_bytes() == b"immutable" and source.stat().st_mtime_ns == before


@pytest.mark.parametrize("destination", ["data/input/report.json", "data/working/link.json"])
def test_unsafe_output_is_rejected_before_detector_runs(context, tmp_path, destination):
    source, command, calls = context
    if "link" in destination:
        link = tmp_path / destination
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
    assert cli.main([*command, "--scenes", "--output", destination]) == 1
    assert calls == [] and source.read_bytes() == b"immutable"


def test_failed_scene_publication_recovers_same_result_without_rerunning_analysis(context, tmp_path, monkeypatch, capsys):
    source, command, calls = context
    real_write = cli.write_json_new
    def interrupted(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise SystemExit(73)
        return real_write(destination, *args, **kwargs)
    monkeypatch.setattr(cli, "write_json_new", interrupted)
    with pytest.raises(SystemExit):
        cli.main([*command, "--scenes", "--output", "data/working/report.json"])
    monkeypatch.setattr(cli, "write_json_new", real_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    payload = json.loads((tmp_path / "data/working/report.json").read_text())
    repo = Repository(tmp_path / "database/clip_factory.db", root=tmp_path, input_dir=source.parent)
    assert repo.list_probes()[0].metadata == payload and len(calls) == 1
    assert payload["analysis"]["scenes"] == "completed" and payload["scene_detection"]["changes"][0]["pts"] == 6000


@pytest.mark.parametrize("key,value", [
    ("threshold", "-1"), ("threshold", "101"), ("threshold", ".nan"), ("threshold", "true"),
    ("timeout", "0"), ("timeout", ".inf"), ("min_scene_duration", "-1"),
    ("video_stream", "-1"), ("video_stream", "0.5"), ("video_stream", "true"),
])
def test_scene_yaml_rejects_invalid_settings(tmp_path, key, value):
    config = tmp_path / "custom.yaml"
    config.write_text(f"analysis:\n  scenes:\n    {key}: {value}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, config_path=config)
