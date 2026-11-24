"""Puerto de acceso al iPhone: DELIBERADAMENTE de solo lectura.

No existe ningún método para escribir, borrar, renombrar ni mover en el dispositivo.
Cualquier adaptador (AFC real, simulador) debe cumplir esta interfaz y nada más.
"""
from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from iphonephotosync.domain.models import DeviceFile, DeviceInfo


class DeviceError(Exception):
    """Base de errores de acceso al dispositivo."""


class DeviceNotFound(DeviceError):
    pass


class TrustRequired(DeviceError):
    """El usuario debe pulsar 'Confiar' en el iPhone."""


class DeviceLocked(DeviceError):
    """El iPhone está bloqueado: pedir al usuario que lo desbloquee."""


class DeviceDisconnected(DeviceError):
    """Se perdió la conexión durante una operación. La operación puede reintentarse."""


class DeviceReadError(DeviceError):
    """Un archivo concreto no pudo leerse (el resto puede seguir)."""


@runtime_checkable
class ReadHandle(Protocol):
    size: int

    def read_at(self, offset: int, length: int) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> "ReadHandle": ...

    def __exit__(self, *exc) -> None: ...


@runtime_checkable
class DeviceSource(Protocol):
    def info(self) -> DeviceInfo: ...

    def list_files(self) -> Iterator[DeviceFile]:
        """Todos los archivos bajo /DCIM, orden determinista."""

    def open_read(self, path: str) -> ReadHandle: ...

    def listing_errors(self) -> list[tuple[str, str]]:
        """Entradas que no pudieron leerse en el último ``list_files`` (ruta, motivo)."""

    def source_sha1(self, path: str) -> bytes | None:
        """SHA-1 calculado por el propio dispositivo, o None si no está disponible."""


@runtime_checkable
class ThumbnailSource(Protocol):
    """Capacidad opcional (solo lectura): la miniatura JPEG que el propio iPhone guarda para un elemento de
    ``/DCIM`` (en ``/PhotoData/Thumbnails/V2/DCIM``). ``None`` si no existe."""

    def read_thumbnail(self, path: str) -> bytes | None: ...


@runtime_checkable
class PhotosDatabaseSource(Protocol):
    """Capacidad opcional (solo lectura): copiar la base de datos de Fotos del iPhone (álbumes, favoritos) a una
    carpeta LOCAL de trabajo. Solo se pueden leer los tres archivos de ``PHOTOS_DB_FILES``."""

    def copy_photos_database(self, dest_dir) -> "object | None": ...


PHOTOS_DB_DIR = "/PhotoData"
PHOTOS_DB_FILES = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")      # juntos: el -wal trae lo reciente

THUMBNAIL_ROOT = "/PhotoData/Thumbnails/V2/DCIM"
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


def thumbnail_dir_for(path: str) -> str:
    """Ruta (en el iPhone) de la carpeta de miniaturas de ``/DCIM/<carpeta>/<archivo>``. Valida el formato:
    la única zona fuera de /DCIM que se puede leer es esta, y solo con esta forma."""
    import posixpath
    norm = posixpath.normpath(path)
    parts = norm.split("/")
    if len(parts) != 4 or parts[:2] != ["", "DCIM"] or ".." in parts or not parts[2] or not parts[3]:
        raise DeviceReadError(f"Ruta no válida para miniatura: {path}")
    return f"{THUMBNAIL_ROOT}/{parts[2]}/{parts[3]}"


# Nombres que NUNCA deben aparecer en un adaptador de dispositivo (comprobado en tests).
FORBIDDEN_DEVICE_METHODS = frozenset(
    {"write", "write_at", "delete", "remove", "rm", "rename", "move", "mkdir", "makedirs",
     "truncate", "push", "set_file_time", "link", "open_write"}
)

CHUNK_SIZE = 4 * 1024 * 1024


def iter_chunks(handle: ReadHandle, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Lee el archivo completo por bloques; falla si hay lectura corta."""
    offset = 0
    while offset < handle.size:
        chunk = handle.read_at(offset, min(chunk_size, handle.size - offset))
        if not chunk:
            raise DeviceReadError(f"Lectura corta en offset {offset} de {handle.size}")
        offset += len(chunk)
        yield chunk
