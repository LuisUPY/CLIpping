"""Regression checks for immutable input, including custom configuration."""

import pytest

from clip_factory.utils.files import FileSafetyError, safe_destination


def test_standard_input_stays_immutable_after_input_is_reconfigured(tmp_path):
    protected = tmp_path / "data/input"
    protected.mkdir(parents=True)
    custom_input = tmp_path / "data/custom-originals"
    with pytest.raises(FileSafetyError, match="inmutable"):
        safe_destination(
            protected / "metadata.json", root=tmp_path, input_dir=custom_input,
        )


def test_symlink_to_standard_input_stays_immutable_with_custom_input(tmp_path):
    protected = tmp_path / "data/input"
    protected.mkdir(parents=True)
    alias = tmp_path / "shortcut"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(FileSafetyError, match="inmutable"):
        safe_destination(
            alias / "metadata.json", root=tmp_path, input_dir=tmp_path / "data/custom",
        )
