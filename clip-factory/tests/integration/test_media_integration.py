"""Real local ffprobe integration; no external videos or network are needed."""

import hashlib
import os
import shutil

import pytest

from clip_factory.media.ffmpeg import MediaError, run_media_command
from clip_factory.media.probe import probe_media
from clip_factory.media.validation import validate_metadata


@pytest.fixture
def media_tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Integración omitida: requiere ffmpeg y ffprobe locales; no se instalaron automáticamente.")
    return ffmpeg, ffprobe


@pytest.mark.integration
def test_real_probe_of_synthetic_video_preserves_original(tmp_path, media_tools):
    ffmpeg, ffprobe = media_tools
    source = tmp_path / "input con espacios" / "original de prueba.mp4"
    source.parent.mkdir()
    run_media_command([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30000/1001",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
        "-t", "1.2", "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-threads", "1", "-shortest", str(source),
    ], timeout=30)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_mtime = source.stat().st_mtime_ns
    info = probe_media(source, ffprobe_bin=ffprobe)
    assert 1.1 <= info.duration <= 1.4
    assert info.resolution == (320, 180)
    assert info.fps == pytest.approx(30000 / 1001)
    assert info.has_audio is True
    assert info.codec == "mpeg4"
    assert info.aspect_ratio == "16:9"
    assert validate_metadata(info, require_audio=True)["status"] == "PASS"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert source.stat().st_mtime_ns == original_mtime


@pytest.mark.integration
def test_real_probe_of_corrupt_media_exposes_ffprobe_error(tmp_path, media_tools):
    _, ffprobe = media_tools
    source = tmp_path / "corrupt.mp4"
    source.write_bytes(b"not a video")
    with pytest.raises(MediaError) as captured:
        probe_media(source, ffprobe_bin=ffprobe)
    assert captured.value.returncode is not None
    assert captured.value.stderr
    assert source.read_bytes() == b"not a video"
