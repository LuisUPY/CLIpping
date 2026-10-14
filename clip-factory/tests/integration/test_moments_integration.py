"""Real local multimedia signals combined by the deterministic moment detector.

Synthetic cuts/tones verify timing and plumbing, never narrative quality. The
explicit TranscriptResult fixture tests the contract, not speech recognition.
The final opt-in check alone invokes the installed ASR on existing permitted
TTS reference media. No test downloads a model or modifies an input.
"""

from dataclasses import asdict, replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata

import pytest

from clip_factory.analysis.audio import analyze_audio
from clip_factory.analysis.moments import (
    MomentDetectionConfig,
    MomentDetectionError,
    detect_moments,
)
from clip_factory.analysis.scenes import detect_scenes
from clip_factory.analysis.transcript import TranscriptConfig, TranscriptResult, TranscriptSegment
from clip_factory.media.ffmpeg import run_media_command
from clip_factory.media.probe import probe_media

pytestmark = pytest.mark.integration


@pytest.fixture
def tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Moment Detection requiere FFmpeg/FFprobe locales; no se instalan automáticamente.")
    return ffmpeg, ffprobe


def signature(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest(), path.stat().st_mtime_ns


def synthesize(root, tools, *, audio="coincident", cuts=True, rate=8000,
               video_origin=0.0, audio_origin=None, dense=False, discontinuity=False):
    """Two known visual cuts and bounded acoustic onsets, with original PTS."""
    source = root / "data/input/original momentos local.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    audio_origin = video_origin if audio_origin is None else audio_origin
    visual = "color=black:s=160x90:r=25:d=20"
    if cuts:
        enabled = "mod(floor(t*2),2)" if dense else "gte(t,5)*lt(t,15)"
        visual += f",drawbox=color=white:t=fill:enable='{enabled}'"
    visual += f",settb=1/1000,setpts=PTS+({video_origin})/TB"
    command = [tools[0], "-nostdin", "-hide_banner", "-v", "error", "-n", "-copyts",
               "-f", "lavfi", "-i", visual]
    if audio != "absent":
        if audio == "silence":
            expression = "0"
        elif audio == "constant":
            expression = "0.8*sin(2*PI*440*t)"
        else:
            # Match absolute source time even when the audio starts after video.
            shift = audio_origin - video_origin
            if dense:
                gate = "mod(floor(t*2),2)"
            else:
                first, second = (2, 12) if audio == "separate" else (5, 15)
                first, second = first-shift, second-shift
                gate = f"gte(t,{first})*lt(t,{first+1})+gte(t,{second})*lt(t,{second+1})"
            expression = f"if({gate},0.8,0.02)*sin(2*PI*440*t)"
        acoustic = f"aevalsrc='{expression}':s={rate}:d=20,asetpts=PTS+({audio_origin})/TB"
        if discontinuity:
            acoustic += ",asetpts='PTS+gte(T,10)*2/TB'"
        command += ["-f", "lavfi", "-i", acoustic]
    command += ["-map", "0:v"]
    if audio != "absent":
        command += ["-map", "1:a"]
    command += ["-c:v", "ffv1", "-enc_time_base:v", "1/1000", "-c:a", "pcm_f32le", "-threads", "1",
                "-fps_mode", "passthrough", "-avoid_negative_ts", "disabled", str(source)]
    run_media_command(command, timeout=30)
    folder = root / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "default.yaml").write_text(
        f"media:\n  ffmpeg_bin: {json.dumps(tools[0])}\n  ffprobe_bin: {json.dumps(tools[1])}\n",
        encoding="utf-8",
    )
    return source


def analyze_sources(path, tools):
    info = probe_media(path, ffprobe_bin=tools[1])
    scenes = detect_scenes(path, media_info=info, ffmpeg_bin=tools[0], ffprobe_bin=tools[1])
    audio = analyze_audio(path, media_info=info, ffmpeg_bin=tools[0], ffprobe_bin=tools[1])
    return scenes, audio


def combine(path, scenes=None, audio=None, transcript=None, **settings):
    origin = float(Fraction(scenes.start_pts) * Fraction(scenes.time_base)) if scenes else 0.0
    return detect_moments(source=str(path.resolve()), duration=20.0, origin_seconds=origin,
                          scenes=scenes, audio=audio, transcript=transcript,
                          config=MomentDetectionConfig(**settings))


def transcript_contract(path, origin=0.0, *, start=4.0, end=7.0, text=None):
    """An explicit contract fixture. It neither runs nor claims to run ASR."""
    segment = TranscriptSegment(
        text or "Esta muestra controlada contiene suficientes palabras para probar el contrato.",
        start, end, origin+start, origin+end,
    )
    return TranscriptResult(
        source=str(path.resolve()), status="PASS", skip_reason=None, engine="explicit-test-contract",
        engine_version=None, model=None, language="es", stream_index=1, segments=(segment,),
        warnings=(), config=TranscriptConfig(), elapsed_seconds=0.0, audio_processing_seconds=0.0,
        inference_seconds=0.0, start_pts=int(origin*16000), time_base="1/16000",
        source_time_base="1/1000", source_start_pts=int(origin*8000), source_sample_rate=8000,
        timestamp_tolerance_samples=16, samples_analyzed=320000, threads=1, sample_rate=16000,
    )


def run_cli(root, source, *options, command="analyze", cwd=None, timeout=40):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "clip_factory", "--root", str(root), command, str(source), *options],
        cwd=cwd or root.parent, env=environment, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def assert_candidates(moments):
    assert len(moments) == 2
    # The falling-energy window [6, 6.5) is within one second of the cut.
    # Merging its evidence with the peak/rise extends the trailing context.
    for candidate, start, end in zip(moments, (3.0, 13.0), (9.5, 19.5)):
        assert candidate.start == pytest.approx(start)
        assert candidate.end == pytest.approx(end)
        assert candidate.duration == pytest.approx(end-start)
        assert {"scene_change", "audio_peak", "audio_energy_rise"} <= set(candidate.reasons)
        assert candidate.evidence
        assert not hasattr(candidate, "score")
    json.dumps([asdict(moment) for moment in moments], allow_nan=False)


@pytest.mark.parametrize("rate", [8000, 48000])
def test_real_visual_and_acoustic_onsets_generate_explainable_candidates(tmp_path, tools, rate):
    source = synthesize(tmp_path, tools, rate=rate)
    before = signature(source)
    scenes, audio = analyze_sources(source, tools)
    assert [change.timestamp_seconds for change in scenes.changes] == [5, 15]
    assert {event.kind for event in audio.events} >= {"peak", "energy_rise", "energy_drop"}
    result = combine(source, scenes, audio)
    assert_candidates(result.moments)
    assert result.elapsed_seconds >= 0 and signature(source) == before


@pytest.mark.parametrize("origin", [-1.25, 0, 2.5])
def test_original_positive_and_negative_offsets_preserve_video_relative_intervals(tmp_path, tools, origin):
    source = synthesize(tmp_path, tools, video_origin=origin)
    before = signature(source)
    scenes, audio = analyze_sources(source, tools)
    assert float(Fraction(scenes.start_pts) * Fraction(scenes.time_base)) == origin
    assert [change.source_timestamp_seconds for change in scenes.changes] == [origin+5, origin+15]
    result = combine(source, scenes, audio)
    assert_candidates(result.moments)
    for moment in result.moments:
        for evidence in moment.evidence:
            assert evidence.start_seconds == pytest.approx(evidence.source_start_seconds-origin)
            assert evidence.end_seconds == pytest.approx(evidence.source_end_seconds-origin)
            assert moment.start <= evidence.support_start_seconds <= evidence.support_end_seconds <= moment.end
            assert float(evidence.stream_start_pts * Fraction(evidence.time_base)) == pytest.approx(
                evidence.stream_origin_seconds)
    assert signature(source) == before


def test_independent_audio_origin_is_aligned_using_source_pts(tmp_path, tools):
    source = synthesize(tmp_path, tools, video_origin=2.5, audio_origin=3.5)
    before = signature(source)
    scenes, audio = analyze_sources(source, tools)
    rises = [event for event in audio.events if event.kind == "energy_rise"]
    assert [event.start_seconds for event in rises] == [4, 14]
    assert [event.source_start_seconds for event in rises] == [7.5, 17.5]
    result = combine(source, scenes, audio)
    assert_candidates(result.moments)
    for moment in result.moments:
        for evidence in moment.evidence:
            assert evidence.stream_origin_seconds == (3.5 if evidence.family == "audio" else 2.5)
            assert evidence.start_seconds == pytest.approx(evidence.source_start_seconds-2.5)
    assert signature(source) == before


def test_explicit_transcript_contract_adds_speech_evidence_without_claiming_asr(tmp_path, tools):
    source = synthesize(tmp_path, tools, video_origin=2.5)
    scenes, audio = analyze_sources(source, tools)
    transcript = transcript_contract(source, 2.5)
    result = combine(source, scenes, audio, transcript)
    first = result.moments[0]
    assert "speech_activity" in first.reasons and "scene_change" in first.reasons
    assert first.start <= 4 and first.end >= 7
    assert any("speech_activity" in moment.reasons for moment in result.moments)
    assert transcript.engine == "explicit-test-contract" and transcript.inference_seconds == 0


@pytest.mark.parametrize("available", ["visual", "acoustic"])
def test_qualified_speech_contract_combines_with_either_other_available_family(tmp_path, tools, available):
    source = synthesize(tmp_path, tools)
    scenes, audio = analyze_sources(source, tools)
    result = combine(source, scenes if available == "visual" else None,
                     audio if available == "acoustic" else None, transcript_contract(source))
    assert result.moments and all("speech_activity" in moment.reasons for moment in result.moments)
    for moment in result.moments:
        assert ("scene_change" in moment.reasons) == (available == "visual")


def test_one_isolated_word_contract_does_not_turn_a_visual_cut_into_interest(tmp_path, tools):
    source = synthesize(tmp_path, tools)
    scenes, _ = analyze_sources(source, tools)
    transcript = transcript_contract(source, start=4.5, end=7.0, text="Increíble")
    assert combine(source, scenes, None, transcript).moments == ()


@pytest.mark.parametrize("audio,cuts", [
    ("absent", True), ("silence", True), ("constant", True),
    ("coincident", False), ("separate", True), ("silence", False),
])
def test_missing_or_unrelated_or_unvarying_signals_are_not_narrative_evidence(tmp_path, tools, audio, cuts):
    source = synthesize(tmp_path, tools, audio=audio, cuts=cuts)
    before = signature(source)
    scenes, acoustic = analyze_sources(source, tools)
    result = combine(source, scenes, acoustic)
    assert result.moments == ()
    if audio == "absent":
        assert acoustic.status == "SKIPPED" and result.warnings
    assert signature(source) == before


def test_no_sources_and_one_family_of_events_are_successful_empty_results(tmp_path, tools):
    source = synthesize(tmp_path, tools)
    scenes, audio = analyze_sources(source, tools)
    for visual, acoustic, speech in ((None, None, None), (scenes, None, None),
                                     (None, audio, None), (None, None, transcript_contract(source))):
        result = combine(source, visual, acoustic, speech)
        assert result.moments == () and result.warnings


def test_overlapping_signals_are_merged_and_dense_events_are_bounded(tmp_path, tools):
    source = synthesize(tmp_path, tools, dense=True)
    scenes, audio = analyze_sources(source, tools)
    assert len(scenes.changes) >= 30 and len(audio.events) >= 30
    result = combine(source, scenes, audio)
    assert len(result.moments) <= 2  # 20 seconds / the default 10 second cooldown.
    for previous, current in zip(result.moments, result.moments[1:]):
        assert previous.end <= current.start
    assert all(0 <= moment.start < moment.end <= 20 for moment in result.moments)
    assert all(moment.duration <= 45 for moment in result.moments)


def test_invalid_source_and_inconsistent_pts_fail_without_mutating_original(tmp_path, tools):
    source = synthesize(tmp_path, tools)
    before = signature(source)
    scenes, audio = analyze_sources(source, tools)
    with pytest.raises(MomentDetectionError):
        combine(source, replace(scenes, source=str(tmp_path / "another.mkv")), audio)
    bad_change = replace(scenes.changes[0], source_timestamp_seconds=100.0)
    with pytest.raises(MomentDetectionError):
        combine(source, replace(scenes, changes=(bad_change,)), audio)
    assert signature(source) == before


@pytest.mark.parametrize("options,command", [
    (["--moments"], "analyze"),
    (["--scenes", "--audio", "--moments"], "analyze"),
    ([], "detect"),
])
def test_cli_reuses_analyzers_once_publishes_json_sqlite_logs_and_preserves_original(
        tmp_path, tools, options, command):
    root = tmp_path / "proyecto local con espacios"
    source = synthesize(root, tools)
    before = signature(source)
    completed = run_cli(root, source, *options, "--output", "data/working/moments.json", command=command)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((root / "data/working/moments.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert payload["analysis"] == {"scenes": "completed", "audio": "completed",
                                    "transcription": "not_run", "moments": "completed"}
    assert payload["moment_detection"]["engine"] and payload["moment_detection"]["config"]
    assert len(payload["moments"]) == 2 and payload["scene_detection"] and payload["audio_analysis"]
    assert "transcription" not in payload
    assert all("score" not in moment and "virality" not in moment for moment in payload["moments"])
    assert [(moment["start"], moment["end"]) for moment in payload["moments"]] == [(3, 9.5), (13, 19.5)]
    json.dumps(payload, allow_nan=False)
    with sqlite3.connect(root / "database/clip_factory.db") as connection:
        rows = connection.execute("SELECT source_path, metadata_json FROM probes").fetchall()
    assert len(rows) == 1 and rows[0][0] == str(source.resolve())
    assert json.loads(rows[0][1]) == payload
    entries = [json.loads(line) for line in (root / "logs/clip_factory.jsonl").read_text().splitlines()]
    for operation in ("scene_detection", "audio_analysis", "moment_detection"):
        matching = [entry for entry in entries if entry["operation"] == operation]
        assert len(matching) == 1 and matching[0]["duration_seconds"] >= 0
        assert matching[0]["file"] == str(source.resolve()) and matching[0]["error"] is None
    assert not any(entry["operation"] == "transcription" for entry in entries)
    assert signature(source) == before


def test_cli_stdout_preserves_negative_video_origin_and_interval_bounds(tmp_path, tools):
    root = tmp_path / "negative offset"
    source = synthesize(root, tools, video_origin=-1.25)
    before = signature(source)
    completed = run_cli(root, source, "--moments", "--output", "-")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert [(moment["start"], moment["end"]) for moment in payload["moments"]] == [(3, 9.5), (13, 19.5)]
    assert signature(source) == before


def test_cli_no_audio_is_empty_with_warning_and_scene_detection_still_runs(tmp_path, tools):
    root = tmp_path / "no audio"
    source = synthesize(root, tools, audio="absent")
    before = signature(source)
    completed = run_cli(root, source, "--moments", "--output", "-")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["moments"] == [] and payload["moment_detection"]["warnings"]
    assert payload["analysis"]["audio"] == "skipped_no_audio"
    assert payload["analysis"]["scenes"] == "completed" and payload["analysis"]["moments"] == "completed"
    assert len(payload["scene_detection"]["changes"]) == 2 and "Advertencia" in completed.stderr
    assert signature(source) == before


def test_cli_temporal_options_change_context_with_explicit_bounded_intervals(tmp_path, tools):
    root = tmp_path / "configured context"
    source = synthesize(root, tools)
    completed = run_cli(root, source, "--moments", "--moment-min-duration", "4",
                        "--moment-max-duration", "6", "--moment-context-before", "1",
                        "--moment-context-after", "2", "--output", "-")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    settings = payload["moment_detection"]["config"]
    assert (settings["min_duration"], settings["max_duration"], settings["context_before"],
            settings["context_after"]) == (4, 6, 1, 2)
    assert [(moment["start"], moment["end"]) for moment in payload["moments"]] == [(4, 8.5), (14, 18.5)]


def test_yaml_can_require_three_families_without_enabling_optional_asr(tmp_path, tools):
    root = tmp_path / "strict coincidence"
    source = synthesize(root, tools)
    with (root / "config/default.yaml").open("a", encoding="utf-8") as handle:
        handle.write("analysis:\n  moments:\n    min_sources: 3\n")
    completed = run_cli(root, source, "--moments", "--output", "-")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["moments"] == [] and payload["moment_detection"]["config"]["min_sources"] == 3
    assert payload["analysis"]["transcription"] == "not_run" and "transcription" not in payload
    assert payload["analysis"]["scenes"] == "completed" and payload["analysis"]["audio"] == "completed"


def test_optional_transcription_unavailable_does_not_block_moments(tmp_path, tools):
    root = tmp_path / "optional transcript"
    source = synthesize(root, tools)
    completed = run_cli(root, source, "--moments", "--transcribe", "--transcript-executable",
                        "clip-factory-intentionally-missing-whisper", "--output", "-")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 5 and len(payload["moments"]) == 2
    assert payload["analysis"]["transcription"] == "skipped_engine_unavailable"
    assert payload["transcription"]["status"] == "SKIPPED" and payload["transcription"]["warnings"]


@pytest.mark.parametrize("options,expected", [
    (["--audio-timeout", "0.000001"], "timeout"),
    (["--scene-timeout", "0.000001"], "timeout"),
    (["--moment-timeout", "0.000001"], "timeout"),
    (["--moment-max-events", "2"], "max_events"),
    (["--audio-stream", "0"], "pista"),
])
def test_upstream_errors_and_timeouts_do_not_publish_partial_moments(tmp_path, tools, options, expected):
    root = tmp_path / "failing moments"
    source = synthesize(root, tools)
    before = signature(source)
    destination = root / "data/working/result.json"
    completed = run_cli(root, source, "--moments", *options, "--output", str(destination))
    assert completed.returncode == 1 and expected in completed.stderr.casefold()
    assert completed.stdout == "" and not destination.exists()
    assert not (root / "database/clip_factory.db").exists()
    assert signature(source) == before


def test_discontinuous_audio_is_a_visible_error_not_empty_moments(tmp_path, tools):
    root = tmp_path / "discontinuous audio"
    source = synthesize(root, tools, discontinuity=True)
    before = signature(source)
    completed = run_cli(root, source, "--moments", "--output", "data/working/result.json")
    assert completed.returncode == 1 and "discontinu" in completed.stderr.casefold()
    assert completed.stdout == "" and not (root / "data/working/result.json").exists()
    assert not (root / "database/clip_factory.db").exists()
    assert signature(source) == before


def test_invalid_media_is_a_visible_error_not_a_successful_empty_run(tmp_path, tools):
    root = tmp_path / "invalid video"
    source = root / "data/input/invalid.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"deliberately invalid local fixture; not multimedia")
    before = signature(source)
    completed = run_cli(root, source, "--moments", "--output", "-")
    assert completed.returncode == 1 and completed.stderr and completed.stdout == ""
    assert not (root / "database/clip_factory.db").exists()
    assert signature(source) == before


def test_invalid_moment_configuration_fails_before_media_analysis(tmp_path, tools):
    root = tmp_path / "invalid config"
    source = synthesize(root, tools)
    before = signature(source)
    with (root / "config/default.yaml").open("a", encoding="utf-8") as handle:
        handle.write("analysis:\n  moments:\n    min_duration: 0\n")
    completed = run_cli(root, source, "--moments", "--output", "-")
    assert completed.returncode == 1 and "min_duration" in completed.stderr
    assert completed.stdout == "" and not (root / "database/clip_factory.db").exists()
    assert signature(source) == before


def test_recovery_publishes_saved_moments_without_reanalyzing(tmp_path, tools, monkeypatch, capsys):
    from clip_factory.cli import main as cli

    root = tmp_path / "recoverable moments"
    source = synthesize(root, tools)
    before = signature(source)
    calls = []
    for name in ("detect_scenes", "analyze_audio", "detect_moments"):
        original = getattr(cli, name)

        def counted(*args, _function=original, _name=name, **kwargs):
            calls.append(_name)
            return _function(*args, **kwargs)

        monkeypatch.setattr(cli, name, counted)
    write = cli.write_json_new

    def interrupted(destination, *args, **kwargs):
        if destination.name == "moments.json":
            raise SystemExit(73)
        return write(destination, *args, **kwargs)

    monkeypatch.setattr(cli, "write_json_new", interrupted)
    with pytest.raises(SystemExit):
        cli.main(["--root", str(root), "analyze", str(source), "--moments",
                  "--output", "data/working/moments.json"])
    monkeypatch.setattr(cli, "write_json_new", write)
    assert cli.main(["--root", str(root), "recover"]) == 0
    capsys.readouterr()
    payload = json.loads((root / "data/working/moments.json").read_text())
    assert payload["schema_version"] == 5 and len(payload["moments"]) == 2
    assert calls == ["detect_scenes", "analyze_audio", "detect_moments"]
    with sqlite3.connect(root / "database/clip_factory.db") as connection:
        row, = connection.execute("SELECT metadata_json FROM probes").fetchall()
    assert json.loads(row[0]) == payload and signature(source) == before


def _words(text):
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(r"[a-z]+", "".join(c for c in normalized if not unicodedata.combining(c)))


def test_real_optional_asr_remains_compatible_with_moments_and_existing_analyzers(
        tmp_path, tools, preserved_reference_fixture):
    manifest_value = os.environ.get("CLIP_FACTORY_VALIDATION_FIXTURES")
    model_value = os.environ.get("CLIP_FACTORY_WHISPER_MODEL")
    executable = shutil.which(os.environ.get("CLIP_FACTORY_WHISPER_BIN", "whisper-cli"))
    if not all((manifest_value, model_value, executable)):
        pytest.skip("ASR real con momentos PENDIENTE: configura manifest, modelo local y whisper-cli; sin descargas.")
    manifest_path, model = Path(manifest_value).expanduser(), Path(model_value).expanduser()
    if not manifest_path.is_file() or not model.is_file():
        pytest.skip("ASR real con momentos PENDIENTE: faltan manifest/modelo locales configurados.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1 and manifest["voices_downloaded"] is False
    item = manifest["fixtures"]["speech_es_two_scenes"]
    source = manifest_path.resolve().parent / item["path"]
    with preserved_reference_fixture(source, item):
        root = tmp_path / "real ASR and moments"
        folder = root / "config"
        folder.mkdir(parents=True)
        (folder / "default.yaml").write_text(
            f"media:\n  ffmpeg_bin: {json.dumps(tools[0])}\n  ffprobe_bin: {json.dumps(tools[1])}\n",
            encoding="utf-8",
        )
        completed = run_cli(root, source, "--moments", "--moment-min-sources", "3",
                            "--transcribe", "--transcript-executable", executable,
                            "--transcript-model", str(model.resolve()), "--transcript-timeout", "600",
                            "--output", "-", timeout=650)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["schema_version"] == 5
        assert payload["analysis"] == {"scenes": "completed", "audio": "completed",
                                        "transcription": "completed", "moments": "completed"}
        transcript = payload["transcription"]
        assert transcript["language"] == "es" and transcript["segments"] and transcript["inference_seconds"] > 0
        recognized = _words(" ".join(segment["text"] for segment in transcript["segments"]))
        expected = _words(item["reference"])
        # Require real reference content; a successful empty subprocess is insufficient.
        assert len(set(recognized) & set(expected)) / len(set(expected)) >= .7
        assert payload["scene_detection"]["changes"] and payload["audio_analysis"]["events"]
        assert payload["moments"]
        video_duration = payload["moment_detection"]["duration"]
        assert video_duration > 0
        for moment in payload["moments"]:
            assert 0 <= moment["start"] < moment["end"] <= video_duration
            assert moment["reasons"] and moment["evidence"]
            assert {evidence["family"] for evidence in moment["evidence"]} == {
                "scenes", "audio", "transcription"}
        json.dumps(payload, allow_nan=False)
