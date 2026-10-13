"""Escrituras nuevas y confinadas; nunca reemplazar originales."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable


class FileSafetyError(ValueError):
    """El destino podría modificar datos protegidos."""


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_destination(
    destination: Path, *, root: Path, input_dir: Path,
    originals: Iterable[Path] = (),
) -> Path:
    """Valida también enlaces simbólicos y hardlinks de archivos existentes."""
    target = destination.expanduser().resolve()
    if not is_within(target, root.resolve()):
        raise FileSafetyError(f"Destino fuera del proyecto: {destination}")
    for protected in (input_dir.resolve(), (root / "data/input").resolve()):
        if is_within(target, protected):
            raise FileSafetyError(f"data/input es inmutable: {destination}")
    for original in originals:
        source = original.resolve()
        if target == source or (
            target.exists() and source.exists() and target.samefile(source)
        ):
            raise FileSafetyError(f"El destino coincide con un original: {destination}")
    if target.exists() and target.is_file() and target.stat().st_nlink > 1:
        raise FileSafetyError(f"No se escriben archivos con hardlinks: {destination}")
    return target


def write_json_new(
    destination: Path, payload: Any, *, root: Path, input_dir: Path,
    originals: Iterable[Path] = (),
) -> Path:
    """Publica JSON completo con creación exclusiva, sin reemplazar otro archivo."""
    target = safe_destination(
        destination, root=root, input_dir=input_dir, originals=originals,
    )
    if target.suffix.lower() != ".json":
        raise FileSafetyError("El archivo de metadata debe terminar en .json")
    if target.exists():
        raise FileExistsError(f"El destino ya existe: {target}")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        # link() crea un nombre nuevo de manera atómica y falla si ya existe.
        os.link(temporary_path, target)
    finally:
        temporary_path.unlink()
    return target
