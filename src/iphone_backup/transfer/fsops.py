"""Operaciones de sistema de archivos del destino, aisladas para poder inyectar fallos en las pruebas
(disco lleno, corrupción al escribir/leer). Todas usan rutas extendidas (\\\\?\\) cuando hace falta."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from iphone_backup.organizer.naming import to_extended


def _posix_rename_noreplace(src: str, dst: str) -> bool:
    """Renombrado atómico que NUNCA sobrescribe en Linux (renameat2) y macOS (renamex_np). Devuelve False si el
    sistema o el sistema de archivos no lo soporta (p. ej. algunos exFAT): entonces se usa la comprobación previa.
    Lanza FileExistsError si el destino existe. Windows no lo necesita (``os.rename`` ya falla si existe)."""
    import ctypes
    import ctypes.util
    import errno
    import sys

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        if sys.platform.startswith("linux"):
            fn = libc.renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            ret = fn(-100, os.fsencode(src), -100, os.fsencode(dst), 1)      # AT_FDCWD, RENAME_NOREPLACE
        elif sys.platform == "darwin":
            fn = libc.renamex_np
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            ret = fn(os.fsencode(src), os.fsencode(dst), 0x4)               # RENAME_EXCL
        else:
            return False
    except (OSError, AttributeError):
        return False
    if ret == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(f"El destino ya existe: {os.path.basename(dst)}")
    if err in {errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL),
               getattr(errno, "EOPNOTSUPP", errno.EINVAL)}:
        return False
    raise OSError(err, os.strerror(err), dst)


class RealFs:
    def open_write(self, path: Path) -> BinaryIO:
        return open(to_extended(path), "wb")

    def open_read(self, path: Path) -> BinaryIO:
        return open(to_extended(path), "rb")

    def fsync(self, f: BinaryIO) -> None:
        f.flush()
        os.fsync(f.fileno())

    def makedirs(self, path: Path) -> None:
        os.makedirs(to_extended(path), exist_ok=True)

    def exists(self, path: Path) -> bool:
        return os.path.lexists(to_extended(path))

    def getsize(self, path: Path) -> int:
        return os.path.getsize(to_extended(path))

    def remove(self, path: Path) -> None:
        try:
            os.remove(to_extended(path))
        except FileNotFoundError:
            pass

    def rename_noreplace(self, src: Path, dst: Path) -> None:
        """Renombra SIN sobrescribir jamás. En Windows ``os.rename`` falla si el destino existe; la
        comprobación previa cubre otros sistemas."""
        if self.exists(dst):
            raise FileExistsError(f"El destino ya existe: {dst.name}")
        if os.name != "nt" and _posix_rename_noreplace(str(src), str(dst)):
            return
        os.rename(to_extended(src), to_extended(dst))

    def set_mtime_ns(self, path: Path, mtime_ns: int) -> None:
        try:
            os.utime(to_extended(path), ns=(mtime_ns, mtime_ns))
        except OSError:
            pass  # conservar la fecha original es un extra; no es motivo para fallar la copia

    def listdir(self, path: Path) -> list[str]:
        try:
            return os.listdir(to_extended(path))
        except FileNotFoundError:
            return []
