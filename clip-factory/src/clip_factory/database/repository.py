"""Repositorio SQLite local; consultas sin creación ni cambios de esquema.

SQLite abre sus propios archivos por nombre. Los descriptores, comprobaciones de
identidad y locks POSIX de este módulo evitan enlaces y cambios detectables; no
son una frontera contra un proceso hostil del mismo usuario que cambie rutas
entre dos instrucciones de SQLite. Cerrar esa ventana requiere un VFS específico,
fuera del alcance local de Fase 1.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

from clip_factory.database.models import ProbeRecord
from clip_factory.utils.files import FileSafetyError, safe_destination, secure_directory


class Repository:
    def __init__(self, database: Path, *, root: Path, input_dir: Path) -> None:
        self.database = database
        self.root = root
        self.input_dir = input_dir

    def _path(self, *, originals: tuple[Path, ...] = ()) -> Path:
        # Conservar el nombre lexical: resolve() ocultaría un symlink final antes
        # de que open(..., O_NOFOLLOW) pueda rechazarlo.
        path = Path(os.path.abspath(self.database.expanduser()))
        for suffix in ("", "-journal", "-wal", "-shm", ".lock"):
            safe_destination(
                Path(str(path) + suffix), root=self.root,
                input_dir=self.input_dir, originals=originals,
            )
        return path

    @staticmethod
    def _validate_id(record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
            raise ValueError("El id de probe debe ser un texto de 1 a 128 caracteres")

    @staticmethod
    def _lock(descriptor: int, *, read_only: bool) -> None:
        operation = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Tiempo agotado esperando el repositorio SQLite")
                time.sleep(0.05)

    @contextmanager
    def _connection(self, *, read_only: bool = False, create: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only and create:
            raise ValueError("Una consulta no puede crear el repositorio")
        path = self._path()
        try:
            with secure_directory(
                path.parent, root=self.root, input_dir=self.input_dir, create=create,
            ) as directory:
                if not create and directory.stat(path.name) is None:
                    raise FileNotFoundError(path)
                # macOS puede usar flock en su VFS SQLite: bloquear el propio
                # DB impediría incluso leer PRAGMA. El lock cooperativo usa un
                # inode separado, y las consultas nunca crean ese archivo.
                lock_name = path.name + ".lock"
                lock_descriptor = None
                if not read_only or directory.stat(lock_name) is not None:
                    lock_flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
                    lock_descriptor = directory.open(lock_name, lock_flags)
                flags = os.O_RDONLY if read_only else os.O_RDWR
                descriptor = None
                try:
                    if lock_descriptor is not None:
                        self._lock(lock_descriptor, read_only=read_only)
                    if create and directory.stat(path.name) is None:
                        try:
                            descriptor = directory.open(path.name, flags | os.O_CREAT | os.O_EXCL)
                        except FileExistsError:
                            # Otro inicializador puede haber creado la base después
                            # de stat; la apertura segura vuelve a comprobarla.
                            descriptor = directory.open(path.name, flags)
                    else:
                        descriptor = directory.open(path.name, flags)
                    identity = os.fstat(descriptor)
                    lock_identity = None if lock_descriptor is None else os.fstat(lock_descriptor)

                    def validate_paths() -> None:
                        directory.check_identity()
                        current = directory.stat(path.name)
                        if current is None or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                            raise FileSafetyError("La identidad de la base SQLite cambió durante la operación")
                        if lock_identity is not None:
                            current_lock = directory.stat(lock_name)
                            if current_lock is None or (current_lock.st_dev, current_lock.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
                                raise FileSafetyError("La identidad del lock SQLite cambió durante la operación")
                        for suffix in ("-journal", "-wal", "-shm"):
                            sidecar = path.name + suffix
                            if directory.stat(sidecar) is not None:
                                sidecar_fd = directory.open(sidecar, flags)
                                os.close(sidecar_fd)

                    validate_paths()
                    uri = path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
                    connection = sqlite3.connect(uri, uri=True, timeout=10)
                    try:
                        validate_paths()
                        if read_only:
                            connection.execute("PRAGMA query_only = ON")
                        yield connection
                        validate_paths()
                        if not read_only:
                            connection.commit()
                            validate_paths()
                    except BaseException as primary:
                        if not read_only:
                            try:
                                connection.rollback()
                            except Exception as rollback_error:
                                raise primary from rollback_error
                        raise
                    finally:
                        connection.close()
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if lock_descriptor is not None:
                        os.close(lock_descriptor)
        except FileNotFoundError as error:
            if not create:
                raise FileNotFoundError(f"La base de datos no está inicializada: {path}") from error
            raise

    @staticmethod
    def _check_schema(connection: sqlite3.Connection, *, allow_empty: bool = False) -> None:
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
        if not allow_empty and (version != 1 or "probes" not in tables):
            raise ValueError("La base de datos no tiene un esquema Clip Factory inicializado")

    def initialize(self) -> None:
        with self._connection(create=True) as connection:
            self._check_schema(connection, allow_empty=True)
            connection.execute("""CREATE TABLE IF NOT EXISTS probes (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )""")
            connection.execute("PRAGMA user_version = 1")

    def add_probe(self, source: Path, metadata: dict[str, Any], *, record_id: str | None = None) -> ProbeRecord:
        selected_id = str(uuid4()) if record_id is None else record_id
        self._validate_id(selected_id)
        self._path(originals=(source,))
        # Validar el payload antes de inicializar o abrir una transacción.
        payload = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        record = ProbeRecord(selected_id, str(source.resolve()), datetime.now(timezone.utc).isoformat(), metadata)
        self.initialize()
        with self._connection() as connection:
            self._check_schema(connection)
            connection.execute("INSERT INTO probes VALUES (?, ?, ?, ?)", (
                record.id, record.source_path, record.created_at, payload,
            ))
        return record

    @staticmethod
    def _record(row: tuple[Any, ...]) -> ProbeRecord:
        return ProbeRecord(row[0], row[1], row[2], json.loads(row[3]))

    def get_probe(self, record_id: str) -> ProbeRecord | None:
        self._validate_id(record_id)
        with self._connection(read_only=True) as connection:
            self._check_schema(connection)
            row = connection.execute(
                "SELECT id, source_path, created_at, metadata_json FROM probes WHERE id = ?", (record_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def delete_probe(self, record_id: str) -> None:
        """Compensa únicamente el registro identificado, sin crear una base."""
        self._validate_id(record_id)
        with self._connection() as connection:
            self._check_schema(connection)
            connection.execute("DELETE FROM probes WHERE id = ?", (record_id,))

    def list_probes(self, limit: int = 100) -> list[ProbeRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit debe ser un entero positivo")
        with self._connection(read_only=True) as connection:
            self._check_schema(connection)
            rows = connection.execute(
                "SELECT id, source_path, created_at, metadata_json FROM probes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._record(row) for row in rows]
