"""Repositorio SQLite local. No almacena ni modifica el contenido del video."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from clip_factory.database.models import ProbeRecord
from clip_factory.utils.files import safe_destination


class Repository:
    def __init__(self, database: Path, *, root: Path, input_dir: Path) -> None:
        self.database = database
        self.root = root
        self.input_dir = input_dir

    def _path(self) -> Path:
        path = safe_destination(self.database, root=self.root, input_dir=self.input_dir)
        for suffix in ("-journal", "-wal", "-shm"):
            safe_destination(Path(str(path) + suffix), root=self.root, input_dir=self.input_dir)
        return path

    def initialize(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Versión de base de datos no soportada: {version}")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables - {"probes"}:
                raise ValueError("La base de datos contiene tablas ajenas a Clip Factory")
            if "probes" in tables:
                columns = [(row[1], row[2].upper(), row[3], row[5]) for row in connection.execute("PRAGMA table_info(probes)")]
                expected = [("id", "TEXT", 0, 1), ("source_path", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0), ("metadata_json", "TEXT", 1, 0)]
                if columns != expected:
                    raise ValueError("El esquema de la tabla probes es incompatible con Clip Factory")
            connection.execute("""CREATE TABLE IF NOT EXISTS probes (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )""")
            connection.execute("PRAGMA user_version = 1")

    def add_probe(self, source: Path, metadata: dict[str, Any]) -> ProbeRecord:
        self.initialize()
        record = ProbeRecord(str(uuid4()), str(source.resolve()), datetime.now(timezone.utc).isoformat(), metadata)
        payload = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self._path(), timeout=10)) as connection, connection:
            connection.execute("INSERT INTO probes VALUES (?, ?, ?, ?)", (
                record.id, record.source_path, record.created_at, payload,
            ))
        return record

    def list_probes(self, limit: int = 100) -> list[ProbeRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit debe ser un entero positivo")
        with closing(sqlite3.connect(self._path(), timeout=10)) as connection:
            rows = connection.execute(
                "SELECT id, source_path, created_at, metadata_json FROM probes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ProbeRecord(row[0], row[1], row[2], json.loads(row[3])) for row in rows]
