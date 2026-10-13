"""Configuration layering, portable defaults, and immutable-source boundaries."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys

import pytest

from clip_factory.config import ConfigError, load_config


def write_yaml(root: Path, text: str, name: str = "config/default.yaml") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_new_root_has_safe_defaults_without_creating_files(tmp_path):
    root = tmp_path / "not-created"
    config = load_config(root=root)
    assert config.root == root
    assert config.paths["input"] == root / "data/input"
    assert config.paths["database"] == root / "database/clip_factory.db"
    assert config.ffprobe_bin == "ffprobe"
    assert config.probe_timeout == 30.0
    assert not root.exists()
    assert json.loads(json.dumps(config.to_dict()))["paths"]["clips"] == "data/clips"
    with pytest.raises(FrozenInstanceError):
        config.root = tmp_path
    with pytest.raises(TypeError):
        config.paths["input"] = tmp_path


def test_layering_explicit_config_and_profile(tmp_path):
    write_yaml(tmp_path, "media:\n  probe_timeout: 11\nlogging:\n  level: debug\n")
    write_yaml(tmp_path, "media:\n  probe_timeout: 12\n  ffprobe_bin: custom-probe\n", "override.yaml")
    write_yaml(tmp_path, "media:\n  probe_timeout: 13\nruntime:\n  threads: 2\n", "config/profiles/small.yaml")
    config = load_config(tmp_path, "override.yaml", "small")
    assert config.probe_timeout == 13.0
    assert config.ffprobe_bin == "custom-probe"
    assert config.log_level == "DEBUG"
    assert config.data["runtime"]["threads"] == 2
    assert config.data["editing"]["width"] == 1080
    detached = config.to_dict()
    detached["paths"]["input"] = "changed"
    assert config.data["paths"]["input"] == "data/input"


def test_explicit_root_beats_environment_and_does_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIP_FACTORY_ROOT", str(tmp_path / "environment"))
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    assert load_config(root=tmp_path / "explicit").root == tmp_path / "explicit"
    assert load_config().root == tmp_path / "environment"


def test_discovers_project_in_cwd_ancestors(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIP_FACTORY_ROOT", raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "clip-factory"\n', encoding="utf-8")
    nested = tmp_path / "some/deep/directory"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert load_config().root == tmp_path


def test_builtin_mac_profile_and_missing_profile(tmp_path):
    config = load_config(tmp_path, profile="mac_m1")
    assert config.data["runtime"] == {"device": "cpu", "threads": 4}
    with pytest.raises(ConfigError, match="Profile does not exist"):
        load_config(tmp_path, profile="unknown")
    with pytest.raises(ConfigError, match="simple name"):
        load_config(tmp_path, profile="../../outside")


@pytest.mark.parametrize("text, message", [
    ("unknown: 1\n", "Unknown configuration key"),
    ("media:\n  unknown: 1\n", "Unknown configuration key"),
    ("media: []\n", "must be a mapping"),
    ("media:\n  probe_timeout: .nan\n", "finite number"),
    ("media:\n  probe_timeout: .inf\n", "finite number"),
    ("media:\n  probe_timeout: true\n", "finite number"),
    ("media:\n  probe_timeout: 0\n", "greater than zero"),
    ("media:\n  ffprobe_bin: ''\n", "nonempty string"),
    ("logging:\n  level: NOISE\n", "logging.level"),
    ("runtime:\n  threads: true\n", "positive integer"),
    ("runtime:\n  device: cuda\n", "runtime.device"),
    ("editing:\n  width: 1079\n", "positive even integer"),
    ("editing:\n  height: true\n", "positive even integer"),
    ("editing:\n  mode: stretch\n", "pad or crop"),
    ("editing:\n  video_codec: vp9\n", "h264 and aac"),
    ("scoring:\n  weights:\n    audio: -1\n", "cannot be negative"),
    ("scoring:\n  weights:\n    audio: 1.0e+308\n    hook: 1.0e+308\n", "sum must be finite"),
    ("paths:\n  working: ../outside\n", "cannot contain"),
    ("paths:\n  working: /tmp/clip-factory\n", "must be relative"),
    ("paths:\n  working: 'C:\\outside'\n", "must be relative"),
    ("paths:\n  working: data/input/work\n", "immutable input"),
    ("paths:\n  working: data\n", "immutable input"),
    ("paths:\n  working: data/clips\n", "Writable paths overlap"),
    ("paths:\n  database: data/exports/clip_factory.db\n", "Writable paths overlap"),
    ("- invalid\n- mapping\n", "must be a YAML mapping"),
    ("media: [\n", "Cannot read YAML configuration"),
    ('media: !!python/object/apply:os.system ["echo unsafe"]\n', "Cannot read YAML configuration"),
])
def test_rejects_invalid_configuration(tmp_path, text, message):
    write_yaml(tmp_path, text)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


def test_all_zero_weights_rejected(tmp_path):
    keys = ("hook", "emotion", "surprise", "activity", "audio", "context", "duration", "completeness")
    write_yaml(tmp_path, "scoring:\n  weights:\n" + "".join(f"    {key}: 0\n" for key in keys))
    with pytest.raises(ConfigError, match="at least one positive"):
        load_config(tmp_path)


def test_symlink_escape_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    write_yaml(project, "paths:\n  working: escape/work\n")
    with pytest.raises(ConfigError, match="escapes the project root"):
        load_config(project)


def test_symlink_into_input_rejected(tmp_path):
    originals = tmp_path / "data/input"
    originals.mkdir(parents=True)
    source = originals / "original.txt"
    source.write_text("unchanged", encoding="utf-8")
    (tmp_path / "shortcut").symlink_to(originals, target_is_directory=True)
    write_yaml(tmp_path, "paths:\n  working: shortcut\n")
    with pytest.raises(ConfigError, match="immutable input"):
        load_config(tmp_path)
    assert source.read_text(encoding="utf-8") == "unchanged"


def test_standard_input_protected_when_input_is_reconfigured(tmp_path):
    write_yaml(tmp_path, "paths:\n  input: data/other-originals\n  exports: data/input/output\n")
    with pytest.raises(ConfigError, match="immutable input"):
        load_config(tmp_path)


def test_existing_file_cannot_be_used_as_directory(tmp_path):
    (tmp_path / "output-file").write_text("preserved", encoding="utf-8")
    write_yaml(tmp_path, "paths:\n  clips: output-file\n")
    with pytest.raises(ConfigError, match="must point to a directory"):
        load_config(tmp_path)


def test_missing_explicit_config_is_not_silently_ignored(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read YAML configuration"):
        load_config(tmp_path, config_path="missing.yaml")


def test_missing_yaml_dependency_is_actionable(tmp_path, monkeypatch):
    write_yaml(tmp_path, "{}\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(ConfigError, match="PyYAML is required"):
        load_config(tmp_path)
