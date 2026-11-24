"""Sondeo de metadatos directamente desde el dispositivo, sin descargar archivos completos.

Fotos: se lee la cabecera (EXIF está al inicio). Vídeos: se extraen ftyp+moov. El material temporal
se escribe en una carpeta local y se elimina siempre. El dispositivo solo se LEE.
La extracción autoritativa (Etapa 5) se hace sobre la copia completa; este sondeo es para la vista previa.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from iphone_backup.domain.filetypes import FileKind, classify
from iphone_backup.domain.models import DeviceFile
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.metadata.quicktime_probe import extract_ftyp_moov
from iphone_backup.ports.device import DeviceReadError, DeviceSource

HEAD_BYTES = 1024 * 1024


def probe_tags(source: DeviceSource, f: DeviceFile, et: ExifToolService, tmp_dir: Path) -> dict | None:
    """Etiquetas ExifTool del archivo o None si no hay nada que sondear. Lanza DeviceError si el
    dispositivo falla."""
    kind, _ = classify(f.ext)
    if kind not in (FileKind.PHOTO, FileKind.VIDEO):
        return None
    with source.open_read(f.path) as h:
        if kind is FileKind.PHOTO:
            data = h.read_at(0, min(f.size, HEAD_BYTES))
        else:
            data = extract_ftyp_moov(h.read_at, h.size)
            if data is None:
                raise DeviceReadError("No se pudo localizar el átomo moov")
    p = tmp_dir / f"probe{f.ext}"
    p.write_bytes(data)
    try:
        return et.read(p)
    finally:
        p.unlink(missing_ok=True)


def make_tmp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="iphone_backup_probe_")
