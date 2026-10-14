"""Limpiar sólo temporales propios e inactivos; vista previa por defecto."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.config.loader import load_config
from clip_factory.utils.files import FileSafetyError, secure_directory, temporary_lock, workspace_lock
from clip_factory.utils.logging import close_logging, configure_logging, operation

_MANAGED_TEMP = re.compile(r"\.metadata-[0-9a-f]{32}\.tmp")


def _visit(config, directory, apply, logger, files, skipped):
    with secure_directory(directory, root=config.root, input_dir=config.paths["input"]) as parent:
        with os.scandir(parent.fd) as entries:
            names = sorted((entry.name, entry.is_dir(follow_symlinks=False), entry.is_symlink()) for entry in entries)
        for name, is_directory, is_link in names:
            path = directory / name
            if is_link:
                raise FileSafetyError(f"No se limpian symlinks: {path}")
            if is_directory:
                _visit(config, path, apply, logger, files, skipped)
                continue
            if not name.endswith(".tmp"):
                continue
            if not _MANAGED_TEMP.fullmatch(name):
                skipped.append({"file": str(path), "reason": "unmanaged"})
                continue
            try:
                descriptor = parent.open(name, os.O_RDONLY)
            except FileNotFoundError:
                continue  # El productor ya retiró su temporal.
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    skipped.append({"file": str(path), "reason": "active"})
                    continue
                expected = os.fstat(descriptor)
                if apply:
                    with operation(logger, "clean_temporary", path):
                        parent.unlink(name, expected=expected)
                        os.fsync(parent.fd)
                files.append(str(path))
            finally:
                os.close(descriptor)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--apply", action="store_true", help="Eliminar sólo temporales propios e inactivos")
    args = parser.parse_args(argv)
    logger = None
    try:
        config = load_config(args.root, args.config, args.profile)
        files, skipped = [], []
        if args.apply:
            logger = configure_logging(config.root, config.paths["input"], config.paths["logs"], config.log_level)
        lock = workspace_lock(config.root, config.paths["input"], config.paths["working"]) if args.apply else nullcontext()
        with lock, (temporary_lock(config.root, config.paths["input"]) if args.apply else nullcontext()):
            if config.paths["working"].exists():
                _visit(config, config.paths["working"], args.apply, logger, files, skipped)
        print(json.dumps({"mode": "apply" if args.apply else "preview", "files": files, "skipped": skipped}, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if logger is not None:
            try:
                close_logging(logger)
            except OSError as error:
                print(f"Error al cerrar el log: {error}", file=sys.stderr)
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
