"""Load layered YAML configuration without creating or modifying files."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import os
from pathlib import Path, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping


class ConfigError(ValueError):
    """A configuration cannot safely be used by the application."""


_DEFAULTS: dict[str, Any] = {
    "paths": {
        "input": "data/input",
        "working": "data/working",
        "clips": "data/clips",
        "exports": "data/exports",
        "thumbnails": "data/thumbnails",
        "subtitles": "data/subtitles",
        "archive": "data/archive",
        "database": "database/clip_factory.db",
        "logs": "logs",
    },
    "media": {"ffmpeg_bin": "ffmpeg", "ffprobe_bin": "ffprobe", "probe_timeout": 30.0},
    "logging": {"level": "INFO"},
    "runtime": {"device": "cpu", "threads": 4},
    "scoring": {"weights": {
        "hook": 1.0,
        "emotion": 1.0,
        "surprise": 1.0,
        "activity": 1.0,
        "audio": 1.0,
        "context": 1.0,
        "duration": 1.0,
        "completeness": 1.0,
    }},
    "editing": {
        "width": 1080,
        "height": 1920,
        "mode": "pad",
        "video_codec": "h264",
        "audio_codec": "aac",
    },
}
_BUILTIN_PROFILES = {
    "mac_m1": {"runtime": {"device": "cpu", "threads": 4}, "media": {"probe_timeout": 30.0}}
}


@dataclass(frozen=True)
class AppConfig:
    """Effective settings; filesystem paths are resolved and safety checked."""

    root: Path
    paths: Mapping[str, Path]
    ffmpeg_bin: str
    ffprobe_bin: str
    probe_timeout: float
    log_level: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-serializable copy, keeping paths portable."""
        return deepcopy(self.data)


def _project_marker(path: Path) -> bool:
    """Identify our project using the literal name in its TOML project table.

    Only this marker is needed, so a TOML dependency is unnecessary on Python 3.10.
    The complete pyproject remains the responsibility of the packaging tools.
    """
    candidate = path / "pyproject.toml"
    if not candidate.is_file():
        return False
    try:
        contents = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read project marker {candidate}: {exc}") from exc
    section = re.search(r"(?ms)^\s*\[project\]\s*\n(.*?)(?=^\s*\[|\Z)", contents)
    return bool(section and re.search(
        r"(?m)^\s*name\s*=\s*(['\"])clip-factory\1\s*(?:#.*)?$", section.group(1)
    ))


def _resolve_root(root: Path | str | None) -> Path:
    explicit = root if root is not None else os.environ.get("CLIP_FACTORY_ROOT")
    try:
        if explicit is not None:
            if not str(explicit).strip():
                raise ConfigError("Project root cannot be empty")
            selected = Path(explicit).expanduser().resolve()
        else:
            current = Path.cwd().resolve()
            selected = next(
                (p for p in (current, *current.parents) if _project_marker(p)), None
            )
            if selected is None:
                repository = Path(__file__).resolve().parents[3]
                selected = repository if (repository / "config/default.yaml").is_file() else current
        if selected.exists() and not selected.is_dir():
            raise ConfigError(f"Project root must be a directory: {selected}")
        return selected
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Cannot resolve project root: {exc}") from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required to read YAML configuration. Install the project dependencies "
            "with 'python -m pip install -e .'. Nothing was installed automatically."
        ) from exc
    try:
        with path.open("r", encoding="utf-8") as handle:
            result = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read YAML configuration {path}: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ConfigError(f"Configuration must be a YAML mapping: {path}")
    return result


def _merge(base: dict[str, Any], overrides: dict[str, Any], location: str = "config") -> None:
    for key, value in overrides.items():
        if not isinstance(key, str) or key not in base:
            raise ConfigError(f"Unknown configuration key: {location}.{key}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{location}.{key} must be a mapping")
            _merge(base[key], value, f"{location}.{key}")
        else:
            base[key] = deepcopy(value)


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{location} must be a nonempty string without null characters")
    return value


def _number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{location} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ConfigError(f"{location} must be a finite number") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{location} must be a finite number")
    return number


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _validate_paths(root: Path, configured: dict[str, Any]) -> Mapping[str, Path]:
    resolved: dict[str, Path] = {}
    for key, value in configured.items():
        value = _text(value, f"paths.{key}")
        relative = Path(value)
        portable = PureWindowsPath(value)
        if relative.is_absolute() or portable.drive or portable.root:
            raise ConfigError(f"paths.{key} must be relative to the project root")
        if ".." in relative.parts or ".." in portable.parts:
            raise ConfigError(f"paths.{key} cannot contain '..'")
        try:
            destination = (root / relative).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigError(f"Cannot resolve paths.{key}: {exc}") from exc
        if not _contains(root, destination):
            raise ConfigError(f"paths.{key} escapes the project root, possibly through a symlink")
        if destination.exists():
            if key == "database" and not destination.is_file():
                raise ConfigError("paths.database must point to a file")
            if key != "database" and not destination.is_dir():
                raise ConfigError(f"paths.{key} must point to a directory")
        resolved[key] = destination

    # data/input remains protected even if a custom input directory is configured.
    try:
        protected = (resolved["input"], (root / "data/input").resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Cannot resolve immutable data/input: {exc}") from exc
    writable = {key: path for key, path in resolved.items() if key != "input"}
    for key, path in writable.items():
        for original in protected:
            if _contains(original, path) or _contains(path, original):
                raise ConfigError(f"paths.{key} overlaps immutable input")
    destinations = list(writable.items())
    for index, (key, path) in enumerate(destinations):
        for other_key, other_path in destinations[index + 1:]:
            if _contains(path, other_path) or _contains(other_path, path):
                raise ConfigError(f"Writable paths overlap: paths.{key} and paths.{other_key}")
    return MappingProxyType(resolved)


def _validate(data: dict[str, Any], root: Path) -> Mapping[str, Path]:
    media = data["media"]
    for key in ("ffmpeg_bin", "ffprobe_bin"):
        _text(media[key], f"media.{key}")
    timeout = _number(media["probe_timeout"], "media.probe_timeout")
    if timeout <= 0:
        raise ConfigError("media.probe_timeout must be greater than zero")
    media["probe_timeout"] = timeout
    level = _text(data["logging"]["level"], "logging.level").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    data["logging"]["level"] = level
    device = data["runtime"]["device"]
    if device not in ("cpu", "auto", "mps"):
        raise ConfigError("runtime.device must be cpu, auto or mps; CUDA is not required or supported")
    threads = data["runtime"]["threads"]
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ConfigError("runtime.threads must be a positive integer")
    weights = data["scoring"]["weights"]
    for key, value in weights.items():
        number = _number(value, f"scoring.weights.{key}")
        if number < 0:
            raise ConfigError(f"scoring.weights.{key} cannot be negative")
        weights[key] = number
    try:
        weight_total = math.fsum(weights.values())
    except OverflowError as exc:
        raise ConfigError("scoring.weights sum must be finite") from exc
    if not math.isfinite(weight_total):
        raise ConfigError("scoring.weights sum must be finite")
    if weight_total <= 0:
        raise ConfigError("scoring.weights must contain at least one positive weight")
    editing = data["editing"]
    for key in ("width", "height"):
        value = editing[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 2 or value % 2:
            raise ConfigError(f"editing.{key} must be a positive even integer")
    if editing["mode"] not in ("pad", "crop"):
        raise ConfigError("editing.mode must be pad or crop")
    if editing["video_codec"] != "h264" or editing["audio_codec"] != "aac":
        raise ConfigError("editing codecs must be h264 and aac for the configured export format")
    return _validate_paths(root, data["paths"])


def load_config(
    root: Path | str | None = None,
    config_path: Path | str | None = None,
    profile: str | None = None,
) -> AppConfig:
    """Load defaults, project YAML, explicit YAML and an optional profile.

    Explicit configuration paths are relative to the selected project root.
    Root selection: argument, CLIP_FACTORY_ROOT, cwd project ancestor, source
    repository, cwd. Missing default YAML is valid for a newly initialized root.
    No directories are created, no models are downloaded, and no tools are run.
    """
    project_root = _resolve_root(root)
    effective = deepcopy(_DEFAULTS)
    default_path = project_root / "config/default.yaml"
    if default_path.exists():
        _merge(effective, _load_yaml(default_path))
    if config_path is not None:
        explicit_path = Path(config_path).expanduser()
        if not explicit_path.is_absolute():
            explicit_path = project_root / explicit_path
        _merge(effective, _load_yaml(explicit_path))
    if profile is not None:
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", profile):
            raise ConfigError("Profile must be a simple name, for example mac_m1")
        profile_path = project_root / "config/profiles" / f"{profile}.yaml"
        if profile_path.exists():
            _merge(effective, _load_yaml(profile_path))
        elif profile in _BUILTIN_PROFILES:
            _merge(effective, _BUILTIN_PROFILES[profile])
        else:
            raise ConfigError(f"Profile does not exist: {profile_path}")
    paths = _validate(effective, project_root)
    return AppConfig(
        root=project_root,
        paths=paths,
        ffmpeg_bin=effective["media"]["ffmpeg_bin"],
        ffprobe_bin=effective["media"]["ffprobe_bin"],
        probe_timeout=effective["media"]["probe_timeout"],
        log_level=effective["logging"]["level"],
        data=effective,
    )
