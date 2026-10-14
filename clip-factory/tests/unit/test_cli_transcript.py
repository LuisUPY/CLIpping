"""Application contract tests use explicit result doubles, never claim real ASR."""

from dataclasses import dataclass
import hashlib
import json

import pytest

from clip_factory.cli import main as cli
from clip_factory.config.loader import ConfigError, load_config
from clip_factory.database.repository import Repository
from clip_factory.media.ffmpeg import MediaError
from clip_factory.media.probe import MediaInfo


@dataclass(frozen=True)
class TranscriptDouble:
    source: str
    status: str = "PASS"
    skip_reason: str | None = None
    language: str | None = "es"
    model: str | None = "local-model.bin"
    engine: str = "whisper.cpp"
    engine_version: str | None = None
    stream_index: int | None = 1
    segments: tuple = ()
    warnings: tuple = ()
    elapsed_seconds: float = .2
    audio_processing_seconds: float = .05
    inference_seconds: float = .15


@dataclass(frozen=True)
class SceneDouble:
    changes: tuple = ()


@dataclass(frozen=True)
class AudioDouble:
    status: str = "PASS"
    warnings: tuple = ()
    events: tuple = ()


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "data/input/original.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original unchanged")
    info = MediaInfo(str(source), 3, 160, 90, 25, "25/1", True, "h264", ())
    monkeypatch.setattr(cli, "probe_media", lambda *a, **k: info)
    calls = []

    def transcribe(*args, **kwargs):
        calls.append(kwargs)
        return TranscriptDouble(str(source))

    monkeypatch.setattr(cli, "transcribe_media", transcribe)
    return source, ["--root", str(tmp_path), "analyze", str(source)], calls


def signature(source):
    return hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns


def records(tmp_path):
    return Repository(tmp_path / "database/clip_factory.db", root=tmp_path,
                      input_dir=tmp_path / "data/input").list_probes()


def test_opt_in_schema_persistence_logging_and_originals(context, tmp_path, capsys):
    source, command, calls = context
    before = signature(source)
    assert cli.main([*command, "--transcribe", "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 4
    assert payload["scope"] == "metadata_and_transcription"
    assert payload["analysis"] == {
        "scenes": "not_run", "audio": "not_run", "transcription": "completed", "moments": "not_run",
    }
    assert payload["transcription"]["language"] == "es"
    assert payload["transcription"]["timestamp_reference"] == "first_decoded_frame_of_selected_stream"
    assert records(tmp_path)[0].metadata == payload
    assert len(calls) == 1 and calls[0]["media_info"].path == str(source)
    assert calls[0]["root"] == tmp_path and calls[0]["working_dir"] == tmp_path / "data/working"
    assert calls[0]["threads"] == 4 and signature(source) == before
    logs = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert {entry["operation"] for entry in logs} == {"transcription", "analyze"}
    assert all(entry["result"] == "PASS" for entry in logs)


def test_transcription_is_not_called_without_opt_in(context, capsys):
    _, command, calls = context
    assert cli.main([*command, "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1 and "transcription" not in payload
    assert payload["analysis"]["transcription"] == "not_run" and calls == []


@pytest.mark.parametrize("options,scope,order", [
    (["--scenes"], "metadata_and_scenes_and_transcription", ["scenes", "transcription"]),
    (["--audio"], "metadata_and_audio_and_transcription", ["audio", "transcription"]),
    (["--scenes", "--audio"], "metadata_and_scenes_and_audio_and_transcription", ["scenes", "audio", "transcription"]),
])
def test_combined_stages_run_sequentially_and_missing_engine_preserves_other_results(
    context, monkeypatch, capsys, options, scope, order,
):
    source, command, _ = context
    seen = []

    def scenes(*a, **k):
        seen.append("scenes")
        return SceneDouble()

    def audio(*a, **k):
        seen.append("audio")
        return AudioDouble()

    def transcript(*a, **k):
        seen.append("transcription")
        return TranscriptDouble(str(source), status="SKIPPED", skip_reason="engine_unavailable",
                                language=None, warnings=("Motor ausente",))

    monkeypatch.setattr(cli, "detect_scenes", scenes)
    monkeypatch.setattr(cli, "analyze_audio", audio)
    monkeypatch.setattr(cli, "transcribe_media", transcript)
    assert cli.main([*command, *options, "--transcribe", "--output", "-"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == scope and payload["schema_version"] == 4 and seen == order
    assert payload["analysis"]["transcription"] == "skipped_engine_unavailable"
    if "--scenes" in options:
        assert payload["analysis"]["scenes"] == "completed"
    if "--audio" in options:
        assert payload["analysis"]["audio"] == "completed"


@pytest.mark.parametrize("reason", ["no_audio", "engine_unavailable", "model_unavailable"])
def test_unavailable_transcription_warns_and_does_not_block_publication(context, tmp_path, monkeypatch, capsys, reason):
    source, command, _ = context
    monkeypatch.setattr(cli, "transcribe_media", lambda *a, **k: TranscriptDouble(
        str(source), status="SKIPPED", skip_reason=reason, language=None, model=None,
        warnings=("Transcripción omitida: " + reason,)))
    assert cli.main([*command, "--transcribe", "--output", "data/working/report.json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads((tmp_path / "data/working/report.json").read_text())
    assert payload["analysis"]["transcription"] == f"skipped_{reason}"
    assert payload["transcription"]["status"] == "SKIPPED"
    assert "Advertencia" in captured.err
    assert records(tmp_path)[0].metadata == payload


@pytest.mark.parametrize("option,value", [
    ("--transcript-engine", "whisper.cpp"), ("--transcript-executable", "whisper-cli"),
    ("--transcript-model", "models/base.bin"), ("--transcript-language", "es"),
    ("--transcript-timeout", "30"), ("--transcript-audio-stream", "1"),
    ("--transcript-chunk-seconds", "20"), ("--transcript-max-segments", "100"),
    ("--transcript-silence-threshold", "-60"),
])
def test_options_require_explicit_opt_in(context, capsys, option, value):
    _, command, calls = context
    assert cli.main([*command, option, value, "--output", "-"]) == 1
    assert "requieren --transcribe" in capsys.readouterr().err and calls == []


def test_yaml_overrides_paths_and_track_are_independent(context, tmp_path, capsys):
    _, command, calls = context
    config = tmp_path / "config/default.yaml"
    config.parent.mkdir()
    config.write_text("analysis:\n  transcription:\n    language: en\n    timeout: 40\n"
                      "    model: models/base.bin\n    audio_stream: 3\n    executable: bin/whisper-cli\n"
                      "    chunk_seconds: 20\n    max_segments: 500\n    silence_dbfs: -60\n")
    assert cli.main([*command, "--transcribe", "--transcript-language", "es",
                     "--transcript-timeout", "25", "--transcript-audio-stream", "1", "--output", "-"]) == 0
    capsys.readouterr()
    settings = calls[0]["config"]
    assert settings.language == "es" and settings.timeout == 25 and settings.audio_stream == 1
    assert settings.model == str(tmp_path / "models/base.bin")
    assert settings.executable == str(tmp_path / "bin/whisper-cli")
    assert settings.chunk_seconds == 20 and settings.max_segments == 500 and settings.silence_dbfs == -60


@pytest.mark.parametrize("option,value", [("--transcript-timeout", "nan"),
                                          ("--transcript-chunk-seconds", "0"),
                                          ("--transcript-language", "fr"),
                                          ("--transcript-model", "https://example.invalid/model.bin"),
                                          ("--transcript-engine", "mlx-whisper")])
def test_invalid_config_fails_before_probe(context, monkeypatch, capsys, option, value):
    _, command, calls = context
    monkeypatch.setattr(cli, "probe_media", lambda *a, **k: pytest.fail("Must validate before probe"))
    assert cli.main([*command, "--transcribe", option, value, "--output", "-"]) == 1
    assert capsys.readouterr().err and calls == []


@pytest.mark.parametrize("key,value", [("timeout", "0"), ("timeout", ".nan"),
                                       ("audio_stream", "true"), ("model", "12"),
                                       ("executable", "''"), ("language", "fr"),
                                       ("engine", "faster-whisper"), ("max_segments", "0")])
def test_invalid_yaml_is_rejected(tmp_path, key, value):
    config = tmp_path / "settings.yaml"
    config.write_text(f"analysis:\n  transcription:\n    {key}: {value}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, config_path=config)


@pytest.mark.parametrize("error", ["transcription timeout", "engine failure"])
def test_failure_does_not_publish_partial_success(context, tmp_path, monkeypatch, capsys, error):
    source, command, _ = context
    before = signature(source)

    def fail(*a, **k):
        raise MediaError(error)

    monkeypatch.setattr(cli, "transcribe_media", fail)
    assert cli.main([*command, "--transcribe", "--output", "data/working/report.json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and error in captured.err
    assert not (tmp_path / "data/working/report.json").exists()
    assert not (tmp_path / "database/clip_factory.db").exists() and signature(source) == before


def test_recovery_reuses_saved_transcript_without_inference(context, tmp_path, monkeypatch, capsys):
    _, command, calls = context
    real_write = cli.write_json_new

    def interrupted(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise SystemExit(73)
        return real_write(destination, *args, **kwargs)

    monkeypatch.setattr(cli, "write_json_new", interrupted)
    with pytest.raises(SystemExit):
        cli.main([*command, "--transcribe", "--output", "data/working/report.json"])
    monkeypatch.setattr(cli, "write_json_new", real_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "data/working/report.json").read_text())
    assert records(tmp_path)[0].metadata == payload and len(calls) == 1


def test_doctor_checks_optional_files_without_running_or_loading_model(tmp_path, monkeypatch):
    report = {"checks": {}, "warnings": [], "status": "PASS"}
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cli._transcript_diagnostics(report, load_config(tmp_path).data["analysis"]["transcription"], tmp_path)
    assert report["status"] == "PASS"
    for name in ("transcription_engine", "transcription_model"):
        assert report["checks"][name]["status"] == "OPTIONAL"
        assert report["checks"][name]["required"] is False
        assert report["checks"][name]["inference_validated"] is False
