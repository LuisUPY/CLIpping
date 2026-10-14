"""Real FFmpeg integration and explicit whisper.cpp process test doubles.

The generated executable verifies our local process/JSON/timeline contract; it
does NOT recognize speech and is never evidence of real ASR validation. The
last two opt-in tests use only an installed engine/model and supplied recordings
with reference text. No test downloads a model or synthesizes pretend speech.
"""

from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import textwrap
from time import monotonic
import unicodedata

import pytest

from clip_factory.analysis.audio import analyze_audio
from clip_factory.analysis.scenes import detect_scenes
from clip_factory.analysis.transcript import TranscriptConfig, TranscriptError, transcribe_media
from clip_factory.media.ffmpeg import MediaError, run_media_command
from clip_factory.media.probe import probe_media

pytestmark = pytest.mark.integration


@pytest.fixture
def tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Transcription omitida: requiere FFmpeg/FFprobe locales; no se instalan automáticamente.")
    return ffmpeg, ffprobe


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    input_dir = root / "data/input"
    working_dir = root / "data/working"
    input_dir.mkdir(parents=True)
    working_dir.mkdir(parents=True)
    return dict(root=root, input_dir=input_dir, working_dir=working_dir)


def signature(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest(), path.stat().st_mtime_ns


def synthesize_audio(path, tools, *, expression="0.3*sin(2*PI*440*t)",
                     duration="1.25", rate=16000, offset=0, filters=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    source = f"aevalsrc=exprs='{expression}':s={rate}:d={duration}"
    if filters:
        source += "," + filters
    if offset:
        source += f",asetpts=PTS+({offset})/TB"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-copyts", "-f", "lavfi", "-i", source,
        "-c:a", "pcm_f32le", "-threads", "1", "-avoid_negative_ts", "disabled", str(path),
    ])
    return path


@pytest.fixture
def process_test_double(tmp_path):
    """An intentionally fake CLI executable; validates plumbing, never ASR."""
    executable = tmp_path / "whisper process test double"
    model = tmp_path / "ggml-test-double.bin"
    model.write_bytes(b"ggml\0process-contract-test-only")
    calls = tmp_path / "test-double-calls.jsonl"
    mode = tmp_path / "test-double-mode.txt"
    mode.write_text("valid", encoding="utf-8")
    script = "#!" + sys.executable + "\n" + textwrap.dedent(
        """
        import json
        from pathlib import Path
        import sys
        import time
        import wave

        CALLS = Path(__CALLS__)
        MODE = Path(__MODE__)
        if '--version' in sys.argv or '--help' in sys.argv or '-h' in sys.argv:
            print('whisper.cpp process test double; --model --output-json-full '
                  '--no-gpu --no-fallback --suppress-nst --language')
            raise SystemExit(0)

        def argument(flag):
            return sys.argv[sys.argv.index(flag) + 1]

        with wave.open(argument('-f'), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
            record = {
                'arguments': sys.argv[1:], 'sample_rate': audio.getframerate(),
                'channels': audio.getnchannels(), 'sample_width': audio.getsampwidth(),
                'samples': audio.getnframes(), 'duration': duration,
            }
        with CALLS.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record) + '\\n')
        mode = MODE.read_text(encoding='utf-8')
        if mode == 'timeout':
            time.sleep(20)
        if mode == 'failure':
            print('deliberate process test double failure', file=sys.stderr)
            raise SystemExit(7)
        if mode == 'error_stderr':
            print('error: deliberate failure despite exit zero', file=sys.stderr)
        output = Path(argument('-of') + '.json')
        if mode == 'invalid_json':
            output.write_text('{invalid', encoding='utf-8')
            raise SystemExit(0)
        if mode == 'missing_output':
            raise SystemExit(0)
        language = argument('-l')
        if language == 'auto':
            language = 'es'
        text = 'Hola mundo' if language == 'es' else 'Hello world'
        end = max(1, min(1000, int(duration * 1000)))
        segment = {
            'offsets': {'from': 0, 'to': end}, 'text': text,
            'tokens': [{'id': 100, 'text': text, 'p': 0.95}],
        }
        if mode == 'invalid_timestamps':
            segment['offsets'] = {'from': 800, 'to': 200}
        output.write_text(json.dumps({
            'model': {'multilingual': True}, 'params': {'model': argument('-m')},
            'result': {'language': language}, 'transcription': [] if mode == 'empty' else [segment],
        }), encoding='utf-8')
        """
    ).replace("__CALLS__", repr(str(calls))).replace("__MODE__", repr(str(mode)))
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return dict(executable=executable, model=model, calls=calls, mode=mode)


def settings(process_test_double, **changes):
    return replace(TranscriptConfig(
        executable=str(process_test_double["executable"]), model=str(process_test_double["model"]),
    ), **changes)


def transcribe(path, tools, project, config):
    return transcribe_media(path, config=config, ffmpeg_bin=tools[0], ffprobe_bin=tools[1], **project)


def recorded_calls(process_test_double):
    path = process_test_double["calls"]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def assert_no_working_files(project):
    # The established workspace lock may persist; decoded media/JSON must not.
    assert not [path for path in project["working_dir"].rglob("*")
                if path.is_file() and path.name != ".clip_factory.lock"]


@pytest.mark.parametrize("language,expected", [("es", "Hola mundo"), ("en", "Hello world"), ("auto", "Hola mundo")])
def test_process_test_double_language_contract_with_real_ffmpeg(
        tools, project, process_test_double, language, expected):
    path = synthesize_audio(project["input_dir"] / "señal con espacios.mkv", tools, rate=48000)
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double, language=language))
    assert result.status == "PASS" and result.language == ("es" if language == "auto" else language)
    assert result.engine == "whisper.cpp" and result.model == str(process_test_double["model"])
    assert [segment.text for segment in result.segments] == [expected]
    segment, = result.segments
    assert segment.start_seconds == 0 and segment.end_seconds == 1
    call, = recorded_calls(process_test_double)
    assert (call["sample_rate"], call["channels"], call["sample_width"]) == (16000, 1, 2)
    assert call["samples"] == 20000
    assert call["arguments"][call["arguments"].index("-l") + 1] == language
    assert signature(path) == before
    assert_no_working_files(project)


def test_digital_silence_never_invokes_test_double_or_invents_text(tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "silence.mkv", tools, expression="0", duration="1.25")
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.status == "PASS" and result.segments == ()
    assert result.language is None and result.inference_seconds == 0
    assert recorded_calls(process_test_double) == []
    assert result.samples_analyzed == 20000 and signature(path) == before
    assert_no_working_files(project)


def test_empty_test_double_recognition_result_does_not_invent_segments(tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "unrecognized-signal.mkv", tools)
    before = signature(path)
    process_test_double["mode"].write_text("empty", encoding="utf-8")
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.status == "PASS" and result.segments == ()
    assert len(recorded_calls(process_test_double)) == 1 and signature(path) == before
    assert_no_working_files(project)


def test_no_audio_is_explicitly_skipped_and_scene_detection_still_works(tools, project, process_test_double):
    path = project["input_dir"] / "video-without-audio.mkv"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "color=black:s=160x90:r=10:d=2,drawbox=color=white:t=fill:enable='gte(t,1)'",
        "-c:v", "ffv1", "-threads", "1", str(path),
    ])
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.status == "SKIPPED" and result.skip_reason == "no_audio"
    assert result.segments == () and result.warnings and result.language is None
    assert recorded_calls(process_test_double) == []
    scenes = detect_scenes(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1])
    assert [change.timestamp_seconds for change in scenes.changes] == [1]
    assert signature(path) == before
    assert_no_working_files(project)


@pytest.mark.parametrize("offset", [-0.5, 0, 3.25])
def test_process_test_double_timestamps_retain_actual_track_offset(
        tools, project, process_test_double, offset):
    path = synthesize_audio(project["input_dir"] / "offset.mkv", tools, offset=offset)
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.time_base == "1/16000" and result.start_pts == int(offset * 16000)
    segment, = result.segments
    assert (segment.start_seconds, segment.end_seconds) == (0, 1)
    assert segment.source_start_seconds == offset
    assert segment.source_end_seconds == offset + 1
    assert signature(path) == before
    assert_no_working_files(project)


def test_track_default_and_absolute_selection_use_requested_audio(tools, project, process_test_double):
    path = project["input_dir"] / "multiple-tracks.mkv"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-copyts",
        "-f", "lavfi", "-i", "color=black:s=160x90:r=10:d=1.25",
        "-f", "lavfi", "-i", "aevalsrc=0:s=16000:d=1.25",
        "-f", "lavfi", "-i", "aevalsrc=0.2*sin(2*PI*440*t):s=16000:d=1.25,asetpts=PTS+3/TB",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "pcm_f32le",
        "-threads", "1", "-disposition:a:0", "0", "-disposition:a:1", "default", str(path),
    ])
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.stream_index == 2 and result.start_pts == 48000
    assert result.segments[0].source_start_seconds == 3
    silent = transcribe(path, tools, project, settings(process_test_double, audio_stream=1))
    assert silent.stream_index == 1 and silent.segments == ()
    assert len(recorded_calls(process_test_double)) == 1
    for index in (0, 10):
        with pytest.raises((TranscriptError, ValueError), match="audio|pista"):
            transcribe(path, tools, project, settings(process_test_double, audio_stream=index))
    assert signature(path) == before
    assert_no_working_files(project)


def test_bounded_chunks_rebase_test_double_segments_and_keep_partial_final_chunk(
        tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "chunks.mkv", tools, duration="2.3", offset=1.5)
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double, chunk_seconds=1))
    calls = recorded_calls(process_test_double)
    assert len(calls) == 3 and [call["samples"] for call in calls] == [16000, 16000, 4800]
    assert [(segment.start_seconds, segment.end_seconds) for segment in result.segments] == [
        (0, 1), (1, 2), (2, 2.3),
    ]
    assert [(segment.source_start_seconds, segment.source_end_seconds) for segment in result.segments] == [
        (1.5, 2.5), (2.5, 3.5), (3.5, 3.8),
    ]
    assert result.samples_analyzed == 36800 and result.audio_processing_seconds > 0
    assert result.inference_seconds > 0 and signature(path) == before
    assert_no_working_files(project)


def test_44100hz_resampling_with_container_quantization_is_not_a_false_timestamp_gap(
        tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "44100hz.mkv", tools, rate=44100, duration="1.003")
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.status == "PASS" and result.source_time_base == "1/1000"
    assert result.samples_analyzed == 16048 and result.time_base == "1/16000"
    assert len(result.segments) == 1 and result.segments[0].source_start_seconds == 0
    assert signature(path) == before
    assert_no_working_files(project)


def test_aac_priming_and_track_origin_follow_decoded_frames_in_source_timeline(
        tools, project, process_test_double):
    path = project["input_dir"] / "aac-offset.mp4"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-copyts", "-f", "lavfi", "-i",
        "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=1.25,asetpts=PTS+2/TB",
        "-c:a", "aac", "-b:a", "128k", str(path),
    ])
    frames = json.loads(run_media_command([
        tools[1], "-v", "error", "-select_streams", "a:0", "-show_frames",
        "-show_entries", "frame=pts,nb_samples", "-of", "json", str(path),
    ]).stdout)["frames"]
    before = signature(path)
    result = transcribe(path, tools, project, settings(process_test_double))
    assert result.status == "PASS" and result.start_pts > 16000
    source_start = float(Fraction(frames[0]["pts"], 48000))
    assert result.segments[0].start_seconds == 0
    assert result.segments[0].source_start_seconds == pytest.approx(source_start, abs=1 / 16000)
    assert result.segments[0].source_end_seconds == pytest.approx(source_start + 1, abs=1 / 16000)
    assert signature(path) == before
    assert_no_working_files(project)


@pytest.mark.parametrize("missing", ["engine", "model"])
def test_missing_optional_dependencies_skip_without_breaking_acoustic_analysis(
        tools, project, process_test_double, missing):
    path = synthesize_audio(project["input_dir"] / "optional.mkv", tools)
    before = signature(path)
    change = {"executable": str(project["root"] / "absent-whisper-cli")} if missing == "engine" else {"model": None}
    result = transcribe(path, tools, project, settings(process_test_double, **change))
    assert result.status == "SKIPPED" and result.skip_reason == f"{missing}_unavailable"
    assert result.segments == () and result.warnings
    assert analyze_audio(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1]).status == "PASS"
    assert recorded_calls(process_test_double) == [] and signature(path) == before
    assert_no_working_files(project)


@pytest.mark.parametrize("mode", ["failure", "error_stderr", "invalid_json", "missing_output", "invalid_timestamps"])
def test_process_test_double_failures_are_visible_and_cleanup_temporaries(
        tools, project, process_test_double, mode):
    path = synthesize_audio(project["input_dir"] / "failure.mkv", tools)
    before = signature(path)
    process_test_double["mode"].write_text(mode, encoding="utf-8")
    with pytest.raises(MediaError):
        transcribe(path, tools, project, settings(process_test_double))
    assert len(recorded_calls(process_test_double)) == 1 and signature(path) == before
    assert_no_working_files(project)


def test_real_subprocess_timeout_of_test_double_is_bounded_and_cleans_temporaries(
        tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "timeout.mkv", tools)
    before = signature(path)
    process_test_double["mode"].write_text("timeout", encoding="utf-8")
    started = monotonic()
    with pytest.raises(MediaError, match="timeout"):
        transcribe(path, tools, project, settings(process_test_double, timeout=0.7))
    assert monotonic() - started < 3 and signature(path) == before
    assert len(recorded_calls(process_test_double)) == 1
    assert_no_working_files(project)


def test_real_ffmpeg_audio_processing_timeout_is_independent_of_inference(
        tools, project, process_test_double, monkeypatch):
    from clip_factory.analysis import transcript

    path = synthesize_audio(project["input_dir"] / "decoder-timeout.mkv", tools)
    info = probe_media(path, ffprobe_bin=tools[1])
    before = signature(path)
    original_spawn = transcript._spawn

    def realtime_decode(command):
        command = list(command)
        if command[0] == tools[0] and "-i" in command:
            index = command.index("-i")
            command[index:index] = ["-readrate", "0.1"]
        return original_spawn(command)

    monkeypatch.setattr(transcript, "_spawn", realtime_decode)
    started = monotonic()
    with pytest.raises(MediaError, match="timeout"):
        transcribe_media(path, config=settings(process_test_double, timeout=0.7), media_info=info,
                         ffmpeg_bin=tools[0], ffprobe_bin=tools[1], **project)
    assert monotonic() - started < 3 and signature(path) == before
    assert recorded_calls(process_test_double) == []
    assert_no_working_files(project)


def test_corrupt_original_is_rejected_without_attempting_inference(tools, project, process_test_double):
    path = project["input_dir"] / "corrupt.mkv"
    path.write_bytes(b"not a valid local media file")
    before = signature(path)
    with pytest.raises(MediaError):
        transcribe(path, tools, project, settings(process_test_double))
    assert recorded_calls(process_test_double) == [] and signature(path) == before
    assert_no_working_files(project)


def test_corrupt_frame_after_valid_probe_cannot_publish_partial_transcription(
        tools, project, process_test_double):
    path = project["input_dir"] / "damaged.flac"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "aevalsrc=0.3*sin(2*PI*440*t):s=16000:d=3", "-c:a", "flac", str(path),
    ])
    info = probe_media(path, ffprobe_bin=tools[1])
    packets = json.loads(run_media_command([
        tools[1], "-v", "error", "-show_packets", "-show_entries", "packet=pos,size", "-of", "json", str(path),
    ]).stdout)["packets"]
    packet = packets[-2]
    # This synthetic fixture is deliberately damaged before its immutable check.
    with path.open("r+b") as handle:
        handle.seek(int(packet["pos"]))
        handle.write(b"\0" * int(packet["size"]))
    before = signature(path)
    with pytest.raises(MediaError):
        transcribe_media(path, config=settings(process_test_double), media_info=info,
                         ffmpeg_bin=tools[0], ffprobe_bin=tools[1], **project)
    assert signature(path) == before
    assert_no_working_files(project)


def test_timestamp_gap_is_rejected_without_inventing_continuity(tools, project, process_test_double):
    path = synthesize_audio(project["input_dir"] / "gap.mkv", tools, duration="1",
                            filters="asetnsamples=n=2000:p=0,asetpts='PTS+if(gte(N,2000),4000,0)'")
    before = signature(path)
    with pytest.raises(MediaError, match="PTS|timestamp|discontin|hueco"):
        transcribe(path, tools, project, settings(process_test_double))
    assert signature(path) == before
    assert_no_working_files(project)


def _words(text):
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(r"[a-z]+", "".join(char for char in normalized if not unicodedata.combining(char)))


@pytest.mark.parametrize("language", ["es", "en"])
def test_real_whisper_asr_opt_in_local_recording_with_reference_text(tools, project, language):
    """Actual ASR, explicitly pending when engine/model/recording are absent."""
    suffix = language.upper()
    model_value = os.environ.get("CLIP_FACTORY_WHISPER_MODEL")
    recording_value = os.environ.get(f"CLIP_FACTORY_SPEECH_{suffix}")
    reference = os.environ.get(f"CLIP_FACTORY_SPEECH_{suffix}_TEXT")
    executable = shutil.which(os.environ.get("CLIP_FACTORY_WHISPER_BIN", "whisper-cli"))
    if not executable or not model_value or not recording_value or not reference:
        pytest.skip(
            f"ASR real {language} PENDIENTE: requiere whisper-cli local, CLIP_FACTORY_WHISPER_MODEL, "
            f"CLIP_FACTORY_SPEECH_{suffix} y CLIP_FACTORY_SPEECH_{suffix}_TEXT; sin descargas ni test doubles."
        )
    model, path = Path(model_value).expanduser(), Path(recording_value).expanduser()
    if not model.is_file() or not path.is_file():
        pytest.skip(f"ASR real {language} PENDIENTE: falta el modelo o la grabación local configurados.")
    expected = set(_words(reference))
    assert len(expected) >= 3, "Aporta una referencia de al menos tres palabras diferentes para validar ASR."
    before = signature(path)
    result = transcribe(path, tools, project, TranscriptConfig(
        executable=executable, model=str(model), language="auto", timeout=600,
    ))
    assert result.status == "PASS" and result.language == language and result.segments
    recognized = set(_words(" ".join(segment.text for segment in result.segments)))
    assert len(expected & recognized) / len(expected) >= 0.5
    previous_end = 0.0
    origin = float(Fraction(result.start_pts, 16000))
    for segment in result.segments:
        assert 0 <= segment.start_seconds < segment.end_seconds <= result.samples_analyzed / 16000 + 0.1
        assert segment.start_seconds >= previous_end
        assert segment.source_start_seconds == pytest.approx(origin + segment.start_seconds)
        assert segment.source_end_seconds == pytest.approx(origin + segment.end_seconds)
        previous_end = segment.end_seconds
    assert signature(path) == before
    assert_no_working_files(project)
