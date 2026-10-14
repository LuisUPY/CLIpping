"""Measure deterministic rules separately from local multimedia analysis.

--video reads a local source once per analyzer and reuses the dataclasses.
--stress-events builds explicit synthetic metadata, not multimedia/ASR output.
Nothing is installed, downloaded, edited or persisted outside stdout.
"""

import argparse
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.analysis.audio import AudioAnalysisConfig, AudioAnalysisResult, AudioEvent, analyze_audio
from clip_factory.analysis.moments import MomentDetectionConfig, detect_moments
from clip_factory.analysis.scenes import SceneChange, SceneDetectionResult, detect_scenes
from clip_factory.analysis.transcript import TranscriptConfig, transcribe_media
from clip_factory.cli.main import _local_transcript_settings, _moment_video_bounds
from clip_factory.config.loader import load_config
from clip_factory.media.probe import probe_media


def signature(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest(), path.stat().st_mtime_ns


def stress_inputs(total):
    count = total // 2
    changes, events = [], []
    for index in range(1, count + 1):
        start = float(Fraction(index, 100))
        end = float(Fraction(index, 100) + Fraction(1, 200))
        changes.append(SceneChange(index * 10, start, start, 80))
        events.append(AudioEvent("peak", index * 80, index * 80 + 40,
                                 start, end, start, end, 1, -1, -4, None))
    duration = float(Fraction(count, 100) + 10)
    scenes = SceneDetectionResult("synthetic-metadata", 0, "1/1000", 0, tuple(changes),
                                  count + 1, 10, 0, 300, 4, "synthetic-contract", 0)
    audio = AudioAnalysisResult(
        source="synthetic-metadata", status="PASS", stream_index=1, sample_rate=8000,
        channels=1, source_time_base="1/1000", time_base="1/8000", start_pts=0,
        window_samples=40, timestamp_tolerance_samples=8, windows=(), events=tuple(events),
        samples_analyzed=round(duration * 8000), rms=.1, rms_dbfs=-20, peak_dbfs=-1,
        config=AudioAnalysisConfig(), threads=4, warnings=(), engine_version="synthetic-contract", elapsed_seconds=0,
    )
    return scenes, audio, duration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video", type=Path)
    group.add_argument("--stress-events", type=int, help="Even number, 2..1000000; generated metadata only")
    parser.add_argument("--transcribe", action="store_true", help="Explicit optional ASR for --video")
    parser.add_argument("--repeats", type=int, choices=range(1, 1001), default=3)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    config = load_config(root=args.root, profile="mac_m1")
    transcript = None
    timings = {}
    notes = []
    before = None
    if args.stress_events is not None:
        if args.transcribe or not 2 <= args.stress_events <= 1_000_000 or args.stress_events % 2:
            parser.error("Stress requires an even event count 2..1000000 and no --transcribe")
        scenes, audio, duration = stress_inputs(args.stress_events)
        origin, bounds_source = 0.0, "explicit_synthetic_fixture"
        source = "synthetic-metadata"
        settings = MomentDetectionConfig(max_events=args.stress_events)
        mode = "generated_metadata_no_multimedia_no_asr"
    else:
        source = args.video.expanduser().resolve(strict=True)
        before = signature(source)
        info = probe_media(source, ffprobe_bin=config.ffprobe_bin, timeout=config.probe_timeout)
        scenes = detect_scenes(source, media_info=info, **config.data["analysis"]["scenes"],
                               ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                               threads=config.data["runtime"]["threads"])
        audio = analyze_audio(source, media_info=info, **config.data["analysis"]["audio"],
                              ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                              threads=config.data["runtime"]["threads"])
        timings.update(scenes_seconds=scenes.elapsed_seconds, audio_seconds=audio.elapsed_seconds)
        if args.transcribe:
            transcript = transcribe_media(
                source, media_info=info, config=TranscriptConfig(**_local_transcript_settings(
                    config.data["analysis"]["transcription"], config.root)),
                root=config.root, input_dir=config.paths["input"], working_dir=config.paths["working"],
                ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
                threads=config.data["runtime"]["threads"],
            )
            timings.update(transcription_seconds=transcript.elapsed_seconds,
                           transcription_audio_seconds=transcript.audio_processing_seconds,
                           transcription_inference_seconds=transcript.inference_seconds)
            notes.extend(transcript.warnings)
        origin, duration, bounds_source, bounds_warnings = _moment_video_bounds(info, scenes)
        notes.extend(bounds_warnings)
        source = str(source)
        settings = MomentDetectionConfig(**config.data["analysis"]["moments"])
        mode = "real_local_analyzers_reused_in_memory"
    results = [detect_moments(source=source, duration=duration, origin_seconds=origin,
                              scenes=scenes, audio=audio, transcript=transcript, config=settings)
               for _ in range(args.repeats)]
    elapsed = [result.elapsed_seconds for result in results]
    result = results[-1]
    canonical = json.dumps([asdict(candidate) for candidate in result.moments], sort_keys=True, allow_nan=False)
    assert all(json.dumps([asdict(candidate) for candidate in run.moments], sort_keys=True, allow_nan=False)
               == canonical for run in results), "Non-deterministic candidate output"
    preserved = None if before is None else before == signature(Path(source))
    if before is not None and not preserved:
        raise RuntimeError("Original changed during benchmark")
    unit = 1024 * 1024 if sys.platform == "darwin" else 1024
    report = {
        "mode": mode, "source": source, "platform": platform.platform(), "python": platform.python_version(),
        "repeats": args.repeats, "upstream_once": timings, "duration_seconds": duration,
        "origin_seconds": origin, "duration_source": bounds_source,
        "moment_detection_seconds": {"median": statistics.median(elapsed), "min": min(elapsed), "max": max(elapsed)},
        "events_considered": result.events_considered, "candidates": len(result.moments),
        "candidates_suppressed": result.candidates_suppressed,
        "python_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit,
        "largest_child_peak_rss_mib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / unit,
        "memory_note": "Individual process high-water marks; includes inputs/results and prior stages, not simultaneous total RAM.",
        "original_preserved": preserved, "warnings": list(dict.fromkeys((*notes, *result.warnings))),
        "moments": [asdict(candidate) for candidate in result.moments],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
