"""Adaptador de solo lectura: un ``ReadHandle`` del iPhone visto como archivo con ``read``/``seek``.
Permite que Pillow o PyAV lean SOLO los trozos que necesitan (p. ej. el primer fotograma de un vídeo de 7 GB
lee ~3 MB) en lugar de bajar el archivo completo."""
from __future__ import annotations

import io

MAX_READ = 4 * 1024 * 1024


class RemoteFile(io.RawIOBase):
    def __init__(self, handle):
        self.h, self.pos, self.size = handle, 0, handle.size
        self.bytes_read = 0
        self.reads = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = 0 if whence == 0 else self.pos if whence == 1 else self.size
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, b) -> int:
        n = min(len(b), MAX_READ, max(0, self.size - self.pos))
        if n <= 0:
            return 0
        data = self.h.read_at(self.pos, n)
        b[: len(data)] = data
        self.pos += len(data)
        self.bytes_read += len(data)
        self.reads += 1
        return len(data)

    def buffered(self, size: int = 1 << 16) -> io.BufferedReader:
        return io.BufferedReader(self, buffer_size=size)
