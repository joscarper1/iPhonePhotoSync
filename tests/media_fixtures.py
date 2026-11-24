"""Generadores de medios SINTÉTICOS para pruebas (nunca datos reales del usuario).

Se usa ExifTool solo aquí, para ESCRIBIR etiquetas en archivos de prueba creados al vuelo.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from iphone_backup.metadata.exiftool_service import default_exiftool_path

EXE = default_exiftool_path()
needs_exiftool = EXE.is_file()


def _box(t: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), t) + payload


def _mvhd() -> bytes:
    body = struct.pack(">IIIII", 0, 0, 0, 1000, 0) + struct.pack(">IH", 0x10000, 0x100) + b"\0" * 10
    body += struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b"\0" * 24 + struct.pack(">I", 1)
    return _box(b"mvhd", body)


def _write_tags(path: Path, tags: dict[str, str]) -> None:
    args = [str(EXE), "-overwrite_original", "-q"] + [f"-{k}={v}" for k, v in tags.items()] + [str(path)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)


_CACHE = Path(tempfile.mkdtemp(prefix="ib_fixtures_"))        # una carpeta por ejecución de pytest
atexit.register(shutil.rmtree, _CACHE, True)


def _cached(path: Path, key: tuple, build) -> Path:
    """Escribir etiquetas con ExifTool cuesta ~1 s por archivo en Windows. Los medios con las mismas etiquetas son
    idénticos, así que se generan UNA vez por ejecución y después se copian (mismo contenido; fecha de copia nueva)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(repr(key).encode()).hexdigest() + path.suffix
    cached = _CACHE / name
    if not cached.is_file():
        build(cached)
    shutil.copyfile(cached, path)
    return path


def make_jpeg(path: Path, tags: dict[str, str] | None = None) -> Path:
    def build(dest: Path) -> None:
        from PIL import Image
        Image.new("RGB", (2, 2), (255, 255, 255)).save(dest, "JPEG")   # JPEG válido sintético
        if tags:
            _write_tags(dest, tags)
    return _cached(path, ("jpeg", tuple(sorted((tags or {}).items()))), build)


def make_png(path: Path) -> Path:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = zlib.compress(b"\x00\xff\xff\xff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    return path


def make_mov(path: Path, tags: dict[str, str] | None = None, mdat_before_moov: int = 0) -> Path:
    """MOV mínimo con átomo moov. Con ``mdat_before_moov`` > 0 el moov queda AL FINAL (como en iPhone)."""
    def build(dest: Path) -> None:
        base = (_box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ") + _box(b"moov", _mvhd())
                + _box(b"mdat", b"\0" * 16))
        dest.write_bytes(base)
        if tags:
            _write_tags(dest, tags)
        if mdat_before_moov:
            data = dest.read_bytes()
            ftyp_len = struct.unpack(">I", data[:4])[0]
            rest = data[ftyp_len:]
            # reordenar: ftyp + mdat grande + (todo lo demás, que contiene el moov con las etiquetas)
            dest.write_bytes(data[:ftyp_len] + _box(b"mdat", b"\xab" * mdat_before_moov) + rest)
    return _cached(path, ("mov", tuple(sorted((tags or {}).items())), mdat_before_moov), build)
