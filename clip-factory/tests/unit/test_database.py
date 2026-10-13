import sqlite3

import pytest

from clip_factory.database.repository import Repository
from clip_factory.utils.files import FileSafetyError


def repository(root):
    return Repository(root / "database/clip_factory.db", root=root, input_dir=root / "data/input")


def test_initialize_idempotent_and_persist_metadata(tmp_path):
    repo = repository(tmp_path)
    repo.initialize()
    repo.initialize()
    source = tmp_path / "input's video.mp4"
    source.write_bytes(b"original")
    metadata = {"duration": 3.25, "video": "español", "streams": []}
    record = repo.add_probe(source, metadata)
    assert repo.list_probes() == [record]
    assert source.read_bytes() == b"original"
    connection = sqlite3.connect(repo.database)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()


def test_database_inside_input_rejected(tmp_path):
    with pytest.raises(FileSafetyError):
        Repository(tmp_path / "data/input/db", root=tmp_path, input_dir=tmp_path / "data/input").initialize()


def test_unrecognized_schema_not_overwritten(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    connection = sqlite3.connect(repo.database)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.close()
    with pytest.raises(ValueError, match="ajenas"):
        repo.initialize()


def test_symlinked_database_sidecar_rejected(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    original = tmp_path / "data/input/original.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    (repo.database.parent / "clip_factory.db-journal").symlink_to(original)
    with pytest.raises(FileSafetyError):
        repo.initialize()
    assert original.read_bytes() == b"original"
