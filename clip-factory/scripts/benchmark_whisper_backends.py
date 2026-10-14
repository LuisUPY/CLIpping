"""Benchmark a local whisper-cli on unchanged reference WAVs, CPU or Metal.

No model or dependency is downloaded. Runs are strictly serial. This measures
one native full-file whisper-cli process per recording/repetition; it does not
measure Clip Factory's bounded chunk adapter or FFmpeg preprocessing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
from time import monotonic
import unicodedata
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.config.loader import load_config
from clip_factory.utils.files import FileSafetyError, is_within, secure_directory


def signature(path: Path) -> dict:
    """Hash while checking that the local source did not change during reading."""
    before = path.stat()
    if not path.is_file():
        raise ValueError(f"Not a local regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        raise ValueError(f"Source changed while hashing: {path}")
    return {"sha256": digest.hexdigest(), "mtime_ns": after.st_mtime_ns,
            "bytes": after.st_size, "device": after.st_dev, "inode": after.st_ino}


def reference_signature(path: Path, fixture: dict, name: str) -> dict:
    """Validate portable content identity and retain this run's exact metadata."""
    current = signature(path)
    # A copied reference can have a new mtime. The manifest identifies bytes;
    # current mtime/device/inode remain strict before/after preservation checks.
    for field in ("sha256", "bytes"):
        if current[field] != fixture[field]:
            raise ValueError(f"Fixture differs from immutable manifest ({field}): {name}")
    return current


def local_file(value: str | Path, base: Path) -> Path:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", str(value)) or "\x00" in str(value):
        raise ValueError("Only existing local files are supported")
    path = Path(value).expanduser()
    path = (base / path if not path.is_absolute() else path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a regular local file: {path}")
    return path


def normalized_words(text: str) -> list[str]:
    # Case/punctuation normalization retains accents; document this with WER.
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold(), re.UNICODE)


def word_error_rate(reference: str, hypothesis: str) -> dict:
    expected, actual = normalized_words(reference), normalized_words(hypothesis)
    if not expected:
        raise ValueError("A nonempty speech reference is required for WER")
    previous = list(range(len(actual) + 1))
    for row, word in enumerate(expected, 1):
        current = [row]
        for column, candidate in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (word != candidate)))
        previous = current
    return {"normalized_wer": previous[-1] / len(expected),
            "word_edit_distance": previous[-1], "reference_words": len(expected),
            "hypothesis_words": len(actual)}


def parse_time(text: str) -> dict:
    # macOS /usr/bin/time -l uses bytes, unlike resource.ru_maxrss on Linux.
    real = re.search(r"(?m)^\s*([0-9.]+)\s+real\s+[0-9.]+\s+user\s+[0-9.]+\s+sys", text)
    rss = re.search(r"(?m)^\s*(\d+)\s+maximum resident set size\s*$", text)
    footprint = re.search(r"(?m)^\s*(\d+)\s+peak memory footprint\s*$", text)
    return {"real_seconds": float(real.group(1)) if real else None,
            "peak_rss_bytes": int(rss.group(1)) if rss else None,
            "peak_memory_footprint_bytes": int(footprint.group(1)) if footprint else None}


def parse_timings(stderr: str) -> dict:
    result = {}
    for key in ("load", "encode", "decode", "total"):
        found = re.search(rf"(?m)^whisper_print_timings:\s+{key}\s+time\s*=\s*([0-9.]+)\s+ms", stderr)
        result[key + "_seconds"] = float(found.group(1)) / 1000 if found else None
    return result


def backend_evidence(stderr: str, backend: str) -> dict:
    use_gpu = re.search(r"use[ _]gpu\s*=\s*([01])\b", stderr, re.I)
    metal_lines = [line for line in stderr.splitlines()
                   if re.search(r"ggml[^\n]*metal|metal[^\n]*(?:device|gpu)", line, re.I)]
    selected = [line for line in metal_lines if re.search(r"Apple\s+M1\b", line, re.I)
                and re.search(r"GPU\s+name|(?:using|selected|found)\s+(?:device|GPU)|device\s*[:=]", line, re.I)]
    failures = [line for line in stderr.splitlines()
                if re.search(r"(?:failed|error|fallback|falling back).*(?:metal|gpu)|(?:metal|gpu).*(?:failed|error|fallback)", line, re.I)]
    requested_gpu = int(use_gpu.group(1)) if use_gpu else None
    confirmed = bool(requested_gpu == 1 and selected and metal_lines and not failures)
    cpu_confirmed = requested_gpu == 0
    return {"requested": backend, "reported_use_gpu": requested_gpu,
            "metal_m1_confirmed": confirmed, "cpu_confirmed": cpu_confirmed,
            "selected_device_lines": selected, "metal_log_lines": metal_lines,
            "backend_failure_lines": failures,
            "status": ("CONFIRMED" if (confirmed if backend == "metal" else cpu_confirmed)
                       else "UNAVAILABLE_OR_UNCONFIRMED")}


def read_small(path: Path, limit: int = 16 * 1024 * 1024) -> str:
    if path.stat().st_size > limit:
        raise ValueError(f"Benchmark artifact exceeds {limit} bytes: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def json_value(text: str):
    def reject(value):
        raise ValueError(f"Nonfinite JSON value: {value}")
    return json.loads(text, parse_constant=reject)


def run_one(args, folder, name: str, source: Path, fixture: dict, model: Path,
            executable: str, repetition: int) -> dict:
    stem = f"{name}.{args.backend}.{repetition:02d}"
    leaves = {key: f"{stem}.{suffix}" for key, suffix in (
        ("stdout", "stdout.txt"), ("stderr", "stderr.txt"),
        ("time", "time.txt"), ("json", "json"))}
    descriptors = {}
    identities = {}
    try:
        for key, leaf in leaves.items():
            descriptors[key] = folder.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            identities[key] = os.fstat(descriptors[key])
        command = [executable, "-m", str(model), "-f", str(source), "-t", "4", "-l", "auto",
                   "-ojf", "-of", str(folder.path / stem), "--no-fallback", "--suppress-nst"]
        if args.backend == "cpu":
            command.append("--no-gpu")
        timed_command = ["/usr/bin/time", "-l", "-o", str(folder.path / leaves["time"]), *command]
        started = monotonic()
        timed_out = False
        with os.fdopen(os.dup(descriptors["stdout"]), "wb") as stdout, \
                os.fdopen(os.dup(descriptors["stderr"]), "wb") as stderr:
            process = subprocess.Popen(timed_command, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                returncode = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait()
        outer_wall = monotonic() - started
        folder.check_identity()
        for key, leaf in leaves.items():
            current = folder.stat(leaf)
            expected = identities[key]
            if current is None or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise FileSafetyError(f"Benchmark artifact was replaced: {leaf}")
        stderr_text = read_small(folder.path / leaves["stderr"])
        measurement = parse_time(read_small(folder.path / leaves["time"]))
        backend = backend_evidence(stderr_text, args.backend)
        payload, parse_error = None, None
        try:
            payload = json_value(read_small(folder.path / leaves["json"]))
            if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
                raise ValueError("whisper-cli did not produce the full transcription JSON contract")
        except (ValueError, UnicodeError, RecursionError) as error:
            parse_error = str(error)
        segments = payload.get("transcription", []) if isinstance(payload, dict) else []
        text = " ".join(part["text"].strip() for part in segments
                        if isinstance(part, dict) and isinstance(part.get("text"), str)).strip()
        reported = payload.get("result", {}) if isinstance(payload, dict) else {}
        language = reported.get("language") if isinstance(reported, dict) else None
        status = "PASS"
        if timed_out:
            status = "TIMEOUT"
        elif returncode != 0 or parse_error:
            status = "FAIL"
        elif backend["status"] != "CONFIRMED":
            status = "BACKEND_UNAVAILABLE_OR_UNCONFIRMED"
        elif not text or language != fixture["language"]:
            status = "RECOGNITION_FAIL"
        duration = fixture["signal_duration_seconds"]
        real = measurement["real_seconds"]
        return {"fixture": name, "repetition": repetition, "status": status,
                "command": command, "returncode": returncode, "timed_out": timed_out,
                "duration_seconds": duration, "outer_wall_seconds": outer_wall,
                **measurement, "rtf": real / duration if real is not None else None,
                "whisper_reported_timings": parse_timings(stderr_text), "backend": backend,
                "language": language, "text": text, "reference": fixture["reference"],
                "segment_count": len(segments), **word_error_rate(fixture["reference"], text),
                "json_parse_error": parse_error, "artifacts": leaves,
                "execution_mode": "native_full_file_one_process",
                "note": "No FFmpeg preprocessing or Clip Factory adapter chunking is timed."}
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True, help="Existing local GGML model")
    parser.add_argument("--executable", default="whisper-cli")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory strictly inside configured working")
    parser.add_argument("--backend", choices=("cpu", "metal"), required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 10), default=3)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--include-consecutive", action="store_true",
                        help="Also run speech_en_consecutive as one native full-file call per repeat")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This benchmark uses macOS /usr/bin/time -l byte units")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    config = load_config(root=args.root, profile="mac_m1")
    manifest_path = local_file(args.manifest, config.root)
    manifest = json_value(manifest_path.read_text(encoding="utf-8"))
    fixtures = manifest["fixtures"]
    names = ["speech_es", "speech_en"] + (["speech_en_consecutive"] if args.include_consecutive else [])
    model = local_file(args.model, config.root)
    executable = shutil.which(args.executable)
    if executable is None:
        parser.error("whisper-cli is unavailable; nothing was installed")
    sources, before = {}, {}
    for name in names:
        fixture = fixtures[name]
        source = local_file(fixture["path"], manifest_path.parent)
        if source.suffix.lower() != ".wav" or not normalized_words(fixture["reference"]):
            raise ValueError(f"Expected a reference speech WAV: {name}")
        duration = fixture["signal_duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Invalid manifest duration: {name}")
        with wave.open(str(source), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError(f"Expected mono PCM16 16 kHz WAV: {name}")
            if abs(wav.getnframes() / wav.getframerate() - duration) > 0.001:
                raise ValueError(f"Manifest/WAV duration mismatch: {name}")
        sources[name] = source
        before[name] = reference_signature(source, fixture, name)
    model_before = signature(model)
    manifest_before = signature(manifest_path)
    destination = args.output_dir.expanduser()
    if not destination.is_absolute():
        destination = config.root / destination
    destination = Path(os.path.abspath(destination))
    working = config.paths["working"]
    if destination == working or not is_within(destination, working):
        raise FileSafetyError("--output-dir must be a new child directory of configured working")
    if any(is_within(source, destination) for source in sources.values()) or is_within(model, destination):
        raise FileSafetyError("The output directory cannot contain a benchmark source/model")
    with secure_directory(destination.parent, root=config.root, input_dir=config.paths["input"], create=True) as parent:
        os.mkdir(destination.name, 0o700, dir_fd=parent.fd)  # Intentionally refuses reuse.
        os.fsync(parent.fd)
    measurements, preserved = [], False
    with secure_directory(destination, root=config.root, input_dir=config.paths["input"]) as folder:
        try:
            for name in names:
                for repetition in range(1, args.repeats + 1):
                    if signature(sources[name]) != before[name]:
                        raise ValueError(f"Original changed before run: {name}")
                    measurements.append(run_one(args, folder, name, sources[name], fixtures[name],
                                                model, executable, repetition))
                    if signature(sources[name]) != before[name]:
                        raise ValueError(f"Original changed during run: {name}")
        finally:
            after = {name: signature(source) for name, source in sources.items()}
            preserved = before == after and model_before == signature(model) and manifest_before == signature(manifest_path)
            summary = {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                       "platform": platform.platform(), "machine": platform.machine(),
                       "backend_requested": args.backend, "threads": 4, "repeats": args.repeats,
                       "model": str(model), "model_signature": model_before,
                       "manifest": str(manifest_path), "source_signatures_before": before,
                       "source_signatures_after": after, "originals_preserved": preserved,
                       "measurements": measurements,
                       "scope": "Serial native full-file whisper-cli; excludes PCM preprocessing and the Clip Factory chunk adapter.",
                       "memory_note": "macOS /usr/bin/time maximum RSS and footprint of one process, bytes. Not whole-system RAM or a separate GPU allocation measurement.",
                       "wer_normalization": "Unicode NFKC, casefold, Unicode word tokens, punctuation removed; accents retained.",
                       "accuracy_scope": manifest.get("permission_and_scope"),
                       "summaries": {}}
            for name in names:
                accepted = [row for row in measurements if row["fixture"] == name and row["status"] == "PASS"]
                summary["summaries"][name] = {"accepted_repetitions": len(accepted)}
                for key in ("real_seconds", "rtf", "peak_rss_bytes", "peak_memory_footprint_bytes", "normalized_wer"):
                    values = [row[key] for row in accepted if row[key] is not None]
                    summary["summaries"][name][key] = ({"median": statistics.median(values), "min": min(values), "max": max(values)}
                                                        if values else None)
            descriptor = folder.open("benchmark.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
        if not preserved:
            raise ValueError("A source/model/manifest changed during the benchmark")
    print(str(destination / "benchmark.json"))
    return 0 if all(row["status"] == "PASS" for row in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
