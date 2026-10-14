"""Acoustic analysis is opt-in and shares safe publication with visual analysis."""

from dataclasses import dataclass, field
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
class AudioResultFixture:
    """Result fixture deliberately independent of signal detection internals."""

    source: str
    status: str = "PASS"
    stream_index: int | None = 1
    sample_rate: int | None = 8000
    channels: int | None = 2
    source_time_base: str | None = "1/1000"
    time_base: str | None = "1/8000"
    start_pts: int | None = 40000
    window_samples: int | None = 4000
    windows: tuple = ()
    events: tuple = ()
    samples_analyzed: int = 24000
    rms: float | None = .1
    rms_dbfs: float | None = -20.0
    peak_dbfs: float | None = -2.0
    config: dict = field(default_factory=lambda: {
        "window_seconds": .5, "peak_dbfs": -6.0, "silence_dbfs": -50.0,
        "change_db": 6.0, "min_silence_duration": .5,
        "energy_floor_dbfs": -120.0, "timeout": 300.0,
        "audio_stream": None, "max_windows": 100000, "max_events": 20000,
    })
    warnings: tuple[str, ...] = ()
    engine_version: str | None = "ffmpeg version test"
    elapsed_seconds: float = .125
    engine: str = "ffmpeg.astats"


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "data/input/original.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable acoustic and visual source")
    info = MediaInfo(str(source), 3, 160, 90, 25, "25/1", True, "h264", ())
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: info)
    calls = {"audio": [], "scenes": []}
    audio_result = AudioResultFixture(str(source))
    scene_result = SceneDetectionResult(
        str(source), 0, "1/1000", 5000, (SceneChange(6000, 1, 6, 42.1),),
        75, 10, 0, 300, 4, "ffmpeg version test", .25)

    def audio(*args, **kwargs):
        calls["audio"].append(kwargs)
        return audio_result

    def scenes(*args, **kwargs):
        calls["scenes"].append(kwargs)
        return scene_result

    monkeypatch.setattr(cli, "analyze_audio", audio)
    monkeypatch.setattr(cli, "detect_scenes", scenes)
    return source, ["--root", str(tmp_path), "analyze", str(source)], calls


@pytest.mark.parametrize("options,schema,scope,audio,scenes", [
    ([], 1, "metadata_only", "not_run", "not_run"),
    (["--scenes"], 2, "metadata_and_scenes", "not_run", "completed"),
    (["--audio"], 3, "metadata_and_audio", "completed", "not_run"),
    (["--scenes", "--audio"], 3, "metadata_and_scenes_and_audio", "completed", "completed"),
])
def test_four_analyze_modes_invoke_only_requested_engines(context, capsys, options, schema, scope, audio, scenes):
    source, command, calls = context
    before = signature(source)
    assert cli.main([*command, *options, "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == schema and payload["scope"] == scope
    assert payload["analysis"] == {
        "audio": audio, "scenes": scenes, "transcription": "not_run", "moments": "not_run",
    }
    assert len(calls["audio"]) == (audio == "completed")
    assert len(calls["scenes"]) == (scenes == "completed")
    assert ("audio_analysis" in payload) == (audio == "completed")
    assert ("scene_detection" in payload) == (scenes == "completed")
    if audio == "completed":
        assert payload["audio_analysis"]["engine"] == "ffmpeg.astats"
        assert payload["audio_analysis"]["timestamp_reference"] == "first_decoded_frame_of_selected_stream"
        assert payload["audio_analysis"]["dbfs_silence"] == "null_means_negative_infinity"
        assert payload["audio_analysis"]["rms_dbfs"] == -20
        assert payload["audio_analysis"]["source_time_base"] == "1/1000"
    assert signature(source) == before


def test_audio_cli_overrides_yaml_and_reuses_probe_and_runtime(context, tmp_path, capsys):
    source, command, calls = context
    config = tmp_path / "config/default.yaml"
    config.parent.mkdir()
    config.write_text(
        "analysis:\n  audio:\n    window_seconds: 0.2\n    peak_dbfs: -10\n"
        "    silence_dbfs: -40\n    change_db: 8\n    min_silence_duration: 0.4\n"
        "    timeout: 45\n    audio_stream: 3\n    max_windows: 500\n"
        "    max_events: 25\n    energy_floor_dbfs: -180\n",
        encoding="utf-8",
    )
    assert cli.main([
        *command, "--audio", "--audio-window", "0.25", "--audio-peak-threshold", "-7",
        "--audio-silence-threshold", "-45", "--audio-change-threshold", "9",
        "--audio-min-silence", "0.75", "--audio-timeout", "20", "--audio-stream", "1",
        "--audio-max-windows", "600", "--audio-max-events", "30", "--audio-energy-floor", "-150",
        "--output", "-",
    ]) == 0
    json.loads(capsys.readouterr().out)
    expected = {
        "window_seconds": .25, "peak_dbfs": -7, "silence_dbfs": -45,
        "change_db": 9, "min_silence_duration": .75, "timeout": 20,
        "audio_stream": 1, "max_windows": 600, "max_events": 30,
        "energy_floor_dbfs": -150,
    }
    assert {key: calls["audio"][0][key] for key in expected} == expected
    assert calls["audio"][0]["media_info"].path == str(source)
    assert calls["audio"][0]["threads"] == 4
    assert calls["scenes"] == []


def test_audio_yaml_applies_without_cli_overrides(context, tmp_path, capsys):
    _, command, calls = context
    config = tmp_path / "config/default.yaml"
    config.parent.mkdir()
    config.write_text("analysis:\n  audio:\n    timeout: 45\n    window_seconds: 0.2\n    audio_stream: 3\n")
    assert cli.main([*command, "--audio", "--output", "-"]) == 0
    capsys.readouterr()
    assert calls["audio"][0]["timeout"] == 45
    assert calls["audio"][0]["window_seconds"] == .2
    assert calls["audio"][0]["audio_stream"] == 3
    assert calls["audio"][0]["peak_dbfs"] == -6


@pytest.mark.parametrize("option,value", [
    ("--audio-window", ".2"), ("--audio-peak-threshold", "-4"),
    ("--audio-silence-threshold", "-45"), ("--audio-change-threshold", "8"),
    ("--audio-min-silence", "1"), ("--audio-timeout", "2"),
    ("--audio-stream", "1"), ("--audio-max-windows", "50"),
    ("--audio-max-events", "10"), ("--audio-energy-floor", "-150"),
])
def test_audio_configuration_requires_opt_in(context, capsys, option, value):
    _, command, calls = context
    assert cli.main([*command, option, value, "--output", "-"]) == 1
    assert "requieren --audio" in capsys.readouterr().err
    assert calls == {"audio": [], "scenes": []}


@pytest.mark.parametrize("options", [["--audio"], ["--audio", "--scenes"]])
@pytest.mark.parametrize("error", ["Audio Analysis timeout", "audio corrupto"])
def test_failed_audio_does_not_publish_partial_results(context, tmp_path, monkeypatch, capsys, options, error):
    source, command, _ = context
    before = signature(source)

    def fail(*args, **kwargs):
        raise MediaError(error)

    monkeypatch.setattr(cli, "analyze_audio", fail)
    assert cli.main([*command, *options, "--output", "data/working/report.json"]) == 1
    captured = capsys.readouterr()
    assert error in captured.err and captured.out == ""
    assert not (tmp_path / "data/working/report.json").exists()
    assert not (tmp_path / "database/clip_factory.db").exists()
    logs = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert next(log for log in logs if log["operation"] == "audio_analysis")["result"] == "FAIL"
    assert next(log for log in logs if log["operation"] == "analyze")["result"] == "FAIL"
    assert signature(source) == before


def test_audio_failure_does_not_affect_separate_scene_analysis(context, monkeypatch, capsys):
    _, command, calls = context

    def fail(*args, **kwargs):
        raise MediaError("audio unavailable")

    monkeypatch.setattr(cli, "analyze_audio", fail)
    assert cli.main([*command, "--scenes", "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scene_detection"]["status"] == "PASS"
    assert payload["analysis"]["audio"] == "not_run" and len(calls["scenes"]) == 1


@pytest.mark.parametrize("destination", ["data/input/report.json", "data/working/link.json"])
def test_audio_unsafe_output_is_rejected_before_engines(context, tmp_path, destination):
    source, command, calls = context
    before = signature(source)
    if "link" in destination:
        link = tmp_path / destination
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
    assert cli.main([*command, "--audio", "--scenes", "--output", destination]) == 1
    assert calls == {"audio": [], "scenes": []} and signature(source) == before


@pytest.mark.parametrize("scenes", [False, True])
def test_interrupted_audio_publication_recovers_without_reanalysis(context, tmp_path, monkeypatch, capsys, scenes):
    source, command, calls = context
    before = signature(source)
    real_write = cli.write_json_new

    def interrupted(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise SystemExit(73)
        return real_write(destination, *args, **kwargs)

    monkeypatch.setattr(cli, "write_json_new", interrupted)
    options = ["--audio", "--output", "data/working/report.json"]
    if scenes:
        options.append("--scenes")
    with pytest.raises(SystemExit):
        cli.main([*command, *options])
    monkeypatch.setattr(cli, "write_json_new", real_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "data/working/report.json").read_text())
    repo = Repository(tmp_path / "database/clip_factory.db", root=tmp_path, input_dir=source.parent)
    assert repo.list_probes()[0].metadata == payload
    assert len(calls["audio"]) == 1 and len(calls["scenes"]) == scenes
    assert payload["schema_version"] == 3 and payload["analysis"]["audio"] == "completed"
    assert list((tmp_path / "data/working/.publications").glob("*.json")) == []
    assert signature(source) == before


def test_no_audio_is_skipped_with_warning_and_visual_analysis_completes(context, monkeypatch, capsys):
    source, command, calls = context
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: MediaInfo(
        str(source), 3, 160, 90, 25, "25/1", False, "h264", ()))
    monkeypatch.setattr(cli, "analyze_audio", lambda *args, **kwargs: AudioResultFixture(
        str(source), status="SKIPPED", stream_index=None, sample_rate=None, channels=None,
        source_time_base=None, time_base=None, start_pts=None, window_samples=None,
        samples_analyzed=0, rms=None, rms_dbfs=None, peak_dbfs=None,
        warnings=("El archivo no contiene audio.",), engine_version=None,
    ))
    assert cli.main([*command, "--scenes", "--audio", "--output", "-"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["analysis"]["audio"] == "skipped_no_audio"
    assert payload["analysis"]["scenes"] == "completed" and len(calls["scenes"]) == 1
    assert payload["audio_analysis"]["status"] == "SKIPPED"
    assert payload["audio_analysis"]["rms_dbfs"] is None
    assert "audio" in captured.err and "Advertencia" in captured.err


@pytest.mark.parametrize("key,value", [
    ("window_seconds", "0"), ("window_seconds", "0.001"), ("window_seconds", "10.1"),
    ("window_seconds", ".nan"), ("window_seconds", "true"),
    ("peak_dbfs", "1"), ("peak_dbfs", ".inf"), ("silence_dbfs", "1"),
    ("silence_dbfs", ".nan"), ("change_db", "0"), ("change_db", "-1"),
    ("min_silence_duration", "-1"), ("timeout", "0"), ("timeout", ".inf"),
    ("audio_stream", "-1"), ("audio_stream", "0.5"), ("audio_stream", "true"),
    ("max_windows", "0"), ("max_windows", "1.5"), ("max_events", "0"),
    ("max_events", "true"), ("energy_floor_dbfs", "1"), ("energy_floor_dbfs", "-119"),
    ("energy_floor_dbfs", "-301"), ("energy_floor_dbfs", ".nan"),
])
def test_audio_yaml_rejects_invalid_configuration(tmp_path, key, value):
    config = tmp_path / "custom.yaml"
    config.write_text(f"analysis:\n  audio:\n    {key}: {value}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, config_path=config)


@pytest.mark.parametrize("settings", [
    "peak_dbfs: -50\n    silence_dbfs: -50",
    "peak_dbfs: -60\n    silence_dbfs: -50",
])
def test_audio_yaml_requires_silence_threshold_below_peak(tmp_path, settings):
    config = tmp_path / "custom.yaml"
    config.write_text(f"analysis:\n  audio:\n    {settings}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, config_path=config)


def test_probe_warning_is_reported_once_when_audio_reuses_it(context, tmp_path, monkeypatch, capsys):
    source, command, _ = context
    warning = 'Advertencia de probe conservada'
    info = MediaInfo(str(source), 3, 160, 90, 25, '25/1', True, 'h264', (),
                     diagnostics=({'severity': 'WARNING', 'message': warning},))
    monkeypatch.setattr(cli, 'probe_media', lambda *args, **kwargs: info)
    monkeypatch.setattr(cli, 'analyze_audio', lambda *args, **kwargs: AudioResultFixture(
        str(source), warnings=(warning,)))
    assert cli.main([*command, '--audio', '--output', '-']) == 0
    output = capsys.readouterr()
    assert output.err.count(warning) == 1
    logs = [json.loads(line) for line in (tmp_path/'logs/clip_factory.jsonl').read_text().splitlines()]
    assert next(log for log in logs if log['operation'] == 'analyze')['warnings'] == [warning]


def test_invalid_audio_options_fail_before_running_either_decoder(context, tmp_path, monkeypatch, capsys):
    _, command, calls = context
    def no_probe(*args, **kwargs):
        pytest.fail('Invalid settings must be rejected before even probing')
    monkeypatch.setattr(cli, 'probe_media', no_probe)
    assert cli.main([*command, '--scenes', '--audio', '--audio-window', '0', '--output', '-']) == 1
    assert calls == {'audio': [], 'scenes': []}
    assert 'window_seconds' in capsys.readouterr().err
    assert not (tmp_path/'database/clip_factory.db').exists()
