"""Verificación independiente del respaldo ya copiado (solo lectura).

Para cada elemento COMMITTED: el archivo existe, su tamaño coincide, su SHA-256 recalculado desde el disco
coincide con el manifiesto y (opcional) el SHA-1 que el iPhone calcula HOY coincide con el registrado al copiar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from iphone_backup.database.manifest import Manifest
from iphone_backup.organizer.naming import UnsafePath, safe_join
from iphone_backup.ports.device import DeviceError, DeviceSource
from iphone_backup.transfer.destination import Destination
from iphone_backup.transfer.engine import _sha256_file
from iphone_backup.transfer.fsops import RealFs


@dataclass
class VerifyReport:
    checked: int = 0
    ok: int = 0
    missing: list[str] = field(default_factory=list)
    size_mismatch: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)
    unsafe_path: list[str] = field(default_factory=list)
    device_checked: int = 0
    device_changed: list[str] = field(default_factory=list)   # el iPhone ya no tiene ese contenido (informativo)
    device_unavailable: int = 0

    @property
    def clean(self) -> bool:
        return not (self.missing or self.size_mismatch or self.hash_mismatch or self.unsafe_path)


def verify_library(dest: Destination, manifest: Manifest, source: DeviceSource | None = None,
                   progress: Callable[[int, int], None] | None = None, fs=None) -> VerifyReport:
    fs = fs or RealFs()
    rep = VerifyReport()
    rows = manifest._conn.execute(
        "SELECT original_name, rel_path, size, sha256, sha1_source, device_path FROM items "
        "WHERE copy_status='committed' ORDER BY id").fetchall()
    for i, r in enumerate(rows, 1):
        rep.checked += 1
        label = r["rel_path"]
        try:
            target = safe_join(dest.library, r["rel_path"].split("/"))
        except UnsafePath:
            rep.unsafe_path.append(label)
            continue
        if not fs.exists(target):
            rep.missing.append(label)
            continue
        if fs.getsize(target) != r["size"]:
            rep.size_mismatch.append(label)
            continue
        digest, _ = _sha256_file(fs, target, 4 * 1024 * 1024)
        if digest != r["sha256"]:
            rep.hash_mismatch.append(label)
            continue
        rep.ok += 1
        if source is not None and r["sha1_source"]:
            try:
                dev = source.source_sha1(r["device_path"])
            except DeviceError:
                rep.device_unavailable += 1
            else:
                if dev is None:
                    rep.device_unavailable += 1
                else:
                    rep.device_checked += 1
                    if dev.hex() != r["sha1_source"]:
                        rep.device_changed.append(label)
        if progress and (i % 10 == 0 or i == len(rows)):
            progress(i, len(rows))
    return rep
