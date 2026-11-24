"""Destino del respaldo: estructura de carpetas, identificación estable y manifiesto.

    <base>/iPhone_Photos/{<año>/..., _Manifest/, _Logs/, _Quarantine/, _Staging/}

El destino se identifica por un GUID guardado en ``_Manifest/backup_id.json`` (y en el manifiesto SQLite),
junto con la etiqueta y el número de serie del volumen. Si el HDD cambia de letra se localiza por GUID;
si el GUID no coincide con el manifiesto, NO se escribe nada.
"""
from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

LIBRARY_DIR = "iPhone_Photos"
RESERVED_DIRS = ("_Manifest", "_Logs", "_Quarantine", "_Staging")


class DestinationMismatch(Exception):
    """El destino pertenece a otro respaldo (o el marcador no coincide con el manifiesto)."""


def volume_info(path: Path | str) -> tuple[int | None, str | None]:
    """(número de serie, etiqueta) del volumen en Windows; (None, None) si no se puede determinar."""
    if os.name != "nt":
        return None, None
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None, None
    label = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(drive + "\\"), label, 261, ctypes.byref(serial), None, None, None, 0)
    return (serial.value, label.value) if ok else (None, None)


@dataclass
class Destination:
    base: Path

    @property
    def library(self) -> Path:
        return self.base / LIBRARY_DIR

    @property
    def manifest_dir(self) -> Path:
        return self.library / "_Manifest"

    @property
    def manifest_path(self) -> Path:
        return self.manifest_dir / "manifest.sqlite"

    @property
    def marker_path(self) -> Path:
        return self.manifest_dir / "backup_id.json"

    @property
    def logs_dir(self) -> Path:
        return self.library / "_Logs"

    @property
    def quarantine_dir(self) -> Path:
        return self.library / "_Quarantine"

    @property
    def staging_dir(self) -> Path:
        return self.library / "_Staging"

    def prepare(self) -> None:
        for d in (self.library, *(self.library / n for n in RESERVED_DIRS)):
            d.mkdir(parents=True, exist_ok=True)

    def read_marker(self) -> dict | None:
        try:
            return json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def bind(self, manifest_backup_id: str) -> list[str]:
        """Vincula marcador y manifiesto. Devuelve avisos (p. ej. volumen distinto). Lanza
        ``DestinationMismatch`` si el marcador pertenece a otro respaldo."""
        warnings: list[str] = []
        serial, label = volume_info(self.base)
        marker = self.read_marker()
        if marker is None:
            self.marker_path.write_text(json.dumps({
                "backup_id": manifest_backup_id, "schema": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "volume_serial": serial, "volume_label": label}, indent=2), encoding="utf-8")
            return warnings
        if marker.get("backup_id") != manifest_backup_id:
            raise DestinationMismatch("El marcador del destino no coincide con el manifiesto: "
                                      "no se escribirá nada hasta que el usuario lo confirme.")
        if serial is not None and marker.get("volume_serial") not in (None, serial):
            warnings.append("El número de serie del volumen cambió (¿otro disco o reformateo?). Confirma el destino.")
        if label is not None and marker.get("volume_label") not in (None, label):
            warnings.append(f"La etiqueta del volumen cambió ('{marker.get('volume_label')}' -> '{label}').")
        return warnings


def windows_drive_roots() -> list[Path]:
    """Raíces candidatas para reencontrar el disco (nombre histórico; en Linux/macOS son los puntos de montaje)."""
    from iphonephotosync.transfer.platform_fs import candidate_roots
    return candidate_roots()


def find_backup_by_id(backup_id: str, roots: Iterable[Path] | None = None) -> Path | None:
    """Localiza el destino por GUID aunque el disco haya cambiado de letra. Devuelve la carpeta ``base``."""
    for root in (roots if roots is not None else windows_drive_roots()):
        for base in (root, *([p for p in root.iterdir() if p.is_dir()] if root.exists() else [])):
            marker = base / LIBRARY_DIR / "_Manifest" / "backup_id.json"
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("backup_id") == backup_id:
                    return base
            except (OSError, json.JSONDecodeError, PermissionError):
                continue
    return None
