"""Fallos parciales y recuperación: nunca borrar ni sobrescribir un output ajeno."""

import json
from pathlib import Path
import sqlite3

import pytest

from clip_factory.cli import main as cli
from clip_factory.database.repository import Repository
from clip_factory.media.probe import MediaInfo


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path / "data/input/source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable source")
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: MediaInfo(
        str(source), 1.5, 320, 180, 25.0, "25/1", True, "mpeg4", ()))
    command = ["--root", str(tmp_path), "probe", str(source), "--output", "data/working/report.json"]
    repo = Repository(tmp_path / "database/clip_factory.db", root=tmp_path, input_dir=source.parent)
    return source, command, repo


def journals(root):
    return list((root / "data/working/.publications").glob("*.json"))


def test_incompatible_database_never_publishes_json(context, tmp_path):
    source, command, repo = context
    repo.database.parent.mkdir()
    with sqlite3.connect(repo.database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    assert cli.main(command) == 1
    assert not (tmp_path / "data/working/report.json").exists()
    assert not journals(tmp_path)
    assert source.read_bytes() == b"immutable source"


def test_insert_failure_never_publishes_and_can_retry(context, tmp_path, monkeypatch):
    _, command, repo = context
    original = Repository.add_probe
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("insert failed")
    monkeypatch.setattr(Repository, "add_probe", fail)
    assert cli.main(command) == 1
    assert not (tmp_path / "data/working/report.json").exists()
    assert repo.list_probes() == []
    assert not journals(tmp_path)
    monkeypatch.setattr(Repository, "add_probe", original)
    assert cli.main(command) == 0


def test_failed_publication_compensates_only_own_record(context, tmp_path, monkeypatch):
    source, command, repo = context
    existing = repo.add_probe(source, {"previous": True})
    real_write = cli.write_json_new
    def fail_output(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise OSError("publication failed")
        return real_write(destination, *args, **kwargs)
    monkeypatch.setattr(cli, "write_json_new", fail_output)
    assert cli.main(command) == 1
    assert not (tmp_path / "data/working/report.json").exists()
    assert repo.list_probes() == [existing]
    assert not journals(tmp_path)


def test_failed_compensation_keeps_journal_and_recovers(context, tmp_path, monkeypatch):
    _, command, repo = context
    real_write, real_delete = cli.write_json_new, Repository.delete_probe
    def fail_output(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise OSError("publication failed")
        return real_write(destination, *args, **kwargs)
    def fail_delete(*args, **kwargs):
        raise sqlite3.OperationalError("compensation failed")
    monkeypatch.setattr(cli, "write_json_new", fail_output)
    monkeypatch.setattr(Repository, "delete_probe", fail_delete)
    assert cli.main(command) == 1
    assert len(journals(tmp_path)) == 1
    assert len(repo.list_probes()) == 1
    monkeypatch.setattr(cli, "write_json_new", real_write)
    monkeypatch.setattr(Repository, "delete_probe", real_delete)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    assert len(repo.list_probes()) == 1
    assert not journals(tmp_path)
    assert json.loads((tmp_path / "data/working/report.json").read_text()) == repo.list_probes()[0].metadata
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0


def test_crash_before_json_can_recover(context, tmp_path, monkeypatch):
    _, command, repo = context
    real_write = cli.write_json_new
    def interrupt(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise SystemExit(73)
        return real_write(destination, *args, **kwargs)
    monkeypatch.setattr(cli, "write_json_new", interrupt)
    with pytest.raises(SystemExit):
        cli.main(command)
    assert len(repo.list_probes()) == 1
    assert not (tmp_path / "data/working/report.json").exists()
    monkeypatch.setattr(cli, "write_json_new", real_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 0
    assert not journals(tmp_path)
    assert (tmp_path / "data/working/report.json").exists()


def test_recovery_conflict_never_overwrites_foreign_output(context, tmp_path, monkeypatch):
    _, command, repo = context
    real_write = cli.write_json_new
    def interrupt(destination, *args, **kwargs):
        if destination.name == "report.json":
            raise SystemExit(73)
        return real_write(destination, *args, **kwargs)
    monkeypatch.setattr(cli, "write_json_new", interrupt)
    with pytest.raises(SystemExit):
        cli.main(command)
    destination = tmp_path / "data/working/report.json"
    destination.write_text("foreign output")
    before = destination.read_bytes()
    monkeypatch.setattr(cli, "write_json_new", real_write)
    assert cli.main(["--root", str(tmp_path), "recover"]) == 1
    assert destination.read_bytes() == before
    assert len(journals(tmp_path)) == 1
    assert len(repo.list_probes()) == 1


def test_output_collision_rolls_back_without_deleting_foreign_file(context, tmp_path, monkeypatch):
    _, command, repo = context
    real_write = cli.write_json_new
    def collide(destination, *args, **kwargs):
        if destination.name == "report.json":
            destination.write_text("foreign output")
        return real_write(destination, *args, **kwargs)
    monkeypatch.setattr(cli, "write_json_new", collide)
    assert cli.main(command) == 1
    assert (tmp_path / "data/working/report.json").read_text() == "foreign output"
    assert repo.list_probes() == []
    assert not journals(tmp_path)


def test_malformed_journal_has_visible_error_and_is_retained(tmp_path):
    folder = tmp_path / "data/working/.publications"
    folder.mkdir(parents=True)
    journal = folder / ("a" * 32 + ".json")
    journal.write_text('{"version": 1}')
    assert cli.main(["--root", str(tmp_path), "recover"]) == 1
    assert journal.exists()
    assert not (tmp_path / "database/clip_factory.db").exists()


@pytest.mark.parametrize("stem", ["v" * 220, "🦜" * 55])
def test_default_report_name_has_fixed_safe_length(tmp_path, monkeypatch, stem):
    source = tmp_path / "data/input" / f"{stem}.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    monkeypatch.setattr(cli, "probe_media", lambda *args, **kwargs: MediaInfo(
        str(source), 1, 320, 180, 25, "25/1", True, "mpeg4", ()))
    assert cli.main(["--root", str(tmp_path), "probe", str(source)]) == 0
    reports = list((tmp_path / "data/working").glob("*.probe.json"))
    assert len(reports) == 1 and len(reports[0].name.encode("utf-8")) < 100
    assert source.read_bytes() == b"original"
