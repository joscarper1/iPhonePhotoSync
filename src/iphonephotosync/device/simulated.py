"""iPhone simulado a partir de una carpeta con estructura ``DCIM/100APPLE/...``.

Permite desarrollar y probar sin dispositivo, con inyección de fallos
(desconexión a mitad de copia, bloqueo, archivo ilegible). Solo lee; nunca escribe en ``root``.
"""
from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from iphonephotosync.domain.models import DeviceFile, DeviceInfo
from iphonephotosync.ports.device import DeviceDisconnected, DeviceLocked, DeviceReadError


@dataclass
class FaultPlan:
    disconnect_after_bytes: int | None = None     # se desconecta al superar este total leído
    locked: bool = False
    unreadable_paths: set[str] = field(default_factory=set)
    corrupt_paths: set[str] = field(default_factory=set)    # lecturas con un byte alterado (fallo de transporte)
    corrupt_times: int = 1                                   # cuántas lecturas se corrompen (luego se normaliza)


class _SimHandle:
    def __init__(self, dev: "SimulatedDeviceSource", fs_path: Path, size: int, corrupt: bool = False):
        self._dev = dev
        self._f = open(fs_path, "rb")
        self.size = size
        self._corrupt = corrupt

    def read_at(self, offset: int, length: int) -> bytes:
        self._dev._check_alive()
        self._f.seek(offset)
        data = self._f.read(length)
        self._dev._account(len(data))
        if self._corrupt and offset == 0 and data:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return data

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "_SimHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SimulatedDeviceSource:
    def __init__(self, root: Path, udid: str = "SIMULATED-0001", faults: FaultPlan | None = None):
        self._root = Path(root)
        self._udid = udid
        self.faults = faults or FaultPlan()
        self._bytes_read = 0
        self._disconnected = False

    # --- control de fallos (solo para pruebas) ---
    def reconnect(self) -> None:
        self._disconnected = False
        self.faults.disconnect_after_bytes = None

    def unlock(self) -> None:
        self.faults.locked = False

    def _check_alive(self) -> None:
        if self.faults.locked:
            raise DeviceLocked("El iPhone está bloqueado")
        if self._disconnected:
            raise DeviceDisconnected("Dispositivo desconectado")

    def _account(self, n: int) -> None:
        self._bytes_read += n
        limit = self.faults.disconnect_after_bytes
        if limit is not None and self._bytes_read > limit:
            self._disconnected = True
            raise DeviceDisconnected("Desconexión simulada durante la lectura")

    def _resolve(self, path: str) -> Path:
        norm = posixpath.normpath(path)
        if not norm.startswith("/DCIM/") or ".." in norm.split("/"):
            raise DeviceReadError(f"Ruta no permitida: {path}")
        fs = self._root / norm.lstrip("/")
        if not fs.is_file():
            raise DeviceReadError(f"No existe: {path}")
        return fs

    # --- interfaz DeviceSource (solo lectura) ---
    def info(self) -> DeviceInfo:
        self._check_alive()
        return DeviceInfo(udid=self._udid, product_type="Simulated", ios_version="0")

    def list_files(self) -> Iterator[DeviceFile]:
        self._check_alive()
        dcim = self._root / "DCIM"
        if not dcim.is_dir():
            return
        for p in sorted(dcim.rglob("*")):
            if not p.is_file():
                continue
            st = p.stat()
            rel = "/" + p.relative_to(self._root).as_posix()
            yield DeviceFile(path=rel, size=st.st_size, mtime_ns=st.st_mtime_ns,
                             birthtime_ns=getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns)

    def listing_errors(self) -> list[tuple[str, str]]:
        return []

    def open_read(self, path: str) -> _SimHandle:
        self._check_alive()
        if path in self.faults.unreadable_paths:
            raise DeviceReadError(f"Archivo ilegible (simulado): {path}")
        fs = self._resolve(path)
        corrupt = path in self.faults.corrupt_paths and self.faults.corrupt_times > 0
        if corrupt:
            self.faults.corrupt_times -= 1
        return _SimHandle(self, fs, fs.stat().st_size, corrupt)

    def copy_photos_database(self, dest_dir):
        """Base de Fotos simulada: ``<root>/PhotoData/Photos.sqlite`` (+wal/shm) copiada a una carpeta local."""
        import shutil
        from iphonephotosync.ports.device import PHOTOS_DB_FILES
        self._check_alive()
        src = self._root / "PhotoData" / PHOTOS_DB_FILES[0]
        if not src.is_file():
            return None
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for name in PHOTOS_DB_FILES:
            s = self._root / "PhotoData" / name
            if s.is_file():
                shutil.copyfile(s, dest / name)
        return dest / PHOTOS_DB_FILES[0]

    def read_thumbnail(self, path: str) -> bytes | None:
        """Miniatura nativa simulada: ``<root>/PhotoData/Thumbnails/V2/DCIM/<carpeta>/<archivo>/<cualquiera>``."""
        from iphonephotosync.ports.device import thumbnail_dir_for
        self._check_alive()
        d = self._root / thumbnail_dir_for(path).lstrip("/")
        if not d.is_dir():
            return None
        files = sorted(p for p in d.iterdir() if p.is_file())
        return files[0].read_bytes() if files else None

    @property
    def bytes_read(self) -> int:
        """Total de bytes entregados por el dispositivo simulado (para comprobar que no se relee lo ya respaldado)."""
        return self._bytes_read

    def source_sha1(self, path: str) -> bytes | None:
        self._check_alive()
        fs = self._resolve(path)
        h = hashlib.sha1()
        with open(fs, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.digest()
