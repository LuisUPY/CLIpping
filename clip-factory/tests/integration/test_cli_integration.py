"""Exercise the installed CLI against a synthetic, immutable local original."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from clip_factory.media.ffmpeg import run_media_command


@pytest.mark.integration
def test_analyze_cli_persists_metadata_logs_and_preserves_original(tmp_path):
    ffmpeg = shutil.which(os.environ.get("CLIP_FACTORY_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CLIP_FACTORY_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Integración CLI omitida: requiere FFmpeg y ffprobe locales.")

    root = tmp_path / "proyecto local con espacios"
    source = root / "data/input/original de prueba.mp4"
    source.parent.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "media:\n"
        f"  ffmpeg_bin: {json.dumps(ffmpeg)}\n"
        f"  ffprobe_bin: {json.dumps(ffprobe)}\n",
        encoding="utf-8",
    )
    run_media_command([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
        "-t", "0.6", "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-threads", "1", "-shortest", str(source),
    ], timeout=30)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_mtime = source.stat().st_mtime_ns
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable, "-m", "clip_factory", "--root", str(root),
            "analyze", str(source), "--output", "data/working/result.json",
        ],
        cwd=tmp_path,
        env=environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = root / "data/working/result.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert 0.5 <= payload["duration"] <= 0.8
    assert payload["scope"] == "metadata_only"
    assert payload["analysis"]["moments"] == "not_run"
    assert payload["validation"]["status"] == "PASS"
    assert payload["has_audio"] is True
    assert payload["resolution"] == "320x180"

    with closing(sqlite3.connect(root / "database/clip_factory.db")) as connection:
        rows = connection.execute("SELECT source_path, metadata_json FROM probes").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == str(source.resolve())
    assert json.loads(rows[0][1]) == payload

    records = [
        json.loads(line)
        for line in (root / "logs/clip_factory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    operations = [record for record in records if record["operation"] == "analyze"]
    assert len(operations) == 1
    log = operations[0]
    assert {"date", "file", "operation", "duration_seconds", "result", "error"} <= log.keys()
    assert datetime.fromisoformat(log["date"]).tzinfo == timezone.utc
    assert log["file"] == str(source.resolve())
    assert log["duration_seconds"] >= 0
    assert log["result"] == "PASS"
    assert log["error"] is None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert source.stat().st_mtime_ns == original_mtime
