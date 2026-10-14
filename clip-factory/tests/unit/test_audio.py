"""Scalar streaming parser, temporal audit and standalone audio engine failures."""

from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
import subprocess

import pytest

from clip_factory.analysis import audio
from clip_factory.media.ffmpeg import DependencyError, MediaError
from clip_factory.media.probe import MediaInfo


def record(parser, pts, samples, rms=None, peak=None):
    parser.feed(f"frame:0 pts:{pts} pts_time:999.999")
    parser.feed(f"lavfi.astats.Overall.Number_of_samples={samples}")
    if rms is not None:
        parser.feed(f"lavfi.astats.Overall.RMS_level={rms}")
    if peak is not None:
        parser.feed(f"lavfi.astats.Overall.Peak_level={peak}")


def parser(**settings):
    return audio._AudioParser(1000, 1000, 1, audio.AudioAnalysisConfig(window_seconds=1, **settings))


def test_parser_uses_integer_pts_sample_counts_and_weighted_energy():
    p = parser()
    record(p, -1000, 2500)
    record(p, -1000, 1000, "-20", "-20")
    record(p, 0, 1000, "0", "0")
    record(p, 1000, 500, "-inf", "-inf")
    p.finish()
    assert p.start_pts == -1000 and p.samples == p.decoded_samples == 2500
    assert [w.start_seconds for w in p.windows] == [0, 1, 2]
    assert [w.source_start_seconds for w in p.windows] == [-1, 0, 1]
    assert p.windows[-1].samples == 500 and p.windows[-1].end_seconds == 2.5
    assert p.windows[-1].rms == 0 and p.windows[-1].rms_dbfs is None
    assert p.sum_squares / p.samples == pytest.approx((.01 * 1000 + 1 * 1000) / 2500)
    assert p.windows[1].rms_change_db == 20 and p.windows[2].rms_change_db == -120
    with pytest.raises(FrozenInstanceError):
        p.windows[0].pts = 0


def test_consecutive_peaks_and_silence_are_grouped_independently_of_changes():
    p = parser(min_silence_duration=2)
    record(p, 0, 5000)
    for pts, rms in ((0, "-20"), (1000, "-1"), (2000, "-1"), (3000, "-inf"), (4000, "-inf")):
        record(p, pts, 1000, rms, rms)
    p.finish()
    assert [(e.kind, e.start_seconds, e.end_seconds, e.window_count) for e in p.events] == [
        ("energy_rise", 1, 2, 1), ("peak", 1, 3, 2),
        ("energy_drop", 3, 4, 1), ("silence", 3, 5, 2),
    ]


def test_consecutive_energy_changes_merge_but_opposite_directions_do_not():
    p = parser(change_db=5)
    record(p, 0, 5000)
    for i, rms in enumerate(("-30", "-24", "-12", "-25", "-32")):
        record(p, i * 1000, 1000, rms, rms)
    p.finish()
    assert [(e.kind, e.window_count, e.change_db) for e in p.events] == [
        ("energy_rise", 2, 12), ("energy_drop", 2, -13),
    ]


def test_rms_changes_use_floor_but_report_real_dbfs():
    p = parser(energy_floor_dbfs=-150)
    record(p, 0, 3000)
    for i, level in enumerate(("-inf", "-140", "-130")):
        record(p, i * 1000, 1000, level, level)
    p.finish()
    assert p.windows[0].rms_dbfs is None and p.windows[0].rms == 0
    assert p.windows[1].rms_dbfs == -140 and p.windows[1].rms_change_db == 10


def test_minimum_silence_uses_sample_duration_despite_pts_quantization():
    p = audio._AudioParser(1000, 1000, 1, audio.AudioAnalysisConfig(window_seconds=1, min_silence_duration=2))
    record(p, 0, 2000)
    record(p, 0, 1000, "-inf", "-inf")
    record(p, 999, 1000, "-inf", "-inf")
    p.finish()
    assert len(p.events) == 1 and p.events[0].window_count == 2


@pytest.mark.parametrize("offset", [-10, 10])
def test_decoded_pts_gaps_or_overlaps_fail_even_inside_window(offset):
    p = parser()
    record(p, 0, 400)
    record(p, 400 + offset, 600)
    with pytest.raises(audio.AudioAnalysisError, match="Discontinuidad"):
        p.finish()


def test_quantization_tolerance_is_anchored_not_accumulated_per_packet():
    p = parser()
    record(p, 0, 1000)
    record(p, 1001, 1000)
    record(p, 2002, 1000)
    with pytest.raises(audio.AudioAnalysisError, match="Discontinuidad"):
        p.finish()


def test_window_pts_cannot_fabricate_an_offset():
    p = parser()
    record(p, 0, 1000)
    record(p, 10, 1000, "-20", "-10")
    with pytest.raises(audio.AudioAnalysisError, match="PTS de ventana"):
        p.finish()


def test_partial_window_is_allowed_only_at_eof():
    p = parser()
    record(p, 0, 1000)
    record(p, 0, 500, "-20", "-10")
    record(p, 500, 500, "-20", "-10")
    with pytest.raises(audio.AudioAnalysisError, match="parcial intermedia"):
        p.finish()


@pytest.mark.parametrize("rms,peak", [("nan", "0"), ("inf", "0"), ("x", "0"), ("-inf", "0"), ("0", "-inf"), ("-10", "-20")])
def test_bad_measurements_are_not_silence(rms, peak):
    p = parser()
    record(p, 0, 1000)
    record(p, 0, 1000, rms, peak)
    with pytest.raises(audio.AudioAnalysisError):
        p.finish()


@pytest.mark.parametrize("samples", ["nan", "inf", "1.5", "0", "-2", "invalid"])
def test_invalid_sample_counts_fail(samples):
    p = parser()
    record(p, 0, samples)
    with pytest.raises(audio.AudioAnalysisError):
        p.finish()


@pytest.mark.parametrize("lines", [
    [], ["frame:0 pts:NOPTS pts_time:NOPTS"], ["lavfi.astats.Overall.RMS_level=-20"],
    ["frame:0 pts:0 pts_time:0", "lavfi.astats.Overall.RMS_level=-20"],
    ["frame:0 pts:0 pts_time:0", "lavfi.astats.Overall.Number_of_samples=1000"],
])
def test_empty_or_incomplete_output_never_means_successful_silence(lines):
    p = parser()
    with pytest.raises(audio.AudioAnalysisError):
        for line in lines:
            p.feed(line)
        p.finish()


def test_duplicate_metrics_fail():
    p = parser()
    record(p, 0, 1000)
    with pytest.raises(audio.AudioAnalysisError, match="duplicada"):
        p.feed("lavfi.astats.Overall.Number_of_samples=1000")


def test_missing_final_window_fails_total_sample_audit():
    p = parser()
    record(p, 0, 2000)
    record(p, 0, 1000, "-20", "-10")
    with pytest.raises(audio.AudioAnalysisError, match="todas las ventanas"):
        p.finish()


@pytest.mark.parametrize("setting", [{"max_windows": 1}, {"max_events": 1}])
def test_retention_limits_fail_visibly_instead_of_truncated_success(setting):
    p = parser(**setting)
    record(p, 0, 3000)
    record(p, 0, 1000, "-20", "-20")
    record(p, 1000, 1000, "0", "0")
    with pytest.raises(audio.AudioAnalysisError, match=next(iter(setting))):
        record(p, 2000, 1000, "-inf", "-inf")
        p.finish()


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"immutable")
    stream = {"index": 0, "codec_type": "audio", "channels": 1, "sample_rate": "1000", "time_base": "1/1000"}
    info = MediaInfo(str(source), 3, 160, 90, 25, "25/1", True, "h264", (stream,))
    monkeypatch.setattr(audio, "probe_media", lambda *args, **kwargs: info)
    monkeypatch.setattr(audio, "run_media_command", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, "ffmpeg version test\n", ""))
    return source, info


def test_absent_audio_skips_without_starting_ffmpeg(context, monkeypatch):
    source, info = context
    def fail(*args, **kwargs):
        pytest.fail("No FFmpeg needed for absent audio")
    monkeypatch.setattr(audio, "run_media_command", fail)
    result = audio.analyze_audio(source, media_info=replace(info, has_audio=False, streams=()))
    assert result.status == "SKIPPED" and result.windows == result.events == ()
    assert result.rms is None and result.start_pts is None and result.warnings
    assert result.engine_version is None
    with pytest.raises(audio.AudioAnalysisError, match="pista"):
        audio.analyze_audio(source, audio_stream=0, media_info=replace(info, has_audio=False, streams=()))


@pytest.mark.parametrize("rate", [True, 8000.5, 0, "0", "NaN", None, "8e3"])
def test_invalid_stream_sample_rates_are_rejected(context, rate):
    source, info = context
    with pytest.raises(audio.AudioAnalysisError):
        audio.analyze_audio(source, media_info=replace(info, streams=({**info.streams[0], "sample_rate": rate},)))


def test_ffmpeg_window_buffer_is_bounded_before_start(context):
    source, info = context
    stream = {**info.streams[0], "sample_rate": "192000", "channels": 64}
    with pytest.raises(audio.AudioAnalysisError, match="32 MiB"):
        audio.analyze_audio(source, window_seconds=10, media_info=replace(info, streams=(stream,)))


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"window_seconds": True}, {"peak_dbfs": float("nan")}, {"threads": True}, {"max_windows": True}])
def test_invalid_options_fail_before_opening_input(kwargs):
    with pytest.raises(ValueError):
        audio.analyze_audio("missing.mp4", **kwargs)


@pytest.mark.parametrize("error", [DependencyError("missing"), MediaError("timeout"), MediaError("No such filter: astats")])
def test_process_errors_are_visible_not_silence(context, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(audio, "stream_media_command", fail)
    with pytest.raises(MediaError, match=str(error)):
        audio.analyze_audio(context[0])


def test_zero_exit_decoder_errors_are_rejected(context, monkeypatch):
    monkeypatch.setattr(audio, "stream_media_command", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, "", "bad audio packet"))
    with pytest.raises(audio.AudioAnalysisError, match="bad audio packet"):
        audio.analyze_audio(context[0])


def test_metadata_source_and_diagnostic_errors_are_not_ignored(context):
    source, info = context
    with pytest.raises(audio.AudioAnalysisError, match="otro archivo"):
        audio.analyze_audio(source, media_info=replace(info, path=str(source.with_name("different.mp4"))))
    with pytest.raises(audio.AudioAnalysisError, match="FFprobe"):
        audio.analyze_audio(source, media_info=replace(info, diagnostics=({"severity": "ERROR", "message": "broken"},)))


def test_internal_timeout_budget_includes_probe_version_and_decode(context, monkeypatch):
    ticks = iter((0, 0, .2, 1.1))
    monkeypatch.setattr(audio, "monotonic", lambda: next(ticks))
    with pytest.raises(audio.AudioAnalysisError, match="timeout"):
        audio.analyze_audio(context[0], timeout=1)


@pytest.mark.parametrize("overrides", [{"sample_rate": "800000000"}, {"channels": 65}])
def test_unusual_stream_headers_cannot_force_large_astats_allocations(context, monkeypatch, overrides):
    source, info = context
    def no_process(*args, **kwargs):
        pytest.fail("Invalid resource metadata must fail before starting FFmpeg")
    monkeypatch.setattr(audio, "run_media_command", no_process)
    with pytest.raises(audio.AudioAnalysisError):
        audio.analyze_audio(source, window_seconds=.01,
            media_info=replace(info, streams=({**info.streams[0], **overrides},)))


def test_only_needed_stats_are_enabled_without_hidden_sliding_buffers(context, monkeypatch):
    source, info = context
    commands = []
    def stream(command, *, on_stdout_line, timeout):
        commands.append(command)
        for line in (
            'frame:0 pts:0 pts_time:0',
            'lavfi.astats.Overall.Number_of_samples=1000',
            'frame:0 pts:0 pts_time:0',
            'lavfi.astats.Overall.Number_of_samples=1000',
            'lavfi.astats.Overall.RMS_level=-inf',
            'lavfi.astats.Overall.Peak_level=-inf',
        ):
            on_stdout_line(line)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(audio, 'stream_media_command', stream)
    result = audio.analyze_audio(source, window_seconds=1)
    filters = commands[0][commands[0].index('-af') + 1]
    assert filters.count('astats=metadata=1:reset=1:length=0:') == 2
    assert 'RMS_peak' not in filters and 'measure_perchannel=none' in filters
    assert result.rms == 0 and result.rms_dbfs is None
    assert result.config.window_seconds == 1 and result.threads == 2
