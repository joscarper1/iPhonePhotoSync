"""Control de espacio en disco del destino (requerimiento §9).

Tres comprobaciones:
  1. ANTES de copiar: espacio libre >= espacio requerido + reserva (``check_space``).
  2. DURANTE la copia: antes de cada archivo, que quepa él más la reserva (``has_room_for``).
  3. Límite del sistema de archivos (FAT32 no admite archivos de 4 GiB o más) (``oversized_for_fs``).

El espacio requerido cuenta solo lo que realmente se copiará (sin lo ya respaldado ni lo ignorado).
El staging está en el mismo volumen y se procesa un archivo a la vez, y el renombrado final no consume
espacio adicional, de modo que no hace falta duplicar el tamaño.
"""
from __future__ import annotations

import ctypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

GIB = 1024 ** 3
MIN_RESERVE = 1 * GIB
RESERVE_FRACTION = 0.01
FAT_MAX_FILE = 4 * GIB - 1
FS_MAX_FILE = {"FAT": FAT_MAX_FILE, "FAT32": FAT_MAX_FILE}


@dataclass(frozen=True)
class SpaceReport:
    required: int
    reserve: int
    free: int
    ok: bool
    shortfall: int          # bytes que faltan (0 si ok)
    checked_path: str

    def message(self) -> str:
        gb = lambda n: f"{n / 1e9:.2f} GB"
        if self.ok:
            return (f"Espacio suficiente: se necesitan {gb(self.required)} (+{gb(self.reserve)} de reserva) "
                    f"y hay {gb(self.free)} libres.")
        return (f"Espacio insuficiente: se necesitan {gb(self.required)} (+{gb(self.reserve)} de reserva), hay "
                f"{gb(self.free)} libres; faltan {gb(self.shortfall)}. Libera espacio o elige otro disco.")


def default_reserve(required: int) -> int:
    return max(MIN_RESERVE, int(required * RESERVE_FRACTION))


def required_bytes(sizes_by_path: dict[str, int], planned_paths: Iterable[str],
                   already_backed_up: Iterable[str] = ()) -> int:
    """Suma de tamaños de lo planificado que aún no está respaldado."""
    skip = set(already_backed_up)
    return sum(sizes_by_path[p] for p in planned_paths if p not in skip)


def _existing_parent(path: Path) -> Path:
    p = Path(os.path.abspath(path))
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


def free_bytes(dest: Path | str) -> tuple[int, str]:
    """Espacio libre del volumen que contiene ``dest`` (aunque la carpeta aún no exista)."""
    p = _existing_parent(Path(dest))
    return shutil.disk_usage(p).free, str(p)


def check_space(dest: Path | str, required: int, reserve: int | None = None) -> SpaceReport:
    reserve = default_reserve(required) if reserve is None else reserve
    free, where = free_bytes(dest)
    need = required + reserve
    return SpaceReport(required, reserve, free, free >= need, max(0, need - free), where)


def has_room_for(dest: Path | str, next_file_size: int, reserve: int = MIN_RESERVE) -> bool:
    """Comprobación por archivo durante la copia."""
    free, _ = free_bytes(dest)
    return free >= next_file_size + reserve


def filesystem_name(path: Path | str) -> str | None:
    """Nombre del sistema de archivos (NTFS, exFAT, FAT32…); None si no se puede determinar."""
    if os.name != "nt":
        from iphone_backup.transfer.platform_fs import posix_filesystem_name
        return posix_filesystem_name(_existing_parent(Path(path)))
    root = os.path.splitdrive(os.path.abspath(_existing_parent(Path(path))))[0]
    if not root:
        return None
    buf = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root + "\\"), None, 0, None, None, None, buf, 64)
    return buf.value.upper() if ok else None


def oversized_for_fs(fs_name: str | None, sizes_by_path: dict[str, int]) -> list[str]:
    """Rutas cuyo tamaño excede el máximo del sistema de archivos (p. ej. > 4 GiB en FAT32)."""
    limit = FS_MAX_FILE.get((fs_name or "").upper())
    if limit is None:
        return []
    return sorted(p for p, s in sizes_by_path.items() if s > limit)
