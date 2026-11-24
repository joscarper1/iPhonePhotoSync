"""Modelo de progreso de la pantalla de respaldo (sin Qt: se prueba con eventos simulados)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


def fmt_bytes(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "calculando…"
    m = int(seconds // 60)
    if m >= 60:
        return f"{m // 60} h {m % 60:02d} min"
    return f"{max(m, 1)} min" if seconds >= 30 else "<1 min"


@dataclass
class ProgressModel:
    total_files: int = 0
    total_bytes: int = 0
    files: int = 0
    bytes_done: int = 0                              # bytes de archivos ya terminados
    current: str = ""
    cur: dict[int, int] = field(default_factory=dict)     # archivo en curso -> bytes leídos (varios a la vez)
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    _ewma: float | None = None
    _mark: tuple[float, int] | None = None

    def event(self, kind: str, **k) -> None:
        item = k.get("item", 0)
        if kind == "start":
            self.total_files, self.total_bytes = k["total"], k["bytes"]
            self.started = time.perf_counter()
            self._mark = (self.started, 0)
        elif kind == "file_start":
            self.current = k.get("path", "")
            self.cur.setdefault(item, 0)
        elif kind == "bytes":
            self.current = k.get("path", self.current)
            self.cur[item] = k.get("done", 0)
        elif kind == "file_done":
            self.files += 1
            self.bytes_done += k.get("size", 0)
            self.cur.pop(item, None)
        elif kind == "file_error":
            self.cur.pop(item, None)
            self.errors.append(f"{k.get('path', '')}: {k.get('message', '')}")

    @property
    def done(self) -> int:
        return self.bytes_done + sum(self.cur.values())

    @property
    def percent(self) -> float:
        return min(100.0, 100.0 * self.done / self.total_bytes) if self.total_bytes else 0.0

    def speed(self, now: float | None = None) -> float | None:
        """Bytes/s suavizados (media móvil); None hasta tener una ventana de ≥2 s."""
        now = now or time.perf_counter()
        if self._mark is None:
            return None
        t, b0 = self._mark
        if now - t >= 2:
            inst = (self.done - b0) / (now - t)
            self._ewma = inst if self._ewma is None else 0.7 * self._ewma + 0.3 * inst
            self._mark = (now, self.done)
        return self._ewma

    def eta(self, now: float | None = None) -> float | None:
        sp = self.speed(now)
        remaining = self.total_bytes - self.done
        return remaining / sp if sp and sp > 0 and remaining > 0 else None
