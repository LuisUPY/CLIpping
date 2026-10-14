import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location("clean_script", Path(__file__).parents[2] / "scripts/clean.py")
clean = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean)


def test_clean_requires_apply_and_preserves_non_temporary_files(tmp_path):
    working = tmp_path / "data/working"
    working.mkdir(parents=True)
    temporary = working / (".metadata-" + "a" * 32 + ".tmp")
    temporary.write_text("temporary")
    unmanaged = working / "generated.tmp"
    unmanaged.write_text("not owned by CLIpping")
    report = working / "report.json"
    report.write_text("{}")
    original = tmp_path / "data/input/video.tmp"
    original.parent.mkdir()
    original.write_text("original")
    assert clean.main(["--root", str(tmp_path)]) == 0
    assert temporary.exists()
    assert clean.main(["--root", str(tmp_path), "--apply"]) == 0
    assert not temporary.exists()
    assert unmanaged.read_text() == "not owned by CLIpping"
    assert report.exists() and original.read_text() == "original"


def test_clean_rejects_links_to_input(tmp_path):
    working = tmp_path / "data/working"
    working.mkdir(parents=True)
    original = tmp_path / "data/input/video.tmp"
    original.parent.mkdir()
    original.write_text("original")
    (working / "unsafe.tmp").symlink_to(original)
    assert clean.main(["--root", str(tmp_path), "--apply"]) == 1
    assert original.read_text() == "original"
