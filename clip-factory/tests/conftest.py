"""Shared checks for portable reference media and strict operation preservation."""

from contextlib import contextmanager
import hashlib

import pytest


def _signature(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), (
        f"El fixture cambió mientras se calculaba su SHA-256: {path}"
    )
    return digest.hexdigest(), after.st_mtime_ns


@pytest.fixture
def preserved_reference_fixture():
    @contextmanager
    def preserve(path, reference):
        # The manifest hash identifies the reference content. Its historical
        # mtime is provenance: copying valid bytes may produce a different mtime.
        before = _signature(path)
        assert before[0] == reference["sha256"], f"SHA-256 del fixture difiere del manifest: {path}"
        try:
            yield before
        finally:
            # No tolerance or historical timestamp: compare this operation's
            # exact local baseline, including when the operation raises.
            assert _signature(path) == before, (
                f"El fixture cambió durante la operación (SHA-256 o mtime_ns): {path}"
            )

    return preserve
