"""Diagnóstico sin instalación automática; ejecutable desde el checkout."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main(["--root", str(Path(__file__).resolve().parents[1]), "doctor", *sys.argv[1:]]))
