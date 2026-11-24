"""Obtiene los átomos ``ftyp`` + ``moov`` de un MOV/MP4 recorriendo solo las cabeceras (con saltos),
para leer metadatos sin descargar el vídeo completo. En vídeos de iPhone el ``moov`` suele estar al final.

El archivo sintético resultante (ftyp+moov) sirve solo para extraer metadatos; nunca se usa como copia.
"""
from __future__ import annotations

import struct
from typing import Callable

ReadAt = Callable[[int, int], bytes]
MAX_MOOV = 64 * 1024 * 1024


def extract_ftyp_moov(read_at: ReadAt, size: int, max_moov: int = MAX_MOOV) -> bytes | None:
    """Devuelve ftyp+moov (bytes) o None si no se encuentra un moov razonable."""
    offset, ftyp, hops = 0, b"", 0
    while offset + 8 <= size and hops < 4096:
        hops += 1
        head = read_at(offset, 16)
        if len(head) < 8:
            return None
        box_size, box_type = struct.unpack(">I4s", head[:8])
        header_len = 8
        if box_size == 1:
            if len(head) < 16:
                return None
            box_size = struct.unpack(">Q", head[8:16])[0]
            header_len = 16
        elif box_size == 0:
            box_size = size - offset
        if box_size < header_len or offset + box_size > size:
            return None   # estructura inconsistente / archivo truncado
        if box_type == b"ftyp" and box_size <= 4096:
            ftyp = read_at(offset, box_size)
        elif box_type == b"moov":
            if box_size > max_moov:
                return None
            moov = read_at(offset, box_size)
            return ftyp + moov if len(moov) == box_size else None
        offset += box_size
    return None
