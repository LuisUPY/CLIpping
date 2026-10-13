"""Basic metadata checks only. Complete output QC belongs to a later phase."""

from __future__ import annotations

import math
from typing import Any

from .probe import MediaInfo


def validate_metadata(info: MediaInfo, *, require_audio: bool = False) -> dict[str, Any]:
    """Check video metadata without claiming playback/decode/black-frame QC.

    Source videos may legitimately be silent, so audio is optional unless the
    caller explicitly requests it. This function never reads or changes media.
    """
    checks = {
        "video_present": info.has_video,
        "resolution_valid": (
            info.width is not None and info.width > 0
            and info.height is not None and info.height > 0
        ),
        "duration_valid": (
            info.duration is not None and math.isfinite(info.duration) and info.duration > 0
        ),
        "fps_valid": info.fps is not None and math.isfinite(info.fps) and info.fps > 0,
        "video_codec_present": bool(info.codec),
    }
    if require_audio:
        checks["audio_present"] = info.has_audio
    errors = [f"Falló la validación básica de metadata: {name}." for name, passed in checks.items() if not passed]
    return {
        "status": "FAIL" if errors else "PASS",
        "scope": "metadata_only",
        "checks": checks,
        "errors": errors,
        "warnings": [
            "No se validaron decodificación completa, frames negros, subtítulos ni formato de exportación."
        ],
    }
