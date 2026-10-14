"""Escrituras exclusivas y aperturas ancladas para el entorno local POSIX."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable, Iterator
from uuid import uuid4


class FileSafetyError(ValueError):
    """El destino podría modificar datos protegidos."""


class WorkspaceBusyError(FileSafetyError):
    """Otro proceso está publicando, recuperando o limpiando resultados."""


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_destination(destination: Path, *, root: Path, input_dir: Path,
                     originals: Iterable[Path] = (), allow_hardlinks: bool = False) -> Path:
    target = destination.expanduser().resolve()
    if not is_within(target, root.resolve()):
        raise FileSafetyError(f"Destino fuera del proyecto: {destination}")
    for protected in (input_dir.resolve(), (root / "data/input").resolve()):
        if is_within(target, protected):
            raise FileSafetyError(f"data/input es inmutable: {destination}")
    for original in originals:
        source = original.resolve()
        if target == source or (target.exists() and source.exists() and target.samefile(source)):
            raise FileSafetyError(f"El destino coincide con un original: {destination}")
    if not allow_hardlinks and target.exists() and target.is_file() and target.stat().st_nlink > 1:
        raise FileSafetyError(f"No se escriben archivos con hardlinks: {destination}")
    return target


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _leaf(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise FileSafetyError("Se esperaba un nombre de archivo simple")


@dataclass
class SecureDirectory:
    """Directorio abierto; las operaciones se resuelven respecto a su descriptor."""

    fd: int
    path: Path

    def check_identity(self) -> None:
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            raise FileSafetyError(f"El directorio cambió durante la operación: {self.path}") from error
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(self.fd)):
            raise FileSafetyError(f"El directorio cambió durante la operación: {self.path}")

    def stat(self, name: str, *, allow_links: bool = False) -> os.stat_result | None:
        _leaf(name)
        self.check_identity()
        try:
            value = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode) or (not allow_links and value.st_nlink != 1):
            raise FileSafetyError(f"Archivo inseguro (enlace o tipo no regular): {self.path / name}")
        return value

    def open(self, name: str, flags: int, mode: int = 0o600, *, allow_links: bool = False) -> int:
        _leaf(name)
        if flags & os.O_TRUNC:
            raise FileSafetyError("No se permite truncar archivos existentes")
        if allow_links and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise FileSafetyError("Los hardlinks sólo pueden inspeccionarse en lectura")
        previous = self.stat(name, allow_links=allow_links)
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        try:
            self.check_identity()
            opened = os.fstat(descriptor)
            current = self.stat(name, allow_links=allow_links)
            if (not stat.S_ISREG(opened.st_mode) or (not allow_links and opened.st_nlink != 1)
                    or current is None or _identity(current) != _identity(opened)
                    or (previous is not None and _identity(previous) != _identity(opened))):
                raise FileSafetyError(f"El archivo cambió durante la apertura: {self.path / name}")
            if previous is None and flags & os.O_CREAT:
                os.fsync(self.fd)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def unlink(self, name: str, *, expected: os.stat_result | None = None) -> None:
        current = self.stat(name, allow_links=expected is not None)
        if current is None:
            raise FileNotFoundError(self.path / name)
        if expected is not None and _identity(current) != _identity(expected):
            raise FileSafetyError(f"El archivo cambió antes de eliminarlo: {self.path / name}")
        os.unlink(name, dir_fd=self.fd)


@contextmanager
def secure_directory(directory: Path, *, root: Path, input_dir: Path,
                     create: bool = False) -> Iterator[SecureDirectory]:
    """Recorre cada componente sin symlinks; crea sólo con create explícito."""
    candidate = Path(os.path.abspath(directory.expanduser()))
    safe_destination(candidate / ".directory-guard", root=root, input_dir=input_dir)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in candidate.parts[1:]:
            try:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass  # Otro proceso creó el nombre; open(O_NOFOLLOW) lo valida.
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise FileSafetyError(f"Directorio inseguro o enlace simbólico: {candidate}") from error
                raise
            os.close(descriptor)
            descriptor = following
        opened = SecureDirectory(descriptor, candidate)
        opened.check_identity()
        yield opened
    finally:
        os.close(descriptor)


def open_safe_file(destination: Path, *, root: Path, input_dir: Path, flags: int,
                   mode: int = 0o600, originals: Iterable[Path] = (), allow_hardlinks: bool = False) -> int:
    originals = tuple(originals)
    original_identities = {_identity(source.stat()) for source in originals if source.exists()}
    safe_destination(destination, root=root, input_dir=input_dir, originals=originals, allow_hardlinks=allow_hardlinks)
    with secure_directory(destination.parent, root=root, input_dir=input_dir,
                          create=bool(flags & os.O_CREAT)) as parent:
        descriptor = parent.open(destination.name, flags, mode, allow_links=allow_hardlinks)
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) in original_identities:
                raise FileSafetyError(f"El archivo abierto es un original: {destination}")
            for original in originals:
                if original.exists() and _identity(original.stat()) == _identity(opened):
                    raise FileSafetyError(f"El archivo abierto es un original: {destination}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


@contextmanager
def workspace_lock(root: Path, input_dir: Path, working_dir: Path,
                   *, blocking: bool = False) -> Iterator[None]:
    descriptor = open_safe_file(working_dir / ".clip_factory.lock", root=root,
                               input_dir=input_dir, flags=os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise WorkspaceBusyError("Hay otra publicación, recuperación o limpieza activa; reintenta después.") from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def temporary_lock(root: Path, input_dir: Path, *, blocking: bool = False) -> Iterator[None]:
    """Coordina creación de temporales y limpieza incluso al usar módulos separados."""
    with workspace_lock(root, input_dir, root, blocking=blocking):
        yield


def write_json_new(destination: Path, payload: Any, *, root: Path, input_dir: Path,
                   originals: Iterable[Path] = ()) -> Path:
    """Publica JSON sin reemplazo; bloquea su temporal hasta retirarlo."""
    originals = tuple(originals)
    safe_destination(destination, root=root, input_dir=input_dir, originals=originals)
    if destination.suffix.lower() != ".json":
        raise FileSafetyError("El archivo de metadata debe terminar en .json")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with temporary_lock(root, input_dir, blocking=True), secure_directory(
            destination.parent, root=root, input_dir=input_dir, create=True) as parent:
        if parent.stat(destination.name) is not None:
            raise FileExistsError(f"El destino ya existe: {destination}")
        temporary = f".metadata-{uuid4().hex}.tmp"
        descriptor = parent.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        failed = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            parent.check_identity()
            os.link(temporary, destination.name, src_dir_fd=parent.fd,
                    dst_dir_fd=parent.fd, follow_symlinks=False)
            os.fsync(parent.fd)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                parent.unlink(temporary, expected=os.fstat(descriptor))
                os.fsync(parent.fd)
            except FileNotFoundError:
                pass  # No sustituir el error principal si el temporal ya fue retirado.
            except (OSError, FileSafetyError) as error:
                if not failed:
                    raise
                print(f"Error adicional al retirar temporal: {error}", file=sys.stderr)
            finally:
                os.close(descriptor)
    return destination


def read_json_safe(source: Path, *, root: Path, input_dir: Path, allow_hardlinks: bool = False) -> Any:
    descriptor = open_safe_file(source, root=root, input_dir=input_dir, flags=os.O_RDONLY,
                                allow_hardlinks=allow_hardlinks)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return json.load(stream)


def recover_json_links(destination: Path, *, root: Path, input_dir: Path) -> None:
    """Retira sólo aliases temporales propios del mismo inode; usar tras validar journal/contenido."""
    safe_destination(destination, root=root, input_dir=input_dir, allow_hardlinks=True)
    with temporary_lock(root, input_dir), secure_directory(
            destination.parent, root=root, input_dir=input_dir) as parent:
        current = parent.stat(destination.name, allow_links=True)
        if current is None or current.st_nlink == 1:
            return
        matches = []
        for name in os.listdir(parent.fd):
            if re.fullmatch(r"\.metadata-[0-9a-f]{32}\.tmp", name):
                candidate = parent.stat(name, allow_links=True)
                if candidate is not None and _identity(candidate) == _identity(current):
                    matches.append(name)
        if current.st_nlink != len(matches) + 1:
            raise FileSafetyError(f"Hardlinks ajenos al protocolo de publicación: {destination}")
        for name in matches:
            parent.unlink(name, expected=current)
        os.fsync(parent.fd)


def unlink_safe(source: Path, *, root: Path, input_dir: Path) -> None:
    safe_destination(source, root=root, input_dir=input_dir)
    with secure_directory(source.parent, root=root, input_dir=input_dir) as parent:
        expected = parent.stat(source.name)
        if expected is None:
            raise FileNotFoundError(source)
        parent.unlink(source.name, expected=expected)
        os.fsync(parent.fd)
