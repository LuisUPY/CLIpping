"""Real local FFmpeg audio metrics, sample timestamps and immutable sources."""

from fractions import Fraction
import hashlib
import json
import math
import os
import shutil
from time import monotonic

import pytest

from clip_factory.analysis.audio import AudioAnalysisError, analyze_audio
from clip_factory.analysis.scenes import SceneDetectionError, detect_scenes
from clip_factory.media.ffmpeg import MediaError, run_media_command
from clip_factory.media.probe import probe_media

pytestmark = pytest.mark.integration


@pytest.fixture
def tools():
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Audio Analysis omitido: requiere FFmpeg/FFprobe locales; no se instalan automáticamente.")
    return ffmpeg, ffprobe


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def synthesize_audio(path, tools, *, expression="0", duration="2", rate=8000, offset=0, filters=""):
    """Float PCM retains known amplitudes without lossy-codec priming/padding."""
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


def analyze(path, tools, **kwargs):
    return analyze_audio(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1], **kwargs)


def events_of(result, kind):
    return [event for event in result.events if event.kind == kind]


def test_digital_silence_groups_windows_and_preserves_original(tmp_path, tools):
    path = synthesize_audio(tmp_path / "input con espacios/silencio.mkv", tools, duration="1.2")
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25, min_silence_duration=.5)
    assert result.status == "PASS" and result.sample_rate == 8000 and result.channels == 1
    assert result.samples_analyzed == 9600 and result.window_samples == 2000
    assert [window.samples for window in result.windows] == [2000, 2000, 2000, 2000, 1600]
    assert all(window.rms == 0 and window.rms_dbfs is None and window.peak_dbfs is None for window in result.windows)
    assert result.rms == 0 and result.rms_dbfs is None and result.peak_dbfs is None
    assert len(result.events) == 1
    event = result.events[0]
    assert event.kind == "silence" and event.window_count == 5
    assert event.start_pts == 0 and event.end_pts == 9600
    assert event.start_seconds == 0 and event.end_seconds == 1.2
    assert signature(path) == before


@pytest.mark.parametrize("expression,channels", [
    ("0.5*sin(2*PI*1000*t)", 1),
    ("0.5*sin(2*PI*1000*t)|-0.5*sin(2*PI*1000*t)", 2),
])
def test_known_sine_rms_mono_and_antiphase_stereo_without_downmix(tmp_path, tools, expression, channels):
    path = synthesize_audio(tmp_path / "constant.mkv", tools, expression=expression, duration="1")
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    assert result.channels == channels and result.samples_analyzed == 8000
    assert len(result.windows) == 4 and result.events == ()
    assert result.rms == pytest.approx(.5 / math.sqrt(2), abs=1e-6)
    assert result.rms_dbfs == pytest.approx(20 * math.log10(.5 / math.sqrt(2)), abs=1e-4)
    assert result.peak_dbfs == pytest.approx(20 * math.log10(.5), abs=1e-4)
    for window in result.windows:
        assert window.rms == pytest.approx(.5 / math.sqrt(2), abs=1e-6)
        assert window.rms_dbfs == pytest.approx(-9.03089987, abs=1e-4)
        assert window.rms_change_db is None or abs(window.rms_change_db) < 1e-4
    assert signature(path) == before


def test_stereo_channels_use_combined_energy_and_largest_sample_peak(tmp_path, tools):
    path = synthesize_audio(tmp_path / "unequal-stereo.mkv", tools,
                            expression="0.8*sin(2*PI*1000*t)|0.2*sin(2*PI*1000*t)", duration="1")
    before = signature(path)
    result = analyze(path, tools)
    assert result.channels == 2 and result.rms == pytest.approx(math.sqrt(.17), abs=1e-6)
    assert result.rms_dbfs == pytest.approx(10 * math.log10(.17), abs=1e-4)
    assert result.peak_dbfs == pytest.approx(20 * math.log10(.8), abs=1e-4)
    peaks = events_of(result, "peak")
    assert len(peaks) == 1 and peaks[0].start_seconds == 0 and peaks[0].end_seconds == 1
    assert peaks[0].window_count == 2 and signature(path) == before


def test_known_peak_and_relative_changes_have_distinct_types_and_grouping(tmp_path, tools):
    path = synthesize_audio(tmp_path / "steps.mkv", tools,
                            expression="if(lt(t,1),0.1,if(lt(t,2),0.8,0.1))", duration="3")
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    peak, = events_of(result, "peak")
    rise, = events_of(result, "energy_rise")
    drop, = events_of(result, "energy_drop")
    assert (peak.start_seconds, peak.end_seconds, peak.window_count) == (1, 2, 4)
    assert (rise.start_seconds, rise.end_seconds, rise.window_count) == (1, 1.25, 1)
    assert (drop.start_seconds, drop.end_seconds, drop.window_count) == (2, 2.25, 1)
    assert abs(rise.change_db) == pytest.approx(20 * math.log10(8), abs=1e-4)
    assert abs(drop.change_db) == pytest.approx(20 * math.log10(8), abs=1e-4)
    assert peak.peak_dbfs == pytest.approx(20 * math.log10(.8), abs=1e-4)
    assert len(result.events) == 3 and signature(path) == before


def test_relative_energy_rise_is_detected_below_absolute_peak_threshold(tmp_path, tools):
    path = synthesize_audio(tmp_path / "quiet-rise.mkv", tools,
                            expression="if(lt(t,0.5),0.001,0.01)", duration="1")
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    assert events_of(result, "peak") == []
    rise, = events_of(result, "energy_rise")
    assert rise.start_seconds == .5 and abs(rise.change_db) == pytest.approx(20, abs=1e-4)
    assert signature(path) == before


def test_consecutive_relative_changes_are_grouped_without_merging_other_kinds(tmp_path, tools):
    path = synthesize_audio(tmp_path / "ramp.mkv", tools,
                            expression="if(lt(t,0.25),0.01,if(lt(t,0.5),0.02,if(lt(t,0.75),0.04,0.08)))",
                            duration="1.5")
    result = analyze(path, tools, window_seconds=.25, change_db=5)
    rise, = events_of(result, "energy_rise")
    assert (rise.start_seconds, rise.end_seconds, rise.window_count) == (.25, 1, 3)
    assert events_of(result, "peak") == [] and events_of(result, "silence") == []


@pytest.mark.parametrize("silence_end,minimum,expected", [(1, .5, [(0.25, 1)]), (.5, .5, []), (.5, .25, [(.25, .5)])])
def test_silence_duration_filters_short_intervals(tmp_path, tools, silence_end, minimum, expected):
    path = synthesize_audio(tmp_path / "interval.mkv", tools,
                            expression=f"if(between(t,0.25,{silence_end - 1 / 8000}),0,0.1)", duration="1.25")
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25, min_silence_duration=minimum)
    assert [(event.start_seconds, event.end_seconds) for event in events_of(result, "silence")] == expected
    assert signature(path) == before


def test_short_final_window_uses_real_sample_count_and_weighted_rms(tmp_path, tools):
    path = synthesize_audio(tmp_path / "partial.mkv", tools,
                            expression="if(lt(t,1),0.1,0.8)", duration="1.125")
    before = signature(path)
    result = analyze(path, tools)
    assert [window.samples for window in result.windows] == [4000, 4000, 1000]
    assert result.samples_analyzed == 9000 and result.windows[-1].end_pts == 9000
    assert result.windows[-1].end_seconds == 1.125
    assert result.rms == pytest.approx(math.sqrt((8000 * .1 ** 2 + 1000 * .8 ** 2) / 9000), abs=1e-6)
    peak, = events_of(result, "peak")
    assert peak.start_seconds == 1 and peak.end_seconds == 1.125
    assert signature(path) == before


@pytest.mark.parametrize("offset", [-.5, 0, 3.25])
def test_positive_and_negative_track_origins_retain_sample_pts(tmp_path, tools, offset):
    path = synthesize_audio(tmp_path / "offset.mkv", tools, expression="if(lt(t,0.5),0.1,0.8)",
                            duration="1", offset=offset)
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    assert result.time_base == "1/8000" and result.start_pts == offset * 8000
    assert [window.pts for window in result.windows] == [int(offset * 8000) + index * 2000 for index in range(4)]
    assert [window.start_seconds for window in result.windows] == [0, .25, .5, .75]
    for window in result.windows:
        assert window.source_start_seconds == float(Fraction(window.pts, 8000))
        assert window.source_end_seconds == float(Fraction(window.end_pts, 8000))
    peak, = events_of(result, "peak")
    assert peak.start_seconds == .5 and peak.source_start_seconds == offset + .5
    assert peak.end_seconds == 1 and peak.source_end_seconds == offset + 1
    assert signature(path) == before


def test_gap_inside_requested_window_is_rejected_instead_of_inventing_continuity(tmp_path, tools):
    path = synthesize_audio(tmp_path / "gap.mkv", tools, expression="0.1", duration="1",
                            filters="asetnsamples=n=1000:p=0,asetpts='PTS+if(gte(N,1000),2000,0)'")
    before = signature(path)
    # The .25 s gap occurs after .125 s, inside the first .5 s analysis window.
    with pytest.raises(AudioAnalysisError, match="PTS|timestamp|discontin|hueco"):
        analyze(path, tools, window_seconds=.5)
    assert signature(path) == before


def test_matroska_44100hz_timestamp_quantization_is_not_a_false_gap(tmp_path, tools):
    path = synthesize_audio(tmp_path / "quantized.mkv", tools, expression="0.2", duration="1.003", rate=44100)
    before = signature(path)
    result = analyze(path, tools, window_seconds=.1)
    assert result.status == "PASS" and result.time_base == "1/44100" and result.source_time_base == "1/1000"
    assert result.timestamp_tolerance_samples >= 45
    assert result.samples_analyzed == 44232 and sum(window.samples for window in result.windows) == 44232
    assert len(result.windows) == 11 and result.windows[-1].samples == 132
    assert result.windows[-1].end_seconds == float(Fraction(44232, 44100))
    assert result.events == () and signature(path) == before


def test_multiple_tracks_default_and_absolute_selection(tmp_path, tools):
    path = tmp_path / "tracks.mkv"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-copyts",
        "-f", "lavfi", "-i", "color=black:s=160x90:r=10:d=1",
        "-f", "lavfi", "-i", "aevalsrc=0.1:s=8000:d=1",
        "-f", "lavfi", "-i", "aevalsrc=0.8:s=8000:d=1,asetpts=PTS+3/TB",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "pcm_f32le",
        "-threads", "1", "-disposition:a:0", "0", "-disposition:a:1", "default", str(path),
    ])
    before = signature(path)
    default = analyze(path, tools)
    assert default.stream_index == 2 and default.start_pts == 24000
    assert default.rms == pytest.approx(.8, abs=1e-6)
    assert len(events_of(default, "peak")) == 1
    first = analyze(path, tools, audio_stream=1)
    assert first.stream_index == 1 and first.start_pts == 0
    assert first.rms == pytest.approx(.1, abs=1e-6) and first.events == ()
    for index in (0, 10):
        with pytest.raises(AudioAnalysisError, match="audio|pista"):
            analyze(path, tools, audio_stream=index)
    assert signature(path) == before


def test_no_audio_is_skipped_and_scene_detection_remains_independent(tmp_path, tools):
    path = tmp_path / "silent-video.mkv"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "color=black:s=160x90:r=10:d=2,drawbox=color=white:t=fill:enable='gte(t,1)'",
        "-c:v", "ffv1", "-threads", "1", str(path),
    ])
    before = signature(path)
    result = analyze(path, tools)
    assert result.status == "SKIPPED" and result.windows == () and result.events == ()
    assert result.samples_analyzed == 0 and result.warnings
    scenes = detect_scenes(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1])
    assert scenes.frames_analyzed == 20 and [cut.timestamp_seconds for cut in scenes.changes] == [1]
    with pytest.raises(AudioAnalysisError, match="audio|pista"):
        analyze(path, tools, audio_stream=0)
    assert signature(path) == before


def test_audio_only_can_succeed_independently_of_video_detector(tmp_path, tools):
    path = synthesize_audio(tmp_path / "audio-only.mkv", tools, expression="0.2", duration="0.5")
    before = signature(path)
    assert analyze(path, tools).status == "PASS"
    with pytest.raises(SceneDetectionError, match="video"):
        detect_scenes(path, ffmpeg_bin=tools[0], ffprobe_bin=tools[1])
    assert signature(path) == before


def test_corrupt_media_fails_visibly_without_modifying_it(tmp_path, tools):
    path = tmp_path / "corrupt.mkv"
    path.write_bytes(b"not a valid audio/video container")
    before = signature(path)
    with pytest.raises(MediaError):
        analyze(path, tools)
    assert signature(path) == before


def test_decoder_error_after_valid_probe_cannot_be_partial_success(tmp_path, tools):
    path = tmp_path / "damaged.flac"
    run_media_command([
        tools[0], "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "aevalsrc=0.5*sin(2*PI*440*t):s=8000:d=3", "-c:a", "flac", str(path),
    ])
    info = probe_media(path, ffprobe_bin=tools[1])
    packets = json.loads(run_media_command([
        tools[1], "-v", "error", "-show_packets", "-show_entries", "packet=pos,size", "-of", "json", str(path),
    ]).stdout)["packets"]
    packet = packets[-2]
    # Retain the container/metadata and early valid audio, then break a late frame.
    with path.open("r+b") as handle:
        handle.seek(int(packet["pos"]))
        handle.write(b"\0" * int(packet["size"]))
    before = signature(path)
    with pytest.raises(MediaError):
        analyze(path, tools, media_info=info)
    assert signature(path) == before


@pytest.mark.parametrize("settings", [{"window_seconds": 0}, {"timeout": 0}, {"change_db": 0}, {"peak_dbfs": 1}, {"silence_dbfs": -5}])
def test_invalid_settings_fail_and_preserve_original(tmp_path, tools, settings):
    path = synthesize_audio(tmp_path / "valid.mkv", tools, expression="0.1", duration="0.1")
    before = signature(path)
    with pytest.raises((ValueError, AudioAnalysisError)):
        analyze(path, tools, **settings)
    assert signature(path) == before


def test_real_ffmpeg_timeout_is_bounded_and_preserves_original(tmp_path, tools, monkeypatch):
    path = synthesize_audio(tmp_path / "timeout.mkv", tools, expression="0.2", duration="1")
    info = probe_media(path, ffprobe_bin=tools[1])
    before = signature(path)
    from clip_factory.analysis import audio
    original = audio.stream_media_command

    def realtime(command, **kwargs):
        command = list(command)
        command[command.index("-i"):command.index("-i")] = ["-readrate", "0.1"]
        return original(command, **kwargs)

    monkeypatch.setattr(audio, "stream_media_command", realtime)
    started = monotonic()
    with pytest.raises(MediaError, match="timeout"):
        analyze(path, tools, media_info=info, timeout=.4)
    assert monotonic() - started < 2 and signature(path) == before


def test_float_signal_above_full_scale_is_measured_and_warned(tmp_path, tools):
    path = synthesize_audio(tmp_path / 'overscale.mkv', tools, expression='2', duration='0.75')
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    assert result.rms == pytest.approx(2, abs=1e-6)
    assert result.peak_dbfs == pytest.approx(20 * math.log10(2), abs=1e-5)
    assert result.warnings and '0 dBFS' in result.warnings[0]
    assert len(events_of(result, 'peak')) == 1
    assert signature(path) == before


def test_aac_priming_and_offset_follow_decoded_pts_and_sample_counts(tmp_path, tools):
    path = tmp_path / 'aac-offset.mp4'
    run_media_command([
        tools[0], '-v', 'error', '-nostdin', '-n', '-copyts', '-f', 'lavfi', '-i',
        "aevalsrc='0.3*sin(2*PI*1000*t)':s=48000:d=1.25,asetpts=PTS+2/TB",
        '-c:a', 'aac', '-b:a', '128k', str(path),
    ])
    before = signature(path)
    result = analyze(path, tools, window_seconds=.25)
    frames = json.loads(run_media_command([
        tools[1], '-v', 'error', '-select_streams', 'a:0', '-show_frames',
        '-show_entries', 'frame=pts,nb_samples', '-of', 'json', str(path),
    ]).stdout)['frames']
    assert result.start_pts == frames[0]['pts']
    assert result.samples_analyzed == sum(frame['nb_samples'] for frame in frames)
    assert result.start_pts > result.sample_rate  # Actual delayed stream, including priming.
    assert result.windows[0].start_seconds == 0
    assert result.windows[0].source_start_seconds == float(Fraction(result.start_pts, result.sample_rate))
    assert signature(path) == before
