"""Local, explicit preprocessing/inference benchmarks; never download a model.

Each repetition uses a fresh worker, so RSS high-water marks are independent.
Audio-only uses the same timestamp auditor/PCM stream as the transcription
adapter and writes one bounded WAV at a time. It performs no recognition.
"""

import argparse
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
from time import monotonic
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.analysis.transcript import (
    TranscriptConfig, TranscriptError, _Timeline, _chunk_workspace, _decode_chunks,
    _pcm_values, _quiet, transcribe_media,
)
from clip_factory.config.loader import load_config
from clip_factory.media.probe import probe_media
from clip_factory.utils.files import secure_directory


def worker(args):
    config = load_config(root=args.root, profile="mac_m1")
    info = probe_media(args.source, ffprobe_bin=config.ffprobe_bin)
    start = monotonic()
    if not args.audio_only:
        result = transcribe_media(
            args.source, config=TranscriptConfig(model=args.model, executable=args.executable,
                                                 timeout=args.timeout, language=args.language),
            media_info=info, ffmpeg_bin=config.ffmpeg_bin, ffprobe_bin=config.ffprobe_bin,
            threads=config.data["runtime"]["threads"], root=config.root,
            input_dir=config.paths["input"], working_dir=config.paths["working"],
        )
        measurement = {
            "status": result.status, "skip_reason": result.skip_reason,
            "audio_processing_seconds": result.audio_processing_seconds if result.status == "PASS" else None,
            "inference_seconds": result.inference_seconds if result.inference_seconds > 0 else None,
            "inference_status": "MEASURED" if result.inference_seconds > 0 else "NOT_EXECUTED",
            "audio_duration_seconds": result.samples_analyzed / 16000,
            "segments": len(result.segments), "warnings": list(result.warnings),
            "model": result.model, "engine_version": result.engine_version,
        }
    else:
        streams = [s for s in info.streams if s.get("codec_type") == "audio"]
        if not streams:
            raise TranscriptError("Sin pista de audio para benchmark.")
        stream = next((s for s in streams if s.get("disposition", {}).get("default")), streams[0])
        rate = int(stream["sample_rate"])
        base = Fraction(stream["time_base"])
        timeline = _Timeline(rate, max(1, math.ceil(base * rate)), max(1, math.ceil(base * 16000)))
        chunks = 0
        with _chunk_workspace(config.root, config.paths["input"], config.paths["working"]) as temporary:
            def consume(pcm, offset):
                nonlocal chunks
                chunks += 1
                if _quiet(_pcm_values(pcm), -50):
                    return
                # The private directory and exclusive open preserve originals.
                with secure_directory(temporary, root=config.root, input_dir=config.paths["input"]) as folder:
                    descriptor = folder.open("chunk.wav", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(descriptor, "wb") as handle:
                        with wave.open(handle, "wb") as wav:
                            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                            wav.writeframes(pcm)
                    folder.unlink("chunk.wav", expected=folder.stat("chunk.wav"))

            filters = (f"asettb=expr=1/{rate},ashowinfo@source,"
                       "aformat=sample_fmts=s16:channel_layouts=mono,aresample=16000,"
                       "asettb=expr=1/16000,ashowinfo@pcm")
            command = [config.ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "level+info", "-nostats",
                       "-xerror", "-copyts", "-threads", "4", "-filter_threads", "1",
                       "-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-i", info.path,
                       "-map", f"0:{stream['index']}", "-vn", "-sn", "-dn", "-af", filters,
                       "-c:a", "pcm_s16le", "-threads", "1", "-f", "s16le", "pipe:1"]
            _decode_chunks(command, timeline, 30 * 16000, start + args.timeout, consume)
        measurement = {
            "status": "PASS_AUDIO_ONLY", "audio_processing_seconds": monotonic() - start,
            "audio_duration_seconds": timeline.samples / 16000, "chunks": chunks,
            "inference_seconds": None, "inference_status": "PENDING_NOT_EXECUTED",
            "warnings": timeline.warnings,
        }
    rss_unit = 1024 * 1024 if sys.platform == "darwin" else 1024
    measurement.update({
        "wall_seconds": monotonic() - start,
        "python_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / rss_unit,
        "largest_child_peak_rss_mib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / rss_unit,
        "child_memory_note": "Mayor hijo individual; FFmpeg/FFprobe en audio-only, incluye whisper-cli al inferir. No es RAM total ni memoria GPU.",
    })
    return measurement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--audio-only", action="store_true", help="Medir PCM/PTS/WAV sin ASR, sin modelo")
    parser.add_argument("--model", help="Modelo local existente; no se descarga")
    parser.add_argument("--executable", default="whisper-cli")
    parser.add_argument("--language", choices=("auto", "es", "en"), default="auto")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--repeats", type=int, choices=range(1, 10), default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False, allow_nan=False))
        return 0
    def signature():
        digest = hashlib.sha256()
        with args.source.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest(), args.source.stat().st_mtime_ns

    before = signature()
    measurements = []
    for _ in range(args.repeats):
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"],
                                 capture_output=True, text=True, check=True, timeout=args.timeout + 60)
        measurements.append(json.loads(process.stdout))
    if signature() != before:
        raise TranscriptError("El original cambió durante el benchmark.")
    summary = {
        "source": str(args.source.resolve()), "platform": platform.platform(),
        "python": platform.python_version(), "mode": "audio_only" if args.audio_only else "transcription",
        "measurements": measurements, "source_preserved": True,
    }
    for key in ("audio_processing_seconds", "inference_seconds", "python_peak_rss_mib", "largest_child_peak_rss_mib"):
        values = [r[key] for r in measurements if r.get(key) is not None]
        summary[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)} if values else None
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
