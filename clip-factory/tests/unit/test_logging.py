import json

import pytest

from clip_factory.utils.logging import close_logging, configure_logging, operation


def test_logs_success_and_error_with_duration_and_file(tmp_path):
    logger = configure_logging(tmp_path, tmp_path / "data/input", tmp_path / "logs")
    source = tmp_path / "data/input/video.mp4"
    with operation(logger, "probe", source):
        pass
    with pytest.raises(RuntimeError, match="visible"):
        with operation(logger, "failed_probe", source):
            raise RuntimeError("visible")
    close_logging(logger)
    records = [json.loads(line) for line in (tmp_path / "logs/clip_factory.jsonl").read_text().splitlines()]
    assert [record["result"] for record in records] == ["PASS", "FAIL"]
    assert records[1]["error"] == "RuntimeError: visible"
    assert all(record["duration_seconds"] >= 0 for record in records)
    assert all(record["file"] == str(source) and record["date"].endswith("+00:00") for record in records)
