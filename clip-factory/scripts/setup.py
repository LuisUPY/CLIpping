"""Crear carpetas/SQLite. No instalar software ni descargar modelos."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip_factory.cli.main import main

if __name__ == "__main__":
    # Las opciones globales proporcionadas por el usuario preceden a init.
    raise SystemExit(main(["--root", str(Path(__file__).resolve().parents[1]), *sys.argv[1:], "init"]))
