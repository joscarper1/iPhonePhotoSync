"""Estado de una copia en curso (o la última), leído del manifiesto en SOLO LECTURA.

No interfiere con el proceso de copia: abre SQLite con ``mode=ro`` y no escribe nada.
La estimación de tiempo ajusta  t(archivo) = a + tamaño / b  con los últimos archivos confirmados
(a = coste fijo por archivo; b = velocidad sostenida), porque con miles de fotos pequeñas la velocidad en MB/s
engaña: manda el coste por archivo.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PENDING = ("discovered", "staged", "verified")


@dataclass
class Status:
    session_id: int | None
    session_status: str | None
    by_state: dict[str, tuple[int, int]] = field(default_factory=dict)    # estado -> (archivos, bytes)
    done_files: int = 0
    done_bytes: int = 0
    pending_files: int = 0
    pending_bytes: int = 0
    seconds_since_last: float | None = None
    files_per_s: float | None = None
    bytes_per_s: float | None = None
    eta_seconds: float | None = None
    eta_basis: str = ""
    session_started_ago: float | None = None
    parts_in_progress: int = 0                    # archivos .part en _Staging (copiándose ahora)
    seconds_since_staging_activity: float | None = None   # desde que los .part dejaron de crecer (None = desconocido)
    part_bytes: int = 0                                    # bytes ya escritos en los .part en curso

    @property
    def total_files(self) -> int:
        return self.done_files + self.pending_files

    @property
    def total_bytes(self) -> int:
        return self.done_bytes + self.pending_bytes

    @property
    def percent_bytes(self) -> float:
        """Avance real: confirmado + lo que ya está escrito en los .part en curso."""
        if not self.total_bytes:
            return 100.0
        return min(100.0, 100.0 * (self.done_bytes + self.part_bytes) / self.total_bytes)

    @property
    def idle_seconds(self) -> float | None:
        """Tiempo sin actividad real: el más reciente entre el último archivo confirmado, el inicio de la sesión
        y el último crecimiento de los .part (un vídeo grande en curso no es un atasco)."""
        cands = [x for x in (self.seconds_since_last, self.session_started_ago,
                             self.seconds_since_staging_activity) if x is not None]
        return min(cands) if cands else None

    @property
    def stalled(self) -> bool:
        idle = self.idle_seconds
        if self.session_status != "running" or idle is None or idle <= 180 or self.pending_files == 0:
            return False
        if self.parts_in_progress and self.seconds_since_staging_activity is None:
            return False          # hay archivos en copia y no sé si crecen (consulta única): no acuso sin pruebas
        return True


_GROWTH: dict[str, tuple[int, float]] = {}     # carpeta de staging -> (bytes totales, instante del último cambio)


def _read_staging(staging: Path, st: Status, now: datetime) -> None:
    """Solo lectura: cuántos .part hay, cuántos bytes llevan y si siguen CRECIENDO.
    No se usa la fecha de modificación: en exFAT no se actualiza mientras el archivo está abierto.
    El crecimiento se detecta comparando muestras sucesivas (modo --watch); en una sola consulta es desconocido."""
    import os
    total = n = 0
    try:
        with os.scandir(staging) as it:
            for e in it:
                if e.name.endswith(".part"):
                    n += 1
                    total += e.stat().st_size
    except OSError:
        return
    st.parts_in_progress, st.part_bytes = n, total
    key, t = str(staging), now.timestamp()
    prev = _GROWTH.get(key)
    if n == 0:
        _GROWTH.pop(key, None)
    elif prev is None or total != prev[0]:
        _GROWTH[key] = (total, t)
        st.seconds_since_staging_activity = 0.0 if prev is not None else None
    else:
        st.seconds_since_staging_activity = max(0.0, t - prev[1])


def fit_time_model(samples: list[tuple[float, int]]) -> tuple[float, float] | None:
    """samples: (segundos que tardó el archivo, tamaño). Ajuste por mínimos cuadrados d = a + s/b.
    Devuelve (a, b) con a >= 0 y b > 0, o None si no hay datos suficientes/consistentes."""
    n = len(samples)
    if n < 8:
        return None
    mx = sum(s for _, s in samples) / n
    my = sum(d for d, _ in samples) / n
    sxx = sum((s - mx) ** 2 for _, s in samples)
    if sxx == 0:
        return (my, 0.0) if my > 0 else None       # todos del mismo tamaño: solo coste fijo
    slope = sum((s - mx) * (d - my) for d, s in samples) / sxx      # segundos por byte
    a = max(0.0, my - slope * mx)
    if slope <= 0:
        return (my, 0.0)
    return a, 1.0 / slope


def read_status(manifest_path: Path | str, *, window: int = 300, now: datetime | None = None) -> Status:
    now = now or datetime.now(timezone.utc)
    uri = f"file:{Path(manifest_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        sess = con.execute("SELECT id, status, started_at FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        st = Status(sess["id"] if sess else None, sess["status"] if sess else None)
        if sess:
            st.session_started_ago = max(0.0, (now - datetime.fromisoformat(sess["started_at"])).total_seconds())
        for r in con.execute("SELECT copy_status s, COUNT(*) n, COALESCE(SUM(size),0) b FROM items GROUP BY copy_status"):
            st.by_state[r["s"]] = (r["n"], r["b"])
        committed = st.by_state.get("committed", (0, 0))
        st.done_files, st.done_bytes = committed
        # lo ya presente en la biblioteca (skipped_dup) no se copia: no cuenta en el total de trabajo
        st.pending_files = sum(st.by_state.get(k, (0, 0))[0] for k in PENDING)
        st.pending_bytes = sum(st.by_state.get(k, (0, 0))[1] for k in PENDING)
        rows = con.execute("SELECT imported_at t, size s FROM items WHERE copy_status='committed' AND "
                           "imported_at IS NOT NULL ORDER BY imported_at DESC LIMIT ?", (window,)).fetchall()
    finally:
        con.close()
    _read_staging(Path(manifest_path).parent.parent / "_Staging", st, now)
    if not rows:
        return st
    stamps = [(datetime.fromisoformat(r["t"]), r["s"]) for r in rows]          # más reciente primero
    st.seconds_since_last = max(0.0, (now - stamps[0][0]).total_seconds())
    # duración por archivo = diferencia entre confirmaciones consecutivas (se descartan pausas largas)
    samples = []
    for (t1, s1), (t0, _) in zip(stamps, stamps[1:]):
        d = (t1 - t0).total_seconds()
        if 0 < d < 120:
            samples.append((d, s1))
    if samples:
        total_t = sum(d for d, _ in samples)
        st.files_per_s = len(samples) / total_t
        st.bytes_per_s = sum(s for _, s in samples) / total_t
    model = fit_time_model(samples)
    if st.pending_files:
        if model and model[1] > 0:
            a, b = model
            st.eta_seconds = a * st.pending_files + st.pending_bytes / b
            st.eta_basis = f"{a:.2f} s por archivo + {b / 1e6:.0f} MB/s"
        elif st.bytes_per_s:
            st.eta_seconds = st.pending_bytes / st.bytes_per_s
            st.eta_basis = "velocidad reciente (poco fiable)"
    return st
