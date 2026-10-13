"""Metadata parsing does not need external media tools."""

import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from clip_factory.media import probe
from clip_factory.media.ffmpeg import MediaError
from clip_factory.media.probe import probe_media
from clip_factory.media.validation import validate_metadata


@pytest.fixture
def source(tmp_path):
    media = tmp_path / "original con espacios; $(literal).mp4"
    media.write_bytes(b"source contents must remain unchanged")
    return media


@pytest.fixture
def metadata():
    return {
        "format": {"duration": "12.345", "start_time": "0.0", "format_name": "mov,mp4"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "avg_frame_rate": "30000/1001", "r_frame_rate": "30/1",
             "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
    }


def _mock_probe(monkeypatch, metadata):
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata), ""))
    monkeypatch.setattr(probe, "run_media_command", execute)
    return execute


def test_probe_metadata_and_preserves_input(source, metadata, monkeypatch):
    execute = _mock_probe(monkeypatch, metadata)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_mtime = source.stat().st_mtime_ns
    info = probe_media(source, ffprobe_bin="/my tools/ffprobe", timeout=13)
    assert info.duration == 12.345
    assert info.resolution == (1920, 1080)
    assert info.fps == pytest.approx(30000 / 1001)
    assert info.fps_fraction == "30000/1001"
    assert info.has_audio is True
    assert info.codec == "h264"
    assert info.aspect_ratio == "16:9"
    assert info.to_dict()["resolution"] == "1920x1080"
    assert json.loads(json.dumps(info.to_dict()))["streams"][1]["codec_name"] == "aac"
    arguments = execute.call_args.args[0]
    assert arguments[0] == "/my tools/ffprobe"
    assert arguments[-1] == str(source.resolve())
    assert arguments[arguments.index("-protocol_whitelist") + 1] == "file,pipe"
    assert execute.call_args.kwargs["timeout"] == 13
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert source.stat().st_mtime_ns == original_mtime


@pytest.mark.parametrize("url", ["https://example.com/video", "file:///tmp/video", "pipe:0", "-"])
def test_rejects_urls_and_protocol_inputs_before_execution(monkeypatch, url):
    execute = _mock_probe(monkeypatch, {})
    with pytest.raises(MediaError, match="locales"):
        probe_media(url)
    execute.assert_not_called()


def test_rejects_missing_and_directory_sources(tmp_path, monkeypatch):
    execute = _mock_probe(monkeypatch, {})
    for path in (tmp_path / "missing.mp4", tmp_path):
        with pytest.raises(MediaError):
            probe_media(path)
    execute.assert_not_called()


@pytest.mark.parametrize("payload", [[], {}, {"streams": []}, {"streams": [None]}, {"streams": [{}], "format": []}])
def test_rejects_invalid_metadata(source, monkeypatch, payload):
    _mock_probe(monkeypatch, payload)
    with pytest.raises(MediaError, match="metadata|streams"):
        probe_media(source)


def test_rejects_invalid_json(source, monkeypatch):
    monkeypatch.setattr(
        probe, "run_media_command", Mock(return_value=subprocess.CompletedProcess([], 0, "not JSON", ""))
    )
    with pytest.raises(MediaError, match="JSON inválido"):
        probe_media(source)


def test_rotation_affects_display_aspect_not_encoded_dimensions(source, metadata, monkeypatch):
    metadata["streams"][0]["side_data_list"] = [{"rotation": -90}]
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert info.rotation == 270
    assert info.resolution == (1920, 1080)
    assert (info.display_width, info.display_height) == (1080, 1920)
    assert info.display_aspect_ratio == "9/16"
    assert info.aspect_ratio == "9:16"


def test_anamorphic_pixels_affect_display_ratio(source, metadata, monkeypatch):
    metadata["streams"][0].update(width=1440, height=1080, sample_aspect_ratio="4:3")
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert info.resolution == (1440, 1080)
    assert info.aspect_ratio == "16:9"
    assert info.sample_aspect_ratio == "4/3"


@pytest.mark.parametrize("dimensions, expected", [((640, 480), "4:3"), ((2560, 1080), "21:9"), ((480, 480), "1:1"), ((320, 500), "custom")])
def test_classifies_common_and_custom_aspects(source, metadata, monkeypatch, dimensions, expected):
    metadata["streams"][0].update(width=dimensions[0], height=dimensions[1])
    _mock_probe(monkeypatch, metadata)
    assert probe_media(source).aspect_ratio == expected


def test_falls_back_to_stream_timestamps_and_rate(source, metadata, monkeypatch):
    metadata["format"].update(duration="N/A", start_time="N/A")
    metadata["streams"][0].update(
        avg_frame_rate="0/0", duration_ts="90000", time_base="1/9000", start_time="-0.05"
    )
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert info.duration == 10.0
    assert info.fps_fraction == "30/1"
    assert info.start_time == -0.05


def test_missing_measurements_stay_unknown_and_fail_basic_validation(source, metadata, monkeypatch):
    metadata["format"]["duration"] = "NaN"
    metadata["streams"][0].update(avg_frame_rate="0/0", r_frame_rate="N/A")
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert info.duration is None
    assert info.fps is None
    assert info.fps_fraction is None
    report = validate_metadata(info)
    assert report["status"] == "FAIL"
    assert report["scope"] == "metadata_only"
    assert report["checks"]["duration_valid"] is False
    assert report["checks"]["fps_valid"] is False


def test_cover_art_is_not_selected_as_video(source, metadata, monkeypatch):
    metadata["streams"].insert(0, {
        "codec_type": "video", "codec_name": "mjpeg", "width": 300, "height": 300,
        "disposition": {"attached_pic": 1},
    })
    _mock_probe(monkeypatch, metadata)
    assert probe_media(source).codec == "h264"


def test_audio_only_has_no_fabricated_video_metadata(source, metadata, monkeypatch):
    metadata["streams"] = [metadata["streams"][1]]
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert info.has_audio is True
    assert info.has_video is False
    assert info.resolution == (None, None)
    assert info.to_dict()["resolution"] is None
    assert validate_metadata(info)["status"] == "FAIL"


def test_silent_video_is_allowed_unless_audio_required(source, metadata, monkeypatch):
    metadata["streams"] = [metadata["streams"][0]]
    _mock_probe(monkeypatch, metadata)
    info = probe_media(source)
    assert validate_metadata(info)["status"] == "PASS"
    report = validate_metadata(info, require_audio=True)
    assert report["status"] == "FAIL"
    assert report["checks"]["audio_present"] is False
    assert report["warnings"]
