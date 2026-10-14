"""Portable fixture identity must not weaken exact operation preservation."""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil

import pytest


def reference(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns, "bytes": path.stat().st_size}


def test_copied_reference_with_different_prior_mtime_is_accepted_and_unchanged(
        tmp_path, preserved_reference_fixture):
    original = tmp_path / "reference.wav"
    original.write_bytes(b"known permitted reference bytes")
    manifest = reference(original)
    relocated = tmp_path / "copied reference.wav"
    shutil.copyfile(original, relocated)
    os.utime(relocated, ns=(relocated.stat().st_atime_ns, manifest["mtime_ns"]+1_000_000_000))
    actual = reference(relocated)
    assert actual["sha256"] == manifest["sha256"] and actual["mtime_ns"] != manifest["mtime_ns"]
    with preserved_reference_fixture(relocated, manifest) as before:
        assert before == (manifest["sha256"], actual["mtime_ns"])
        assert relocated.read_bytes() == original.read_bytes()
    assert reference(relocated) == actual


@pytest.mark.parametrize("mismatch", ["content", "manifest_hash"])
def test_incorrect_reference_content_or_manifest_hash_fails_before_operation(
        tmp_path, preserved_reference_fixture, mismatch):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"known reference")
    manifest = reference(path)
    if mismatch == "content":
        path.write_bytes(b"other content!")
    else:
        manifest["sha256"] = "0" * 64
    operation_started = False
    with pytest.raises(AssertionError, match="SHA-256.*manifest"):
        with preserved_reference_fixture(path, manifest):
            operation_started = True
    assert operation_started is False


def test_changed_content_is_rejected_even_when_operation_restores_mtime(
        tmp_path, preserved_reference_fixture):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"first content")
    manifest = reference(path)
    with pytest.raises(AssertionError, match="durante la operación"):
        with preserved_reference_fixture(path, manifest):
            path.write_bytes(b"other content")
            os.utime(path, ns=(path.stat().st_atime_ns, manifest["mtime_ns"]))
    assert path.stat().st_mtime_ns == manifest["mtime_ns"]
    assert reference(path)["sha256"] != manifest["sha256"]


@pytest.mark.parametrize("delta_ns", [1, 1_000_000_000])
def test_changed_mtime_is_rejected_without_tolerance_even_when_hash_is_unchanged(
        tmp_path, preserved_reference_fixture, delta_ns):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"identical reference bytes")
    manifest = reference(path)
    with pytest.raises(AssertionError, match="durante la operación"):
        with preserved_reference_fixture(path, manifest):
            os.utime(path, ns=(path.stat().st_atime_ns, manifest["mtime_ns"]+delta_ns))
    assert reference(path)["sha256"] == manifest["sha256"]
    assert path.stat().st_mtime_ns == manifest["mtime_ns"]+delta_ns


def test_failed_operation_keeps_its_error_when_reference_is_preserved(tmp_path, preserved_reference_fixture):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"untouched reference")
    manifest = reference(path)
    with pytest.raises(RuntimeError, match="deliberate engine failure"):
        with preserved_reference_fixture(path, manifest):
            raise RuntimeError("deliberate engine failure")
    assert reference(path) == manifest


def test_failed_operation_still_rejects_modified_reference(tmp_path, preserved_reference_fixture):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"untouched reference")
    manifest = reference(path)
    with pytest.raises(AssertionError, match="durante la operación") as caught:
        with preserved_reference_fixture(path, manifest):
            os.utime(path, ns=(path.stat().st_atime_ns, manifest["mtime_ns"]+1))
            raise RuntimeError("deliberate engine failure")
    assert isinstance(caught.value.__context__, RuntimeError)


@pytest.fixture(scope="module")
def native_benchmark():
    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_whisper_backends.py"
    spec = importlib.util.spec_from_file_location("fixture_validation_native_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_benchmark_accepts_reference_with_relocated_mtime(tmp_path, native_benchmark):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"known reference WAV bytes")
    manifest = reference(path)
    os.utime(path, ns=(path.stat().st_atime_ns, manifest["mtime_ns"]+1))
    baseline = native_benchmark.reference_signature(path, manifest, "local_reference")
    assert baseline["sha256"] == manifest["sha256"] and baseline["bytes"] == manifest["bytes"]
    assert baseline["mtime_ns"] == manifest["mtime_ns"]+1
    assert baseline == native_benchmark.signature(path)


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("bytes", 999)])
def test_native_benchmark_still_rejects_wrong_reference_hash_or_size(
        tmp_path, native_benchmark, field, value):
    path = tmp_path / "fixture.wav"
    path.write_bytes(b"known reference WAV bytes")
    manifest = reference(path)
    manifest[field] = value
    with pytest.raises(ValueError, match=field):
        native_benchmark.reference_signature(path, manifest, "local_reference")
