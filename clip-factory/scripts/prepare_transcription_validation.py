#!/usr/bin/env python3
"""Prepare controlled local ASR fixtures, never download voices or overwrite files.

macOS ``say`` uses voices already installed on the computer. These synthetic
fixtures exercise real recognition and timestamps; they do not measure accuracy
on natural speech. Generated speech stays in data/working for local testing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave


SPEECH = {
    "es": {
        "voice": "Paulina",
        "locale": "es_MX",
        "reference": (
            "Buenos días. Esta mañana caminamos por el parque y encontramos un perro pequeño. "
            "Después preparamos café, abrimos las ventanas y escuchamos a los pájaros. "
            "El próximo sábado visitaremos a nuestra familia y llevaremos frutas, pan y flores. "
            "Queremos hablar con calma sobre nuestros planes para el verano."
        ),
    },
    "en": {
        "voice": "Samantha",
        "locale": "en_US",
        "reference": (
            "Good morning. Today we walked through the park and found a small friendly dog. "
            "Then we made coffee, opened the windows, and listened to the birds. "
            "Next Saturday we will visit our family and bring fresh fruit, bread, and flowers. "
            "We want to talk quietly about our plans for the summer."
        ),
    },
}


def run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return completed.stdout


def fingerprint(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    details = path.stat()
    return {"sha256": digest.hexdigest(), "mtime_ns": details.st_mtime_ns, "bytes": details.st_size}


def inspect(path: Path, ffprobe: str) -> dict[str, object]:
    metadata = json.loads(run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ]))
    first_packet = json.loads(run([
        ffprobe, "-v", "error", "-show_packets", "-read_intervals", "%+#1",
        "-show_entries", "packet=stream_index,pts,pts_time,duration_time", "-of", "json", str(path),
    ])).get("packets", [])
    return {
        "format_duration_seconds": float(metadata["format"]["duration"]),
        "streams": [
            {key: stream[key] for key in (
                "index", "codec_type", "codec_name", "sample_rate", "channels", "time_base", "start_time",
            ) if key in stream}
            for stream in metadata["streams"]
        ],
        "first_packet": first_packet[0] if first_packet else None,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=root / "data/working/transcription-validation/fixtures")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    input_dir = (root / "data/input").resolve()
    working_dir = (root / "data/working").resolve()
    if directory == input_dir or input_dir in directory.parents or working_dir not in directory.parents:
        parser.error("Las muestras deben quedar dentro de data/working y fuera de data/input.")
    ffmpeg, ffprobe = shutil.which(args.ffmpeg), shutil.which(args.ffprobe)
    if not ffmpeg or not ffprobe or not Path("/usr/bin/say").is_file():
        parser.error("Requiere say de macOS y FFmpeg/FFprobe existentes; no se instalan automáticamente.")
    voices = run(["/usr/bin/say", "-v", "?"])
    for speech in SPEECH.values():
        if not any(line.startswith(speech["voice"] + " ") for line in voices.splitlines()):
            parser.error(f"La voz local {speech['voice']} no está disponible; no se descargará.")
    directory.mkdir(parents=True, exist_ok=False)
    fixtures: dict[str, dict[str, object]] = {}

    def register(name: str, path: Path, **extra: object) -> None:
        fixtures[name] = {"path": path.name, **extra, **inspect(path, ffprobe), **fingerprint(path)}

    def ff(arguments: list[str]) -> None:
        run([ffmpeg, "-nostdin", "-v", "error", "-n", *arguments])

    for language, speech in SPEECH.items():
        reference = directory / f"speech_{language}.txt"
        reference.write_text(speech["reference"] + "\n", encoding="utf-8")
        aiff = directory / f"speech_{language}.aiff"
        wav = directory / f"speech_{language}.wav"
        run(["/usr/bin/say", "-v", speech["voice"], "-r", "160", "-f", str(reference), "-o", str(aiff)])
        ff(["-i", str(aiff), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        with wave.open(str(wav), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if not 15 <= duration <= 30:
            raise RuntimeError(f"Duración TTS fuera del rango controlado: {language}, {duration:.3f}s")
        register(f"speech_{language}", wav, language=language, voice=speech["voice"],
                 locale=speech["locale"], reference=speech["reference"], reference_path=reference.name,
                 signal_duration_seconds=duration, origin_seconds=0.0,
                 source_type="controlled_local_macos_tts", natural_speech=False)
        offset = 2.5 if language == "es" else -1.25
        shifted = directory / f"speech_{language}_offset.mkv"
        ff(["-copyts", "-i", str(wav), "-af", f"asetpts=PTS+({offset})/TB", "-c:a", "pcm_s16le",
            "-avoid_negative_ts", "disabled", str(shifted)])
        register(f"speech_{language}_offset", shifted, language=language, voice=speech["voice"],
                 reference=speech["reference"], signal_duration_seconds=duration, origin_seconds=offset,
                 source_type="controlled_local_macos_tts", natural_speech=False)

    speech_es = directory / "speech_es.wav"
    duration = fixtures["speech_es"]["signal_duration_seconds"]
    cut = float(math.floor(float(duration) / 2))
    combined = directory / "speech_es_two_scenes.mkv"
    ff(["-f", "lavfi", "-i", f"color=c=red:s=320x180:r=2:d={duration}", "-i", str(speech_es),
        "-vf", f"drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,{cut})'",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "pcm_s16le",
        "-threads", "1", str(combined)])
    register("speech_es_two_scenes", combined, language="es", reference=SPEECH["es"]["reference"],
             signal_duration_seconds=duration, scene_cut_seconds=cut, origin_seconds=0.0,
             source_type="controlled_local_macos_tts_and_generated_color_video", natural_speech=False)

    for name, signal in (("silence", "anullsrc=r=16000:cl=mono"),
                         ("tone", "sine=frequency=440:sample_rate=16000")):
        wav = directory / f"{name}.wav"
        ff(["-f", "lavfi", "-i", signal, "-t", "10", "-c:a", "pcm_s16le", str(wav)])
        register(name, wav, reference="", signal_duration_seconds=10.0, origin_seconds=0.0,
                 source_type="generated_digital_signal", contains_speech=False)

    no_audio = directory / "no_audio.mkv"
    ff(["-f", "lavfi", "-i", "color=c=green:s=320x180:r=2:d=10", "-an", "-c:v", "mpeg4",
        "-threads", "1", str(no_audio)])
    register("no_audio", no_audio, reference="", source_type="generated_color_video", contains_audio=False)

    consecutive = directory / "speech_en_consecutive.wav"
    ff(["-i", str(directory / "speech_en.wav"), "-filter_complex", "[0:a]asplit=3[a][b][c];[a][b][c]concat=n=3:v=0:a=1[out]",
        "-map", "[out]", "-c:a", "pcm_s16le", str(consecutive)])
    register("speech_en_consecutive", consecutive, language="en", reference=" ".join([SPEECH["en"]["reference"]] * 3),
             signal_duration_seconds=float(fixtures["speech_en"]["signal_duration_seconds"]) * 3,
             origin_seconds=0.0, repeats=3, source_type="controlled_local_macos_tts", natural_speech=False)

    for language in SPEECH:
        for suffix in ("txt", "aiff"):
            path = directory / f"speech_{language}.{suffix}"
            fixtures[f"speech_{language}_{suffix}"] = {"path": path.name, **fingerprint(path)}
    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "description": "Controlled local synthetic speech fixtures for real whisper.cpp inference.",
        "permission_and_scope": (
            "Reference texts are original material written for this validation. Existing macOS voices "
            "were used locally without downloading voice assets. Generated recordings stay in local working "
            "storage for testing; no redistribution or commercial voice license is asserted. "
            "These are controlled TTS integration samples, not a natural-speech accuracy evaluation."
        ),
        "generator": "scripts/prepare_transcription_validation.py",
        "voices_downloaded": False,
        "network_used": False,
        "say_rate_words_per_minute": 160,
        "ffmpeg_version": run([ffmpeg, "-version"]).splitlines()[0],
        "ffprobe_version": run([ffprobe, "-version"]).splitlines()[0],
        "fixtures": fixtures,
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    print(json.dumps({name: {key: item[key] for key in ("path", "signal_duration_seconds", "origin_seconds") if key in item}
                      for name, item in fixtures.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
