"""Barra de progreso de texto (ASCII, segura en cualquier consola de Windows)."""
from __future__ import annotations

import sys
import time

from iphonephotosync.app.status import Status


def fmt_bytes(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "calculando…"
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{max(m, 1)} min" if seconds >= 30 else "<1 min"


def bar(percent: float, width: int = 28) -> str:
    filled = int(width * max(0.0, min(100.0, percent)) / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {percent:5.1f}%"


def render_status(st: Status) -> str:
    lines = [bar(st.percent_bytes)]
    inflight = f" (+{fmt_bytes(st.part_bytes)} en curso)" if st.part_bytes else ""
    lines.append(f"Confirmado: {fmt_bytes(st.done_bytes)}{inflight} de {fmt_bytes(st.total_bytes)}  |  "
                 f"Archivos: {st.done_files} de {st.total_files}")
    if st.files_per_s:
        lines.append(f"Ritmo reciente: {st.files_per_s:.2f} archivos/s, {(st.bytes_per_s or 0) / 1e6:.1f} MB/s")
    if st.pending_files:
        lines.append(f"Tiempo restante estimado: {fmt_eta(st.eta_seconds)}"
                     + (f"  (modelo: {st.eta_basis})" if st.eta_basis else ""))
    extra = {k: v[0] for k, v in st.by_state.items() if k not in ("committed", "discovered", "staged", "verified")}
    if extra:
        lines.append("Otros: " + ", ".join(f"{k} {n}" for k, n in sorted(extra.items())))
    if st.session_status:
        when = "" if st.seconds_since_last is None else f" | último archivo confirmado hace {int(st.seconds_since_last)} s"
        lines.append(f"Sesión {st.session_id}: {st.session_status}{when}")
        if st.session_status == "running" and st.parts_in_progress:
            act = st.seconds_since_staging_activity
            lines.append(f"Copiando ahora: {st.parts_in_progress} archivos"
                         + ("" if act is None else f" (último crecimiento hace {int(act)} s)"))
    if st.stalled:
        lines.append("AVISO: sin actividad hace más de 3 minutos (¿iPhone bloqueado o cable desconectado?).")
    return "\n".join(lines)


class LiveBar:
    """Progreso en vivo de ``backup``. En consola interactiva se redibuja una línea; si la salida está
    redirigida a un archivo/tubería, imprime una línea completa cada ``interval`` segundos."""

    def __init__(self, total_files: int, total_bytes: int, out=None, interval: float = 0.5, plain_interval: float = 15.0):
        self.out = out or sys.stdout
        self.total_files, self.total_bytes = total_files, total_bytes
        self.files = 0
        self.bytes_done = 0           # bytes de archivos ya terminados
        self._cur: dict[int, int] = {}    # item -> bytes ya leídos de cada archivo EN CURSO (varios a la vez)
        self.name = ""
        self.t0 = time.perf_counter()
        self._last = 0.0
        self._tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._interval = interval if self._tty else plain_interval
        self._ewma: float | None = None
        self._mark = (self.t0, 0)

    @property
    def current(self) -> int:
        return sum(self._cur.values())

    def line(self) -> str:
        done = self.bytes_done + self.current
        pct = 100.0 * done / self.total_bytes if self.total_bytes else 100.0
        now = time.perf_counter()
        t, b0 = self._mark
        if now - t >= 2:
            inst = (done - b0) / (now - t)
            self._ewma = inst if self._ewma is None else 0.7 * self._ewma + 0.3 * inst
            self._mark = (now, done)
        speed = self._ewma or (done / max(now - self.t0, 1e-6))
        eta = (self.total_bytes - done) / speed if speed > 0 and self.total_bytes > done else None
        return (f"{bar(pct, 24)} {fmt_bytes(done)}/{fmt_bytes(self.total_bytes)} | {speed / 1e6:.1f} MB/s | "
                f"quedan {fmt_eta(eta)} | {self.files}/{self.total_files} | {self.name[:24]}")

    def _emit(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last < self._interval:
            return
        self._last = now
        if self._tty:
            self.out.write("\r" + self.line().ljust(118))
        else:
            self.out.write(self.line() + "\n")
        self.out.flush()

    def event(self, kind: str, **k) -> None:
        item = k.get("item", 0)
        if kind == "file_start":
            self.name = k.get("path", "")
            self._cur.setdefault(item, 0)
        elif kind == "bytes":
            self.name = k.get("path", self.name)
            self._cur[item] = k.get("done", 0)
        elif kind == "file_done":
            self.files += 1
            self.bytes_done += k.get("size", 0)
            self._cur.pop(item, None)
        elif kind == "file_error":
            self._cur.pop(item, None)
        elif kind == "finished":
            self._emit(force=True)
            if self._tty:
                self.out.write("\n")
            return
        self._emit()
