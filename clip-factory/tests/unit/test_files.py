from pathlib import Path

import pytest

from clip_factory.utils.files import FileSafetyError, safe_destination, write_json_new


def test_new_json_never_replaces_original_or_existing_output(tmp_path):
    original = tmp_path / "data/input/video.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    options = {"root": tmp_path, "input_dir": original.parent, "originals": [original]}
    with pytest.raises(FileSafetyError):
        write_json_new(original, {}, **options)
    target = tmp_path / "data/working/result.json"
    write_json_new(target, {"title": "español"}, **options)
    previous = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_json_new(target, {"replace": True}, **options)
    assert target.read_bytes() == previous
    assert original.read_bytes() == b"original"
    assert not list(target.parent.glob("*.tmp"))


def test_protects_canonical_input_when_input_is_reconfigured(tmp_path):
    with pytest.raises(FileSafetyError):
        safe_destination(tmp_path / "data/input/result.json", root=tmp_path, input_dir=tmp_path / "custom")


def test_rejects_symlink_and_hardlink_to_original(tmp_path):
    source = tmp_path / "data/input/original.json"
    source.parent.mkdir(parents=True)
    source.write_text("original")
    linked = tmp_path / "link.json"
    linked.symlink_to(source)
    with pytest.raises(FileSafetyError):
        safe_destination(linked, root=tmp_path, input_dir=source.parent)
    hardlink = tmp_path / "hard.json"
    hardlink.hardlink_to(source)
    with pytest.raises(FileSafetyError):
        safe_destination(hardlink, root=tmp_path, input_dir=source.parent)
    assert source.read_text() == "original"


def test_rejects_escape_and_invalid_json(tmp_path):
    options = {"root": tmp_path, "input_dir": tmp_path / "data/input"}
    with pytest.raises(FileSafetyError):
        write_json_new(tmp_path.parent / "out.json", {}, **options)
    with pytest.raises(FileSafetyError):
        write_json_new(tmp_path / "out.mp4", {}, **options)
    with pytest.raises(ValueError):
        write_json_new(tmp_path / "out.json", {"score": float("nan")}, **options)
    assert not (tmp_path / "out.json").exists()
