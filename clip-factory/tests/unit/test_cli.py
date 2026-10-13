import json

import pytest

from clip_factory.cli import main as cli
from clip_factory.database.repository import Repository
from clip_factory.media.ffmpeg import DependencyError
from clip_factory.media.probe import MediaInfo


@pytest.fixture
def fake_video(tmp_path, monkeypatch):
    video = tmp_path / "data/input/video con espacios.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"original immutable")
    info = MediaInfo(str(video), 2.5, 1920, 1080, 29.97002997, "30000/1001", True, "h264", ())
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: info)
    return video


def test_init_and_config(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "init"]) == 0
    assert (tmp_path / "database/clip_factory.db").is_file()
    assert (tmp_path / "data/input").is_dir()
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "--profile", "mac_m1", "config"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime"]["device"] == "cpu"


def test_probe_stdout_and_database_preserve_input(fake_video, tmp_path, capsys):
    before = fake_video.read_bytes()
    assert cli.main(["--root", str(tmp_path), "analyze", str(fake_video), "--output", "-"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["duration"] == 2.5
    assert result["scope"] == "metadata_only"
    assert result["analysis"]["moments"] == "not_run"
    assert fake_video.read_bytes() == before
    repo = Repository(tmp_path / "database/clip_factory.db", root=tmp_path, input_dir=fake_video.parent)
    assert len(repo.list_probes()) == 1


def test_default_and_explicit_outputs_never_replace(fake_video, tmp_path):
    command = ["--root", str(tmp_path), "probe", str(fake_video)]
    assert cli.main(command) == 0
    assert len(list((tmp_path / "data/working").glob("*.probe.json"))) == 1
    assert cli.main([*command, "--output", "data/input/report.json"]) == 1
    assert not (tmp_path / "data/input/report.json").exists()
    assert cli.main([*command, "--output", "data/working/report.json"]) == 0
    original_report = (tmp_path / "data/working/report.json").read_bytes()
    assert cli.main([*command, "--output", "data/working/report.json"]) == 1
    assert (tmp_path / "data/working/report.json").read_bytes() == original_report


def test_missing_dependency_logged_and_nonzero(fake_video, tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise DependencyError("No ffprobe")
    monkeypatch.setattr(cli, "probe_media", fail)
    assert cli.main(["--root", str(tmp_path), "probe", str(fake_video)]) == 1
    assert "No ffprobe" in capsys.readouterr().err
    record = json.loads((tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()[-1])
    assert record["result"] == "FAIL" and "No ffprobe" in record["error"]
    assert not (tmp_path / "database/clip_factory.db").exists()


def test_original_cannot_be_log_file(tmp_path):
    source = tmp_path / "logs/clip_factory.jsonl"
    source.parent.mkdir()
    source.write_bytes(b"video source bytes")
    assert cli.main(["--root", str(tmp_path), "probe", str(source)]) == 1
    assert source.read_bytes() == b"video source bytes"


def test_reserved_commands_fail_explicitly(capsys):
    assert cli.main(["process", "video.mp4"]) == 2
    assert "pendiente" in capsys.readouterr().err


def test_init_without_sqlite_is_an_explicit_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "sqlite3", None)
    assert cli.main(["--root", str(tmp_path), "init"]) == 1
    assert "SQLite no está disponible" in capsys.readouterr().err
    assert not (tmp_path / "database/clip_factory.db").exists()


def test_invalid_metadata_is_saved_but_returns_failure(fake_video, tmp_path, monkeypatch, capsys):
    info = MediaInfo(str(fake_video), None, None, None, None, None, True, None, ())
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: info)
    assert cli.main(["--root", str(tmp_path), "probe", str(fake_video), "--output", "-"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["validation"]["status"] == "FAIL"
    assert "validación" in captured.err
