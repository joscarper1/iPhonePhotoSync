"""Nombres seguros para Windows, rutas largas y contención dentro de la carpeta de destino."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

MONTHS_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
             "Septiembre", "Octubre", "Noviembre", "Diciembre"]
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_COMPONENT = 100
LONG_PATH_THRESHOLD = 240


class UnsafePath(Exception):
    pass


def sanitize_component(name: str, max_len: int = MAX_COMPONENT) -> str:
    """Nombre de carpeta/archivo válido en Windows. Conserva acentos (NFC); reemplaza caracteres
    inválidos por '_'; evita nombres reservados, puntos/espacios finales y cadenas vacías."""
    s = unicodedata.normalize("NFC", name)
    s = _INVALID.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    if not s:
        return "_"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s


def sanitize_filename(name: str) -> str:
    """Como sanitize_component pero conserva la extensión al truncar."""
    stem, ext = os.path.splitext(unicodedata.normalize("NFC", name))
    ext = _INVALID.sub("_", ext)[:16]
    return sanitize_component(stem, MAX_COMPONENT - len(ext)) + ext


def month_folder(month: int) -> str:
    return f"{month:02d} - {MONTHS_ES[month - 1]}"


def safe_join(root: Path, parts: tuple[str, ...] | list[str], filename: str | None = None) -> Path:
    """root/parts/filename garantizando que no se sale de ``root`` (anti recorrido de rutas)."""
    root = Path(os.path.abspath(root))
    target = root
    for p in (*parts, *([filename] if filename else [])):
        if p in ("", ".", "..") or "/" in p or "\\" in p or ":" in p:
            raise UnsafePath(f"Componente de ruta no permitido: {p!r}")
        target = target / p
    resolved = Path(os.path.abspath(target))
    if os.path.commonpath([str(root).casefold(), str(resolved).casefold()]) != str(root).casefold():
        raise UnsafePath(f"La ruta sale de la carpeta de destino: {resolved}")
    return resolved


def to_extended(path: Path) -> Path:
    """Prefijo \\\\?\\ para rutas largas en Windows (> ~240 caracteres). No-op en otros casos."""
    s = os.path.abspath(path)
    if os.name == "nt" and len(s) > LONG_PATH_THRESHOLD and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + s[2:])
        return Path("\\\\?\\" + s)
    return Path(s)
