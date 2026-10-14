"""Read local media metadata with ffprobe without changing the source file."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .ffmpeg import MediaError, run_media_command


@dataclass(frozen=True)
class MediaInfo:
    """Metadata, not a guarantee that every frame or audio packet decodes.

    Width/height describe encoded pixels. Display dimensions account for
    quarter-turn rotation; ``display_aspect_ratio`` also incorporates SAR.
    Unknown measurements are ``None`` rather than invented values. For VFR,
    ``fps`` is ffprobe's average, not a constant-frame-rate guarantee.
    """

    path: str
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    fps_fraction: str | None
    has_audio: bool
    codec: str | None
    streams: tuple[dict[str, Any], ...]
    format_name: str | None = None
    start_time: float | None = None
    rotation: float = 0.0
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None
    aspect_ratio: str | None = None
    display_width: int | None = None
    display_height: int | None = None
    diagnostics: tuple[dict[str, str], ...] = ()

    @property
    def status(self) -> str:
        """Probe diagnostics status, independent of metadata validation/QC.

        A zero process exit code does not guarantee clean input. Keep warnings
        distinguishable from errors without discarding useful metadata.
        """
        severities = {entry["severity"].upper() for entry in self.diagnostics}
        if "ERROR" in severities:
            return "FAIL"
        if severities & {"WARNING", "WARN"}:
            return "WARN"
        return "PASS"

    @property
    def resolution(self) -> tuple[int | None, int | None]:
        return self.width, self.height

    @property
    def has_video(self) -> bool:
        return self.width is not None and self.height is not None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["streams"] = list(result["streams"])
        result["diagnostics"] = list(result["diagnostics"])
        result["status"] = self.status
        result["video"] = self.path
        result["has_video"] = self.has_video
        result["resolution"] = (
            f"{self.width}x{self.height}" if self.has_video else None
        )
        return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _ratio(value: Any) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        ratio = Fraction(str(value).replace(":", "/"))
        numeric = float(ratio)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return ratio if math.isfinite(numeric) and numeric > 0 else None


def _fraction_string(ratio: Fraction | None) -> str | None:
    return f"{ratio.numerator}/{ratio.denominator}" if ratio is not None else None


def _duration(stream: dict[str, Any]) -> float | None:
    duration = _number(stream.get("duration"))
    if duration is not None and duration >= 0:
        return duration
    ticks = _number(stream.get("duration_ts"))
    time_base = _ratio(stream.get("time_base"))
    if ticks is not None and ticks >= 0 and time_base is not None:
        duration = ticks * float(time_base)
        return duration if math.isfinite(duration) else None
    return None


def _rotation(stream: dict[str, Any]) -> float:
    side_data = stream.get("side_data_list", [])
    if isinstance(side_data, list):
        for entry in side_data:
            if isinstance(entry, dict):
                rotation = _number(entry.get("rotation"))
                if rotation is not None:
                    return rotation % 360
    tags = stream.get("tags", {})
    if isinstance(tags, dict):
        rotation = _number(tags.get("rotate"))
        if rotation is not None:
            return rotation % 360
    return 0.0


def _classify_aspect(ratio: Fraction | None) -> str | None:
    if ratio is None:
        return None
    targets = {
        "16:9": Fraction(16, 9),
        "4:3": Fraction(4, 3),
        "21:9": Fraction(21, 9),
        "1:1": Fraction(1, 1),
        "9:16": Fraction(9, 16),
    }
    closest = min(targets, key=lambda key: abs(float(ratio / targets[key]) - 1))
    return closest if abs(float(ratio / targets[closest]) - 1) <= 0.02 else "custom"


def _local_file(path: str | Path) -> Path:
    raw_path = str(path)
    # Reject protocols before Path normalization (including file:// and pipe:).
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw_path) or raw_path == "-":
        raise MediaError("Sólo se aceptan rutas de archivos locales, no URLs ni protocolos.")
    try:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise MediaError(f"La ruta no es un archivo regular: {source}")
    except (OSError, RuntimeError, ValueError) as exc:
        raise MediaError(f"No se puede leer el archivo local {path}: {exc}") from exc
    return source


def probe_media(
    path: Path | str, *, ffprobe_bin: str = "ffprobe", timeout: float = 30.0
) -> MediaInfo:
    """Inspect a regular local file; fail explicitly on unreadable/bad metadata.

    The protocol allowlist blocks network references in playlist/container input.
    This performs metadata inspection only, without decoding all frames or
    generating packets, clips, thumbnails or output media.
    """
    source = _local_file(path)
    command = [
        ffprobe_bin,
        "-v", "error",
        "-protocol_whitelist", "file,pipe",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(source),
    ]
    result = run_media_command(command, timeout=timeout)
    # This command explicitly uses -v error. Anything emitted on stderr is an
    # error diagnostic even when ffprobe returns useful JSON and exit code 0.
    # Keep that policy here: normal FFmpeg progress also uses stderr, so the
    # generic process adapter must not classify all stderr output as a failure.
    stderr = result.stderr.strip()
    diagnostics = ({"severity": "ERROR", "message": stderr},) if stderr else ()

    def invalid_metadata(message: str) -> MediaError:
        detail = f" Diagnóstico de ffprobe: {stderr}" if stderr else ""
        return MediaError(
            message + detail,
            command=command,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise invalid_metadata(f"ffprobe devolvió JSON inválido para {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise invalid_metadata(f"ffprobe devolvió metadata inválida para {source}.")
    streams = payload.get("streams")
    container = payload.get("format", {})
    if (
        not isinstance(streams, list)
        or not streams
        or not all(isinstance(stream, dict) for stream in streams)
        or not isinstance(container, dict)
    ):
        raise invalid_metadata(f"ffprobe no devolvió streams válidos para {source}.")
    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and not (isinstance(stream.get("disposition"), dict)
                 and stream["disposition"].get("attached_pic"))
    ]
    # Prefer the default video track while retaining all tracks in metadata.
    video = next(
        (stream for stream in videos
         if isinstance(stream.get("disposition"), dict)
         and stream["disposition"].get("default")),
        videos[0] if videos else {},
    )
    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    frame_rate = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    fps = float(frame_rate) if frame_rate is not None else None
    duration = _duration(container)
    if duration is None:
        durations = [value for stream in streams if (value := _duration(stream)) is not None]
        duration = max(durations) if durations else None
    rotation = _rotation(video)
    sar = _ratio(video.get("sample_aspect_ratio"))
    dar = _ratio(video.get("display_aspect_ratio"))
    if width is not None and height is not None:
        dar = Fraction(width, height) * (sar or Fraction(1, 1))
    display_width, display_height = width, height
    if rotation in (90.0, 270.0):
        display_width, display_height = height, width
        dar = 1 / dar if dar is not None else None
    start_time = _number(container.get("start_time"))
    if start_time is None:
        start_time = _number(video.get("start_time"))
    return MediaInfo(
        path=str(source),
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        fps_fraction=_fraction_string(frame_rate),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        codec=video.get("codec_name"),
        streams=tuple(streams),
        format_name=container.get("format_name"),
        start_time=start_time,
        rotation=rotation,
        sample_aspect_ratio=_fraction_string(sar),
        display_aspect_ratio=_fraction_string(dar),
        aspect_ratio=_classify_aspect(dar),
        display_width=display_width,
        display_height=display_height,
        diagnostics=diagnostics,
    )
