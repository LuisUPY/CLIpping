"""Reject incompatible databases and normalize invalid-source path errors."""

import sqlite3

import pytest

from clip_factory.cli.main import main
from clip_factory.database.repository import Repository


def test_initialize_rejects_incompatible_probes_schema(tmp_path):
    database = tmp_path / "database/clip_factory.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probes (unrelated TEXT)")
    repository = Repository(database, root=tmp_path, input_dir=tmp_path / "data/input")
    with pytest.raises(ValueError):
        repository.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_cli_symlink_loop_fails_cleanly(tmp_path, capsys):
    source = tmp_path / "loop.mp4"
    source.symlink_to(source)
    assert main(["--root", str(tmp_path), "probe", str(source)]) == 1
    assert "Error:" in capsys.readouterr().err
