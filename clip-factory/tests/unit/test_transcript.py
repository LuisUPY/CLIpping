"""Transcription contracts, conservative text filtering and bounded processes.

Process outputs are deliberate fixtures, never evidence of speech recognition.
"""

from array import array
from dataclasses import FrozenInstanceError, replace
import json
import math
from pathlib import Path
import subprocess
import sys
from time import monotonic

import pytest

from clip_factory.analysis import transcript as t
from clip_factory.media.ffmpeg import DependencyError, MediaError
from clip_factory.media.probe import MediaInfo
from clip_factory.utils.files import FileSafetyError


@pytest.mark.parametrize("setting", [
    {"engine": "mlx-whisper"}, {"engine": "faster-whisper"}, {"executable": ""},
    {"executable": "\0"}, {"executable": None}, {"model": ""}, {"model": "http://model"},
    {"model": 1}, {"model": "a\0b"}, {"language": "fr"}, {"language": []},
    {"timeout": True}, {"timeout": 0}, {"timeout": -1}, {"timeout": math.inf},
    {"timeout": math.nan}, {"timeout": "30"}, {"chunk_seconds": .99},
    {"chunk_seconds": 31}, {"chunk_seconds": math.inf}, {"chunk_seconds": False},
    {"silence_dbfs": -121}, {"silence_dbfs": -19}, {"silence_dbfs": math.nan},
    {"audio_stream": True}, {"audio_stream": -1}, {"audio_stream": 1.0},
    {"max_segments": True}, {"max_segments": 0}, {"max_segments": 1_000_001},
])
def test_configuration_rejects_invalid_values(setting):
    with pytest.raises(ValueError):
        t.TranscriptConfig(**setting)


def test_configuration_and_segments_are_immutable_and_paths_serializable(tmp_path):
    config = t.TranscriptConfig(executable=tmp_path / "whisper-cli", model=tmp_path / "ggml-base.bin")
    assert isinstance(config.executable, str) and isinstance(config.model, str)
    with pytest.raises(FrozenInstanceError):
        config.language = "en"
    segment = t.TranscriptSegment("hola", 0, 1, -2, -1)
    with pytest.raises(FrozenInstanceError):
        segment.text = "changed"


def pcm(*levels):
    values = array("h")
    for level, samples in levels:
        values.extend([level] * samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def segment(text="Hola mundo", start=0, end=1000, tokens=None):
    return {"text": text, "offsets": {"from": start, "to": end},
            "tokens": [{"id": 100, "text": text, "p": .95}] if tokens is None else tokens}


def payload(*segments, language="es", multilingual=True):
    return {"model": {"multilingual": multilingual}, "result": {"language": language},
            "transcription": list(segments)}


def parse(data, *, audio=None, offset=0, origin=0, config=None):
    return t._parse_output(data, pcm=audio if audio is not None else pcm((1000, 16000)),
                           offset_samples=offset, origin=origin, config=config or t.TranscriptConfig())


@pytest.mark.parametrize("language,text", [("es", "Hola mundo"), ("en", "Hello world")])
def test_segment_language_text_and_offsets(language, text):
    parsed, detected, warnings = parse(payload(segment(text), language=language), offset=48000, origin=-16000)
    assert detected == language and warnings == []
    assert parsed == [t.TranscriptSegment(text, 3, 4, 2, 3)]


def test_empty_recognition_is_valid_without_invented_language_or_text():
    assert parse(payload()) == ([], None, [])


def test_segment_lying_inside_silent_part_of_spoken_chunk_is_removed():
    audio = pcm((1000, 8000), (0, 8000))
    parsed, language, warnings = parse(payload(segment("hola", 0, 500), segment("inventado", 500, 1000)), audio=audio)
    assert [item.text for item in parsed] == ["hola"]
    assert language == "es" and any("silencioso" in warning for warning in warnings)


def test_short_signal_is_not_diluted_by_overall_silent_chunk():
    values = t._pcm_values(pcm((0, 15999), (1000, 1)))
    assert not t._quiet(values, -50)
    assert t._quiet(t._pcm_values(pcm((0, 16000))), -50)
    assert t._quiet(t._pcm_values(pcm((10, 16000))), -50)


@pytest.mark.parametrize("text", ["[BLANK_AUDIO]", "(silence)", "[Music]", "[música]", "[risas]",
                                 "<|nospeech|>", "[_EOT_]", "[_TT_500_]", "  "])
def test_noise_annotations_and_special_tokens_are_not_transcriptions(text):
    assert parse(payload(segment(text)))[0] == []


@pytest.mark.parametrize("text", ["music", "silencio", "ruido", "música", "[Hola mundo]", "(español)"])
def test_legitimate_spoken_words_and_non_noise_wrappers_are_preserved(text):
    assert parse(payload(segment(text)))[0][0].text == text


def test_special_tokens_are_removed_without_losing_surrounding_text():
    assert parse(payload(segment("<|es|> Hola\n mundo [_EOT_]")))[0][0].text == "Hola mundo"


@pytest.mark.parametrize("data", [None, {}, [], {"transcription": {}}, {"transcription": [None]},
                                 payload({"text": None}), payload({"text": "hola"}),
                                 payload(segment(), language="Spanish"),
                                 payload(segment(), multilingual="true")])
def test_missing_or_malformed_json_fails_visibly(data):
    with pytest.raises(t.TranscriptError):
        parse(data)


@pytest.mark.parametrize("start,end", [(True, 1000), (0, True), (-1, 100), (1.0, 100),
                                       (500, 400), (1000, 1000), (1001, 1040), (0, 1051)])
def test_invalid_segment_timestamps_fail(start, end):
    with pytest.raises(t.TranscriptError, match="timestamps"):
        parse(payload(segment(start=start, end=end)))


def test_rounding_at_chunk_end_is_clamped_with_visible_warning():
    parsed, _, warnings = parse(payload(segment(end=1020)))
    assert parsed[0].end_seconds == 1
    assert any("ajustados" in warning for warning in warnings)


def test_large_overlap_fails_instead_of_fabricating_synchronization():
    with pytest.raises(t.TranscriptError, match="solapados"):
        parse(payload(segment(start=0, end=600), segment(start=500, end=1000)))


def test_small_timestamp_quantization_overlap_is_disclosed():
    parsed, _, warnings = parse(payload(segment(start=0, end=500), segment(start=490, end=1000)))
    assert parsed[1].start_seconds == .5 and warnings


@pytest.mark.parametrize("tokens", [[], [{"id": 100, "text": "hola"}],
                                    [{"id": 50257, "text": "[_EOT_]", "p": 1}]])
def test_text_without_usable_lexical_confidence_is_discarded_with_warning(tokens):
    parsed, language, warnings = parse(payload(segment(tokens=tokens)))
    assert parsed == [] and language is None
    assert any("probabilidades" in warning for warning in warnings)


def test_special_token_probabilities_do_not_rescue_low_lexical_confidence():
    tokens = [{"id": 100, "text": "hola", "p": .01}]
    tokens.extend({"id": 50257 + i, "text": f"[_TT_{i}_]", "p": 1} for i in range(100))
    parsed, _, warnings = parse(payload(segment(tokens=tokens)))
    assert parsed == [] and any("baja confianza" in warning for warning in warnings)


def test_partial_lexical_confidence_does_not_bypass_conservative_filter():
    tokens = [{"id": 100, "p": .95}, {"id": 101, "text": "sin probabilidad"}]
    parsed, language, warnings = parse(payload(segment(tokens=tokens)))
    assert parsed == [] and language is None and any("probabilidades" in note for note in warnings)


@pytest.mark.parametrize("requested,reported", [("es", "en"), ("en", "es")])
def test_engine_cannot_silently_ignore_requested_language(requested, reported):
    with pytest.raises(t.TranscriptError, match="idioma distinto"):
        parse(payload(segment(), language=reported), config=t.TranscriptConfig(language=requested))


def test_confidence_uses_mean_log_probability_not_arithmetic_mean():
    tokens = [{"id": 100, "p": .01}, {"id": 101, "p": .99}]
    assert parse(payload(segment(tokens=tokens)))[0] == []


@pytest.mark.parametrize("probability", [math.nan, math.inf, -1, 1.1, True, "0.9", None])
def test_invalid_token_probabilities_are_engine_errors(probability):
    with pytest.raises(t.TranscriptError, match="probabilidad"):
        parse(payload(segment(tokens=[{"id": 100, "p": probability}])))


@pytest.mark.parametrize("tokens", [[None], [{"id": -1, "p": .9}], [{"id": True, "p": .9}],
                                    [{"id": "100", "p": .9}], [{"p": .9}], {}])
def test_invalid_token_contract_is_not_silently_accepted(tokens):
    with pytest.raises(t.TranscriptError, match="token"):
        parse(payload(segment(tokens=tokens)))


def test_en_model_metadata_cannot_be_used_as_spanish_or_auto():
    with pytest.raises(t.TranscriptError, match="inglés"):
        parse(payload(segment(), multilingual=False))
    config = t.TranscriptConfig(language="en")
    parsed, language, _ = parse(payload(segment("Hello"), language="en", multilingual=False), config=config)
    assert language == "en" and parsed[0].text == "Hello"
    eot_only = segment(tokens=[{"id": 50256, "p": 1}])
    assert parse(payload(eot_only, language="en", multilingual=False), config=config)[0] == []


def test_missing_detected_language_errors_in_auto_and_explicit_language_is_known():
    data = payload(segment())
    data["result"] = {}
    with pytest.raises(t.TranscriptError, match="idioma detectado"):
        parse(data)
    assert parse(data, config=t.TranscriptConfig(language="es"))[1] == "es"


def test_segment_retention_limit_does_not_return_truncated_success():
    with pytest.raises(t.TranscriptError, match="max_segments"):
        parse(payload(segment(end=500), segment(start=500)), config=t.TranscriptConfig(max_segments=1))


def frame(timeline, tag, pts, samples, *, rate=16000, channels=1):
    timeline.feed(f"[ashowinfo@{tag} @ address] [info] n:0 pts:{pts} pts_time:0 fmt:s16 channels:{channels} rate:{rate} nb_samples:{samples}")


def test_timeline_preserves_negative_origin_and_audits_sample_totals():
    timeline = t._Timeline(48000, 1, 1)
    frame(timeline, "source", -24000, 48000, rate=48000)
    frame(timeline, "pcm", -8000, 16000)
    timeline.finish(32000)
    assert timeline.source_start == -24000 and timeline.start == -8000
    assert timeline.source_samples == 48000 and timeline.samples == 16000


@pytest.mark.parametrize("tag,shift", [("source", -10), ("source", 10), ("pcm", -10), ("pcm", 10)])
def test_timeline_rejects_gaps_and_overlaps_before_chunking(tag, shift):
    timeline = t._Timeline(16000, 1, 1)
    frame(timeline, "source", 0, 8000)
    frame(timeline, "pcm", 0, 8000)
    with pytest.raises(t.TranscriptError, match="Discontinuidad"):
        frame(timeline, tag, 8000 + shift, 8000)


def test_timeline_tolerance_is_anchored_not_accumulated():
    timeline = t._Timeline(16000, 1, 1)
    frame(timeline, "source", 0, 1000)
    frame(timeline, "source", 1001, 1000)
    with pytest.raises(t.TranscriptError, match="Discontinuidad"):
        frame(timeline, "source", 2002, 1000)


@pytest.mark.parametrize("operation", ["no_frames", "pcm_first", "missing_bytes", "bad_resample_total", "origin_changed", "rate_changed"])
def test_incomplete_or_inconsistent_timeline_is_never_silence(operation):
    timeline = t._Timeline(16000, 1, 1)
    with pytest.raises(t.TranscriptError):
        if operation == "pcm_first":
            frame(timeline, "pcm", 0, 1000)
        elif operation == "rate_changed":
            frame(timeline, "source", 0, 1000, rate=48000)
        elif operation != "no_frames":
            frame(timeline, "source", 0, 16000)
            frame(timeline, "pcm", 100 if operation == "origin_changed" else 0,
                  15000 if operation == "bad_resample_total" else 16000)
        timeline.finish(31000 if operation == "missing_bytes" else timeline.samples * 2)


def test_ffmpeg_error_exit_zero_is_not_valid_silence_and_diagnostics_are_bounded():
    timeline = t._Timeline(16000, 1, 1)
    for _ in range(20):
        timeline.feed("[decoder] [warning] issue")
        timeline.feed("[decoder] [error] corrupt")
    assert len(timeline.errors) == len(timeline.warnings) == 8
    with pytest.raises(t.TranscriptError, match="informó errores"):
        timeline.finish(0)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    input_dir = root / "data/input"
    working = root / "data/working"
    input_dir.mkdir(parents=True)
    working.mkdir()
    source = input_dir / "local.mkv"
    source.write_bytes(b"media fixture, not real encoded audio")
    info = MediaInfo(path=str(source), duration=1, width=None, height=None, fps=None,
                     fps_fraction=None, codec=None, has_audio=True,
                     streams=({"index": 0, "codec_type": "audio", "sample_rate": "16000",
                               "channels": 1, "time_base": "1/16000"},))
    return dict(root=root, input_dir=input_dir, working_dir=working), source, info


def test_no_audio_is_skipped_before_executable_lookup(project, monkeypatch):
    paths, source, info = project
    monkeypatch.setattr(t.shutil, "which", lambda _: pytest.fail("missing audio must not inspect engine"))
    result = t.transcribe_media(source, media_info=replace(info, streams=(), has_audio=False), **paths)
    assert result.status == "SKIPPED" and result.skip_reason == "no_audio"
    assert result.language is None and result.segments == () and result.samples_analyzed == 0


def test_engine_absent_does_not_touch_original_or_create_temporaries(project, monkeypatch):
    paths, source, info = project
    original, mtime = source.read_bytes(), source.stat().st_mtime_ns
    monkeypatch.setattr(t.shutil, "which", lambda _: None)
    result = t.transcribe_media(source, media_info=info, **paths)
    assert result.status == "SKIPPED" and result.skip_reason == "engine_unavailable"
    assert result.warnings and source.read_bytes() == original and source.stat().st_mtime_ns == mtime
    assert list(paths["working_dir"].iterdir()) == []


@pytest.mark.parametrize("model_state", ["unset", "missing", "directory", "empty"])
def test_model_unavailable_is_explicit_and_no_engine_is_started(project, monkeypatch, model_state):
    paths, source, info = project
    model = paths["root"] / "model.bin"
    if model_state == "directory":
        model.mkdir()
    elif model_state == "empty":
        model.touch()
    config = t.TranscriptConfig(model=None if model_state == "unset" else model)
    monkeypatch.setattr(t.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(t, "_run_bounded", lambda *args: pytest.fail("missing model must not start engine"))
    result = t.transcribe_media(source, config=config, media_info=info, **paths)
    assert result.skip_reason == "model_unavailable" and result.status == "SKIPPED"
    assert list(paths["working_dir"].iterdir()) == []


@pytest.mark.parametrize("name", ["ggml-base.en.bin", "ggml-tiny.en-q5_1.bin"])
def test_english_only_model_filename_rejects_spanish_auto_before_execution(project, monkeypatch, name):
    paths, source, info = project
    model = paths["root"] / name
    model.write_bytes(b"model fixture")
    monkeypatch.setattr(t.shutil, "which", lambda _: sys.executable)
    with pytest.raises(t.TranscriptError, match=".en"):
        t.transcribe_media(source, config=t.TranscriptConfig(model=model), media_info=info, **paths)


def test_explicit_bad_track_does_not_turn_into_missing_engine_skip(project):
    paths, source, info = project
    with pytest.raises(t.TranscriptError, match="pista 2"):
        t.transcribe_media(source, config=t.TranscriptConfig(audio_stream=2), media_info=info, **paths)


@pytest.mark.parametrize("threads", [False, 0, -1, 1.0, 65])
def test_invalid_threads_are_rejected_before_media_processing(project, threads):
    paths, source, info = project
    with pytest.raises(ValueError, match="threads"):
        t.transcribe_media(source, media_info=info, threads=threads, **paths)


def test_metadata_from_another_file_and_probe_errors_fail(project):
    paths, source, info = project
    with pytest.raises(t.TranscriptError, match="metadata"):
        t.transcribe_media(source, media_info=replace(info, path=str(paths["root"] / "other.mkv")), **paths)
    with pytest.raises(t.TranscriptError, match="FFprobe"):
        t.transcribe_media(source, media_info=replace(info, diagnostics=({"severity": "ERROR", "message": "bad"},)), **paths)


@pytest.mark.parametrize("path", ["https://example.org/media.mp4", "pipe:0", "-"])
def test_remote_inputs_are_rejected_without_subprocess(project, path):
    paths, _, _ = project
    with pytest.raises(MediaError):
        t.transcribe_media(path, **paths)


def test_bounded_engine_output_drains_both_pipes_without_deadlock():
    command = [sys.executable, "-c", "import os; os.write(1,b'a'*300000); os.write(2,b'b'*300000)"]
    result = t._run_bounded(command, monotonic() + 5)
    assert result.stdout == "a" * 65536 and result.stderr == "b" * 65536


def test_engine_timeout_is_visible_and_reaps_child():
    command = [sys.executable, "-c", "import time; time.sleep(20)"]
    start = monotonic()
    with pytest.raises(t.TranscriptError, match="timeout"):
        t._run_bounded(command, start + .1)
    assert monotonic() - start < 2


def test_engine_exit_code_retains_bounded_stderr():
    command = [sys.executable, "-c", "import sys; print('engine broken',file=sys.stderr); sys.exit(7)"]
    with pytest.raises(t.TranscriptError) as failure:
        t._run_bounded(command, monotonic() + 5)
    assert failure.value.returncode == 7 and "engine broken" in failure.value.stderr
    assert failure.value.command == tuple(command)


@pytest.mark.parametrize("diagnostic", ["error: failed despite zero", "fatal: broken", "[ERROR] missing model",
                                         "unknown argument: --fake", "failed to open local model",
                                         "whisper_init_from_file_with_params_no_state: failed to open model"])
def test_engine_error_prefixes_are_recognized_even_after_exit_zero(diagnostic):
    assert t._engine_reported_error(diagnostic)


@pytest.mark.parametrize("diagnostic", ["output_json: saving output to '/tmp/error/result.json'",
                                         "whisper_init_from_file_with_params_no_state: loading model from '/tmp/failed/model.bin'",
                                         "whisper_model_load: model path = '/tmp/error/failed.bin'",
                                         "whisper_full: processing 16000 samples, 1 threads"])
def test_normal_engine_logs_with_error_words_in_paths_remain_valid(diagnostic):
    assert not t._engine_reported_error(diagnostic)


def test_engine_guard_failure_is_not_swallowed_and_reaps_child():
    command = [sys.executable, "-c", "import time; time.sleep(20)"]
    def fail():
        raise t.TranscriptError("output size limit")
    start = monotonic()
    with pytest.raises(t.TranscriptError, match="size limit"):
        t._run_bounded(command, start + 5, fail)
    assert monotonic() - start < 2


def test_missing_executable_is_dependency_error(tmp_path):
    with pytest.raises(DependencyError):
        t._run_bounded([str(tmp_path / "absent")], monotonic() + 1)


def test_private_chunk_directory_cleans_only_owned_files(project):
    paths, source, _ = project
    before = source.read_bytes()
    with t._chunk_workspace(**paths) as working:
        assert working.parent == paths["working_dir"] and working.stat().st_mode & 0o777 == 0o700
        (working / "chunk.wav").write_bytes(b"temporary samples")
        (working / "result.json").write_text("{}")
    assert not working.exists() and source.read_bytes() == before


def test_temporaries_cannot_be_created_inside_input(project):
    paths, _, _ = project
    with pytest.raises(FileSafetyError, match="inmutable"):
        with t._chunk_workspace(paths["root"], paths["input_dir"], paths["input_dir"]):
            pytest.fail("unsafe directory must not be entered")


def test_symlink_replacement_during_cleanup_does_not_hide_original_engine_error(project, capsys):
    paths, source, _ = project
    before = source.read_bytes()
    with pytest.raises(t.TranscriptError, match="primary failure"):
        with t._chunk_workspace(**paths) as working:
            (working / "result.json").symlink_to(source)
            raise t.TranscriptError("primary failure")
    assert "Error adicional" in capsys.readouterr().err
    assert source.read_bytes() == before


def stub_full_contract(project, monkeypatch, output_json, *, chunks=1):
    """Exercise transcribe orchestration without claiming decoding or ASR."""
    paths, source, info = project
    model = paths["root"] / "ggml-test.bin"
    model.write_bytes(b"test-double-model")
    monkeypatch.setattr(t.shutil, "which", lambda _: sys.executable)

    def run(command, deadline, guard=None):
        if "--help" in command:
            help_text = "--model --output-json-full --language --no-gpu --no-fallback --suppress-nst"
            return subprocess.CompletedProcess(command, 0, help_text, "")
        destination = Path(command[command.index("-of") + 1] + ".json")
        destination.write_text(output_json, encoding="utf-8")
        if guard:
            guard()
        return subprocess.CompletedProcess(command, 0, "", "")

    def decode(command, timeline, chunk_samples, deadline, consume):
        frame(timeline, "source", 0, chunks * 16000)
        frame(timeline, "pcm", 0, chunks * 16000)
        for index in range(chunks):
            consume(pcm((1000, 16000)), index * 16000)
        timeline.finish(chunks * 32000)

    monkeypatch.setattr(t, "_run_bounded", run)
    monkeypatch.setattr(t, "_decode_chunks", decode)
    return source, info, t.TranscriptConfig(model=model, chunk_seconds=1), paths


def test_accumulated_utf8_text_limit_fails_before_retaining_oversized_transcript(project, monkeypatch):
    # Four accented characters occupy eight UTF-8 bytes. One chunk fits;
    # adding the second crosses 12 bytes although max_segments still permits it.
    source, info, config, paths = stub_full_contract(
        project, monkeypatch, json.dumps(payload(segment("áéíó"))), chunks=2,
    )
    monkeypatch.setattr(t, "_TEXT_LIMIT", 12)
    before = source.read_bytes(), source.stat().st_mtime_ns
    with pytest.raises(t.TranscriptError, match="texto transcrito acumulado"):
        t.transcribe_media(source, config=config, media_info=info, **paths)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert not list(paths["working_dir"].iterdir())


def test_deeply_nested_engine_json_becomes_visible_media_error_and_cleans_temporaries(project, monkeypatch):
    output = '{"transcription": [], "nested":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    source, info, config, paths = stub_full_contract(project, monkeypatch, output)
    before = source.read_bytes(), source.stat().st_mtime_ns
    with pytest.raises(t.TranscriptError, match="JSON válido") as failure:
        t.transcribe_media(source, config=config, media_info=info, **paths)
    assert isinstance(failure.value.__cause__, RecursionError)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert not list(paths["working_dir"].iterdir())
