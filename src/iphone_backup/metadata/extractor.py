"""Extractor de metadatos definitivos sobre la copia local (staging)."""
from __future__ import annotations

from pathlib import Path

from iphone_backup.domain.filetypes import FileKind
from iphone_backup.domain.models import DeviceFile
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.metadata.policy import MediaMetadata, interpret


def exiftool_extractor(et: ExifToolService, use_mtime_fallback: bool = False):
    def extract(path: Path, f: DeviceFile, kind: FileKind) -> MediaMetadata:
        return interpret(et.read(path), kind, mtime_ns=f.mtime_ns, use_mtime_fallback=use_mtime_fallback)

    return extract
