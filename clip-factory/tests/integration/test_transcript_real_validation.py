"""Opt-in real whisper.cpp checks using controlled local TTS reference fixtures.

No speech is generated, engine mocked, voice/model downloaded, or original
modified by these tests. Passing is evidence of real ASR on controlled synthetic
speech, not an estimate of accuracy on natural recordings.
"""

from fractions import Fraction
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

from clip_factory.analysis.scenes import detect_scenes
from clip_factory.analysis.transcript import TranscriptConfig, transcribe_media

pytestmark = pytest.mark.integration


def words(text):
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(r"[a-z]+", "".join(character for character in decomposed
                                       if not unicodedata.combining(character)))


def word_error_rate(reference, recognized):
    expected, actual = words(reference), words(recognized)
    assert len(expected) >= 20 and len(set(expected)) >= 20
    previous = list(range(len(actual) + 1))
    for index, token in enumerate(expected, 1):
        current = [index]
        for other_index, other in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[other_index] + 1,
                               previous[other_index - 1] + (token != other)))
        previous = current
    return previous[-1] / len(expected)


@pytest.fixture
def real_context(tmp_path):
    manifest_value = os.environ.get("CLIP_FACTORY_VALIDATION_FIXTURES")
    model_value = os.environ.get("CLIP_FACTORY_WHISPER_MODEL")
    executable = shutil.which(os.environ.get("CLIP_FACTORY_WHISPER_BIN", "whisper-cli"))
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not all((manifest_value, model_value, executable, ffmpeg, ffprobe)):
        pytest.skip("Validación ASR real controlada PENDIENTE: configura modelo local, motor y "
                    "CLIP_FACTORY_VALIDATION_FIXTURES (manifest.json). No se descarga ni simula ASR.")
    manifest_path, model = Path(manifest_value).expanduser(), Path(model_value).expanduser()
    if not manifest_path.is_file() or not model.is_file():
        pytest.skip("Validación ASR real PENDIENTE: falta el manifest o modelo local configurado.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1 and manifest["voices_downloaded"] is False
    root = tmp_path / "independent real ASR project"
    input_dir, working_dir = root / "data/input", root / "data/working"
    input_dir.mkdir(parents=True)
    working_dir.mkdir(parents=True)
    return {
        "manifest": manifest, "fixture_dir": manifest_path.resolve().parent,
        "root": root, "input_dir": input_dir, "working_dir": working_dir,
        "executable": executable, "model": str(model.resolve()), "ffmpeg": ffmpeg, "ffprobe": ffprobe,
    }


def fixture(context, name):
    item = context["manifest"]["fixtures"][name]
    path = context["fixture_dir"] / item["path"]
    assert path.is_file() and path.resolve().parent == context["fixture_dir"]
    return path, item


def transcribe(context, path, *, language="auto", chunk_seconds=30):
    return transcribe_media(
        path, config=TranscriptConfig(executable=context["executable"], model=context["model"],
                                      language=language, timeout=600, chunk_seconds=chunk_seconds),
        root=context["root"], input_dir=context["input_dir"], working_dir=context["working_dir"],
        ffmpeg_bin=context["ffmpeg"], ffprobe_bin=context["ffprobe"], threads=4,
    )


def assert_speech_result(result, item):
    assert result.status == "PASS" and result.language == item["language"] and result.segments
    recognized = " ".join(segment.text for segment in result.segments)
    assert word_error_rate(item["reference"], recognized) <= 0.35, recognized
    assert result.inference_seconds > 0 and result.audio_processing_seconds > 0
    assert result.samples_analyzed / 16000 == pytest.approx(item["signal_duration_seconds"], abs=0.001)
    assert result.sample_rate == 16000 and result.time_base == "1/16000"
    origin = float(Fraction(result.start_pts, 16000))
    assert origin == pytest.approx(item["origin_seconds"], abs=0.001)
    last_end = 0
    for segment in result.segments:
        assert last_end <= segment.start_seconds < segment.end_seconds
        assert segment.end_seconds <= result.samples_analyzed / 16000 + 0.01
        assert segment.source_start_seconds == pytest.approx(origin + segment.start_seconds)
        assert segment.source_end_seconds == pytest.approx(origin + segment.end_seconds)
        last_end = segment.end_seconds


@pytest.mark.parametrize("language", ["es", "en"])
def test_real_whisper_recognizes_explicit_language_controlled_speech(
        real_context, language, preserved_reference_fixture):
    path, item = fixture(real_context, f"speech_{language}")
    with preserved_reference_fixture(path, item):
        result = transcribe(real_context, path, language=language)
        assert_speech_result(result, item)


@pytest.mark.parametrize("language", ["es", "en"])
def test_real_whisper_auto_language_preserves_positive_and_negative_offsets(
        real_context, language, preserved_reference_fixture):
    path, item = fixture(real_context, f"speech_{language}_offset")
    with preserved_reference_fixture(path, item):
        result = transcribe(real_context, path)
        assert_speech_result(result, item)


@pytest.mark.parametrize("name", ["silence", "tone", "no_audio"])
def test_real_whisper_generates_no_words_for_controlled_absence_of_speech(
        real_context, name, preserved_reference_fixture):
    path, item = fixture(real_context, name)
    with preserved_reference_fixture(path, item):
        result = transcribe(real_context, path)
        assert result.segments == () and result.language is None
        if name == "no_audio":
            assert result.status == "SKIPPED" and result.skip_reason == "no_audio"
            with preserved_reference_fixture(path, item):
                scenes = detect_scenes(path, ffmpeg_bin=real_context["ffmpeg"],
                                       ffprobe_bin=real_context["ffprobe"])
            assert scenes.frames_analyzed > 0 and scenes.changes == ()
        else:
            assert result.status == "PASS" and result.skip_reason is None
            assert result.samples_analyzed == 160000 and result.warnings
            if name == "silence":
                assert result.inference_seconds == 0
            else:
                assert result.inference_seconds > 0


def test_real_whisper_consecutive_chunks_preserve_full_speech_timeline(
        real_context, preserved_reference_fixture):
    path, item = fixture(real_context, "speech_en_consecutive")
    with preserved_reference_fixture(path, item):
        result = transcribe(real_context, path)
        assert_speech_result(result, item)
        assert any(segment.start_seconds >= 30 for segment in result.segments)
        assert any("chunk" in warning for warning in result.warnings)
        assert not [path for path in real_context["working_dir"].rglob("*")
                    if path.is_file() and path.name != ".clip_factory.lock"]


def test_real_whisper_cli_combines_asr_scenes_audio_json_and_persistence(
        real_context, tmp_path, preserved_reference_fixture):
    path, item = fixture(real_context, "speech_es_two_scenes")
    with preserved_reference_fixture(path, item):
        config_dir = real_context["root"] / "config"
        config_dir.mkdir()
        (config_dir / "default.yaml").write_text(
            f"media:\n  ffmpeg_bin: {json.dumps(real_context['ffmpeg'])}\n"
            f"  ffprobe_bin: {json.dumps(real_context['ffprobe'])}\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run([
            sys.executable, "-m", "clip_factory", "--root", str(real_context["root"]), "analyze", str(path),
            "--scenes", "--audio", "--transcribe", "--transcript-executable", real_context["executable"],
            "--transcript-model", real_context["model"], "--transcript-language", "auto",
            "--transcript-timeout", "600", "--output", "-",
        ], cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, check=False, timeout=650)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        json.dumps(payload, allow_nan=False)
        assert payload["schema_version"] == 4 and payload["validation"]["status"] == "PASS"
        assert payload["analysis"] == {"scenes": "completed", "audio": "completed",
                                       "transcription": "completed", "moments": "not_run"}
        assert [change["timestamp_seconds"] for change in payload["scene_detection"]["changes"]] == [item["scene_cut_seconds"]]
        assert payload["audio_analysis"]["status"] == "PASS" and payload["audio_analysis"]["windows"]
        transcript = payload["transcription"]
        assert transcript["status"] == "PASS" and transcript["language"] == "es"
        assert transcript["model"] == real_context["model"] and transcript["segments"]
        assert word_error_rate(item["reference"], " ".join(segment["text"] for segment in transcript["segments"])) <= 0.35
        with sqlite3.connect(real_context["root"] / "database/clip_factory.db") as connection:
            rows = connection.execute("SELECT source_path, metadata_json FROM probes").fetchall()
        assert len(rows) == 1 and rows[0][0] == str(path.resolve())
        assert json.loads(rows[0][1]) == payload
