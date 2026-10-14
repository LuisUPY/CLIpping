"""Revisión adversarial de la frontera entre creación y lock del temporal."""

import importlib.util
import os
from pathlib import Path

import pytest

from clip_factory.utils import files
from clip_factory.utils.files import FileSafetyError, SecureDirectory, open_safe_file, write_json_new


spec = importlib.util.spec_from_file_location("clean_security_review", Path(__file__).parents[2] / "scripts/clean.py")
clean = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean)


def test_clean_during_temporary_creation_preserves_active_writer(tmp_path, monkeypatch):
    """clean puede entrar antes de que el productor llegue a flock."""
    working = tmp_path / "data/working"
    working.mkdir(parents=True)
    original = tmp_path / "data/input/video.mp4"
    original.parent.mkdir()
    original.write_bytes(b"original")
    destination = working / "result.json"
    real_open = SecureDirectory.open
    cleaner_results = []

    def clean_after_create(parent, name, flags, mode=0o600, **kwargs):
        descriptor = real_open(parent, name, flags, mode, **kwargs)
        if name.startswith(".metadata-") and name.endswith(".tmp") and flags & os.O_CREAT:
            cleaner_results.append(clean.main(["--root", str(tmp_path), "--apply"]))
        return descriptor

    monkeypatch.setattr(SecureDirectory, "open", clean_after_create)
    write_json_new(destination, {"duration": 1}, root=tmp_path, input_dir=original.parent, originals=[original])
    assert cleaner_results == [1]
    assert destination.read_text().strip() == '{\n  "duration": 1\n}'
    assert original.read_bytes() == b"original"


def test_json_parent_swap_at_publication_cannot_touch_input(tmp_path, monkeypatch):
    working = tmp_path / "data/working"
    working.mkdir(parents=True)
    original = tmp_path / "data/input/result.json"
    original.parent.mkdir()
    original.write_bytes(b"original")
    destination = working / original.name
    real_link = files.os.link

    def swap_parent_then_link(source, target, **kwargs):
        if str(source).startswith(".metadata-"):
            working.rename(tmp_path / "detached-working")
            working.symlink_to(original.parent, target_is_directory=True)
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(files.os, "link", swap_parent_then_link)
    with pytest.raises(FileSafetyError, match="directorio cambió"):
        write_json_new(destination, {"duration": 1}, root=tmp_path, input_dir=original.parent)
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_leaf_swap_before_open_cannot_append_to_input(tmp_path, monkeypatch, link_type):
    logs = tmp_path / "logs"
    logs.mkdir()
    destination = logs / "events.jsonl"
    destination.write_bytes(b"previous log")
    original = tmp_path / "data/input/video.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    real_open = files.os.open
    swapped = False

    def swap_leaf_then_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if name == destination.name and dir_fd is not None and not swapped:
            destination.unlink()
            if link_type == "symbolic":
                destination.symlink_to(original)
            else:
                os.link(original, destination)
            swapped = True
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(files.os, "open", swap_leaf_then_open)
    with pytest.raises((OSError, FileSafetyError)):
        descriptor = open_safe_file(destination, root=tmp_path, input_dir=original.parent,
                                    flags=os.O_WRONLY | os.O_APPEND)
        try:
            os.write(descriptor, b"should never reach original")
        finally:
            os.close(descriptor)
    assert swapped
    assert original.read_bytes() == b"original"
