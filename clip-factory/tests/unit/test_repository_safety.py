"""Regresiones de lectura, compensación y rutas SQLite de Fase 1."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3

import pytest

from clip_factory.database.repository import Repository
from clip_factory.utils.files import FileSafetyError


def repository(root):
    return Repository(root / "database/clip_factory.db", root=root, input_dir=root / "data/input")


@pytest.mark.parametrize("operation", ["list", "get", "delete"])
def test_missing_repository_does_not_create_database_or_directory(tmp_path, operation):
    repo = repository(tmp_path)
    with pytest.raises(FileNotFoundError, match="no está inicializada"):
        if operation == "list":
            repo.list_probes()
        elif operation == "get":
            repo.get_probe("missing")
        else:
            repo.delete_probe("missing")
    assert list(tmp_path.iterdir()) == []


def test_existing_directory_missing_database_remains_empty(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    with pytest.raises(FileNotFoundError):
        repo.list_probes()
    assert list(repo.database.parent.iterdir()) == []


def test_queries_connect_with_explicit_read_only_uri(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    repo.initialize()
    connect = sqlite3.connect
    calls = []

    def traced_connect(database, **kwargs):
        calls.append((database, kwargs))
        return connect(database, **kwargs)

    monkeypatch.setattr("clip_factory.database.repository.sqlite3.connect", traced_connect)
    assert repo.list_probes() == []
    assert repo.get_probe("missing") is None
    assert len(calls) == 2
    assert all(database.endswith("?mode=ro") and kwargs["uri"] for database, kwargs in calls)
    assert {path.name for path in repo.database.parent.iterdir()} == {repo.database.name, repo.database.name + ".lock"}


def test_existing_empty_database_query_does_not_initialize(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    repo.database.touch()
    with pytest.raises(ValueError, match="no tiene un esquema"):
        repo.list_probes()
    assert repo.database.read_bytes() == b""


def test_query_legacy_database_does_not_create_lock(tmp_path):
    repo = repository(tmp_path)
    repo.initialize()
    Path(str(repo.database) + ".lock").unlink()
    assert repo.list_probes() == []
    assert {path.name for path in repo.database.parent.iterdir()} == {repo.database.name}


def test_explicit_record_id_allows_safe_compensation(tmp_path):
    repo = repository(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    first = repo.add_probe(source, {"duration": 1}, record_id="first")
    second = repo.add_probe(source, {"duration": 2}, record_id="second")
    assert repo.get_probe(first.id) == first
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_probe(source, {"duration": 99}, record_id=first.id)
    assert repo.get_probe(first.id) == first
    repo.delete_probe(first.id)
    repo.delete_probe("'; DELETE FROM probes; --")
    assert repo.list_probes() == [second]
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("record_id", ["", None, False, 1, "x" * 129])
def test_invalid_lookup_ids_do_not_create_files(tmp_path, record_id):
    repo = repository(tmp_path)
    with pytest.raises(ValueError):
        repo.get_probe(record_id)
    assert list(tmp_path.iterdir()) == []


def test_invalid_metadata_does_not_create_database(tmp_path):
    repo = repository(tmp_path)
    with pytest.raises(ValueError):
        repo.add_probe(tmp_path / "source.mp4", {"duration": float("nan")})
    assert list(tmp_path.iterdir()) == []


def test_database_cannot_be_the_source_original(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    repo.database.write_bytes(b"original")
    with pytest.raises(FileSafetyError):
        repo.add_probe(repo.database, {})
    assert repo.database.read_bytes() == b"original"


def test_database_final_symlink_rejected_even_inside_project(tmp_path):
    repo = repository(tmp_path)
    repo.database.parent.mkdir()
    original = tmp_path / "original.sqlite"
    original.write_bytes(b"original")
    repo.database.symlink_to(original)
    with pytest.raises(FileSafetyError):
        repo.initialize()
    assert original.read_bytes() == b"original"


def test_database_ancestor_symlink_rejected_even_inside_project(tmp_path):
    repo = repository(tmp_path)
    original_dir = tmp_path / "originals"
    original_dir.mkdir()
    original = original_dir / repo.database.name
    original.write_bytes(b"original")
    repo.database.parent.symlink_to(original_dir, target_is_directory=True)
    with pytest.raises(FileSafetyError):
        repo.initialize()
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm", ".lock"])
@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_existing_sidecar_links_never_modify_original(tmp_path, suffix, link_type):
    repo = repository(tmp_path)
    repo.initialize()
    original = tmp_path / "original.mp4"
    original.write_bytes(b"original")
    sidecar = Path(str(repo.database) + suffix)
    if sidecar.exists():
        sidecar.unlink()
    if link_type == "symbolic":
        sidecar.symlink_to(original)
    else:
        os.link(original, sidecar)
    with pytest.raises(FileSafetyError):
        repo.initialize()
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("operation", ["initialize", "read"])
def test_parent_swap_during_connect_detected_before_queries(tmp_path, monkeypatch, operation):
    repo = repository(tmp_path)
    repo.initialize()
    input_dir = tmp_path / "data/input"
    input_dir.mkdir(parents=True)
    protected = input_dir / repo.database.name
    protected.write_bytes(repo.database.read_bytes())
    before = protected.read_bytes()
    connect = sqlite3.connect

    def swap_then_connect(database, **kwargs):
        repo.database.parent.rename(tmp_path / "database-moved")
        repo.database.parent.symlink_to(input_dir, target_is_directory=True)
        return connect(database, **kwargs)

    monkeypatch.setattr("clip_factory.database.repository.sqlite3.connect", swap_then_connect)
    with pytest.raises(FileSafetyError):
        if operation == "initialize":
            repo.initialize()
        else:
            repo.list_probes()
    assert protected.read_bytes() == before
    assert {path.name for path in input_dir.iterdir()} == {protected.name}


def test_database_swap_during_connect_detected_before_schema_changes(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    repo.initialize()
    protected = tmp_path / "original.sqlite"
    protected.write_bytes(repo.database.read_bytes())
    before = protected.read_bytes()
    connect = sqlite3.connect

    def swap_then_connect(database, **kwargs):
        repo.database.rename(repo.database.with_name("old.sqlite"))
        repo.database.symlink_to(protected)
        return connect(database, **kwargs)

    monkeypatch.setattr("clip_factory.database.repository.sqlite3.connect", swap_then_connect)
    with pytest.raises(FileSafetyError):
        repo.initialize()
    assert protected.read_bytes() == before


def test_read_only_connection_cannot_modify_rows(tmp_path):
    repo = repository(tmp_path)
    repo.initialize()
    before = repo.database.read_bytes()
    with repo._connection(read_only=True) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO probes VALUES ('id', 'source', 'time', '{}')")
    assert repo.database.read_bytes() == before


def test_database_uri_handles_reserved_and_unicode_name(tmp_path):
    repo = Repository(tmp_path / "database/clip #? español.db", root=tmp_path, input_dir=tmp_path / "data/input")
    source = tmp_path / "video.mp4"
    record = repo.add_probe(source, {"duration": 1})
    assert repo.list_probes() == [record]
    assert repo.get_probe(record.id) == record


def test_concurrent_writers_preserve_rows_without_sqlite_lock_conflict(tmp_path):
    repo = repository(tmp_path)
    repo.initialize()
    source = tmp_path / "video.mp4"
    source.write_bytes(b"original")

    def write_record(index):
        return repo.add_probe(source, {"duration": index}, record_id=f"record-{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(write_record, range(8)))
    assert {record.id for record in repo.list_probes()} == {record.id for record in records}
    assert source.read_bytes() == b"original"
