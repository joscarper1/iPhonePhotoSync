"""Instantánea de escaneo por iPhone (identificado por su UDID): permite no repetir el recorrido lento de la
biblioteca del destino. Es estado propio del respaldo; solo se guarda en ``_Manifest/scans`` del destino.

* ``<udid>.json``          : cabecera (cuándo, qué destino, cuántos archivos). Pequeña: se lee siempre.
* ``<udid>.library.json``  : listado de la biblioteca existente (ruta, tamaño, fecha) del último recorrido completo.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from iphonephotosync.library.external_index import ExtFile

VERSION = 1


def _safe(udid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", udid) or "dispositivo"


@dataclass
class SnapshotInfo:
    udid: str
    model: str | None
    dest: str
    saved_at: datetime
    device_files: int
    library_files: int

    def age_hours(self, now: datetime | None = None) -> float:
        return max(0.0, ((now or datetime.now(timezone.utc)) - self.saved_at).total_seconds() / 3600)


class SnapshotStore:
    def __init__(self, manifest_dir: Path):
        self.dir = Path(manifest_dir) / "scans"

    def _meta(self, udid: str) -> Path:
        return self.dir / f"{_safe(udid)}.json"

    def _lib(self, udid: str) -> Path:
        return self.dir / f"{_safe(udid)}.library.json"

    def info(self, udid: str, dest: str | None = None) -> SnapshotInfo | None:
        """None si no hay instantánea válida (inexistente, dañada, de otra versión o de otro destino)."""
        try:
            d = json.loads(self._meta(udid).read_text(encoding="utf-8"))
            if d.get("version") != VERSION or d.get("udid") != udid:
                return None
            if dest is not None and _norm(d.get("dest", "")) != _norm(dest):
                return None
            return SnapshotInfo(udid, d.get("model"), d["dest"], datetime.fromisoformat(d["saved_at"]),
                                int(d["device_files"]), int(d["library_files"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_albums(self, udid: str, index) -> None:
        """Álbumes y favoritos de este iPhone (para el escaneo rápido; no hace falta copiar la base de Fotos)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic(self.dir / f"{_safe(udid)}.albums.json", index.to_json())

    def load_albums(self, udid: str):
        from iphonephotosync.app.albums import AlbumIndex
        try:
            return AlbumIndex.from_json((self.dir / f"{_safe(udid)}.albums.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def load_listing(self, udid: str) -> list[ExtFile] | None:
        try:
            raw = json.loads(self._lib(udid).read_text(encoding="utf-8"))
            return [ExtFile(rel, int(size), int(mt)) for rel, size, mt in raw]
        except (OSError, ValueError, TypeError):
            return None

    def save(self, udid: str, model: str | None, dest: str, device_files: int, listing: list[ExtFile],
             now: datetime | None = None) -> SnapshotInfo:
        self.dir.mkdir(parents=True, exist_ok=True)
        saved = now or datetime.now(timezone.utc)
        _atomic(self._lib(udid), json.dumps([[f.rel, f.size, f.mtime_ns] for f in listing], separators=(",", ":")))
        meta = {"version": VERSION, "udid": udid, "model": model, "dest": dest, "saved_at": saved.isoformat(),
                "device_files": device_files, "library_files": len(listing)}
        _atomic(self._meta(udid), json.dumps(meta, indent=2, ensure_ascii=False))     # la cabecera va al final
        return SnapshotInfo(udid, model, dest, saved, device_files, len(listing))


def _norm(p: str) -> str:
    import os
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
