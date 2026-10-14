"""Independent regression cases for interrupted local publication."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from clip_factory.cli.main import _payload_hash, main
from clip_factory.database.repository import Repository
from clip_factory.utils.files import write_json_new


@pytest.mark.parametrize("crash_stage", ["journal", "destination"])
def test_recovery_after_abrupt_exit_between_link_and_temporary_cleanup(
    tmp_path, crash_stage, capsys
):
    """Real process death must not make the supported recovery command unusable."""
    source = tmp_path / "data/input/original.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable source")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_mtime = source.stat().st_mtime_ns
    script = r'''
import os
from pathlib import Path
import sys
from clip_factory.cli.main import _publish_probe
from clip_factory.config.loader import load_config
from clip_factory.database.repository import Repository
from clip_factory.utils import files

root = Path(sys.argv[1])
stage = sys.argv[2]
config = load_config(root)
source = root / "data/input/original.mp4"
repository = Repository(config.paths["database"], root=root,
                        input_dir=config.paths["input"])
real_link = files.os.link
def link_then_exit(src, dst, *args, **kwargs):
    result = real_link(src, dst, *args, **kwargs)
    if (stage == "journal" and dst != "result.json") or (
        stage == "destination" and dst == "result.json"
    ):
        os._exit(73)
    return result
files.os.link = link_then_exit
_publish_probe(repository, source, {"duration": 1, "video": str(source)},
               root / "data/working/result.json", config)
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), crash_stage],
        cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=10,
        stdin=subprocess.DEVNULL, shell=False, check=False,
    )
    assert result.returncode == 73, result.stderr
    journals = list((tmp_path / "data/working/.publications").glob("*.json"))
    assert len(journals) == 1
    interrupted = journals[0] if crash_stage == "journal" else tmp_path / "data/working/result.json"
    assert interrupted.stat().st_nlink == 2
    recovery_result = main(["--root", str(tmp_path), "recover"])
    captured = capsys.readouterr()
    assert recovery_result == 0, captured.err
    assert (tmp_path / "data/working/result.json").exists()
    assert not list((tmp_path / "data/working/.publications").glob("*.json"))
    repository = Repository(tmp_path / "database/clip_factory.db", root=tmp_path,
                            input_dir=tmp_path / "data/input")
    assert len(repository.list_probes()) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert source.stat().st_mtime_ns == original_mtime


def test_recovery_rejects_existing_json_with_different_boolean_type(tmp_path, capsys):
    """JSON true and 1 must not become equivalent when checking conflicts."""
    source = tmp_path / "data/input/original.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable source")
    payload = {"duration": 1, "video": str(source)}
    record_id = uuid4().hex
    journal = tmp_path / "data/working/.publications" / f"{record_id}.json"
    destination = tmp_path / "data/working/result.json"
    write_json_new(journal, {
        "version": 1, "record_id": record_id, "source": str(source),
        "destination": "data/working/result.json", "metadata": payload,
        "sha256": _payload_hash(payload),
    }, root=tmp_path, input_dir=source.parent)
    conflicting = {"duration": True, "video": str(source)}
    destination.write_text(json.dumps(conflicting), encoding="utf-8")
    before = destination.read_bytes()
    result = main(["--root", str(tmp_path), "recover"])
    captured = capsys.readouterr()
    assert result == 1, captured.out
    assert "Conflicto" in captured.err
    assert destination.read_bytes() == before
    assert journal.exists()
    assert not (tmp_path / "database/clip_factory.db").exists()
    assert source.read_bytes() == b"immutable source"
