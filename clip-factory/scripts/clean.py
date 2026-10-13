"""Previsualizar o eliminar exclusivamente archivos *.tmp dentro de working."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.config.loader import load_config
from clip_factory.utils.files import FileSafetyError, is_within, safe_destination
from clip_factory.utils.logging import close_logging, configure_logging, operation


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--apply", action="store_true", help="Autorizar la eliminación de temporales mostrados")
    args = parser.parse_args(argv)
    logger = None
    try:
        config = load_config(args.root, args.config, args.profile)
        working = config.paths["working"]
        candidates = []
        if working.exists():
            for item in working.rglob("*.tmp"):
                if item.is_symlink() or any(parent.is_symlink() for parent in item.parents if is_within(parent, working)):
                    raise FileSafetyError(f"No se limpian symlinks: {item}")
                if item.is_file():
                    candidates.append(safe_destination(item, root=config.root, input_dir=config.paths["input"]))
        candidates.sort()
        if args.apply:
            logger = configure_logging(config.root, config.paths["input"], config.paths["logs"], config.log_level)
            for candidate in candidates:
                with operation(logger, "clean_temporary", candidate):
                    candidate.unlink()
        print(json.dumps({"mode": "apply" if args.apply else "preview", "files": [str(p) for p in candidates]}, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if logger is not None:
            close_logging(logger)


if __name__ == "__main__":
    raise SystemExit(main())
