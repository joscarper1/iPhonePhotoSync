"""Clasificación de archivos de /DCIM por extensión (tipos observados en la Etapa 0)."""
from __future__ import annotations

from enum import Enum


class FileKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    SIDECAR = "sidecar"   # .AAE: instrucciones de edición; se conserva junto a su foto
    IGNORED = "ignored"   # p. ej. .bin de diagnóstico de cámara


_TYPES: dict[str, tuple[FileKind, str]] = {
    ".heic": (FileKind.PHOTO, "image/heic"),
    ".heif": (FileKind.PHOTO, "image/heif"),
    ".jpg": (FileKind.PHOTO, "image/jpeg"),
    ".jpeg": (FileKind.PHOTO, "image/jpeg"),
    ".png": (FileKind.PHOTO, "image/png"),
    ".webp": (FileKind.PHOTO, "image/webp"),
    ".mov": (FileKind.VIDEO, "video/quicktime"),
    ".mp4": (FileKind.VIDEO, "video/mp4"),
    ".aae": (FileKind.SIDECAR, "application/xml"),
}


def classify(ext: str) -> tuple[FileKind, str | None]:
    """Devuelve (tipo, mime). Extensiones desconocidas se ignoran, nunca se descartan en silencio:
    el escáner debe contarlas y reportarlas."""
    return _TYPES.get(ext.lower(), (FileKind.IGNORED, None))
