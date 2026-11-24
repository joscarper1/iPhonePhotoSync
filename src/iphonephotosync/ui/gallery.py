"""Galería de lo pendiente: miniatura + casilla por elemento, con carga diferida (solo lo visible)."""
from __future__ import annotations

import threading

from PySide6.QtCore import QPoint, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from iphonephotosync.app.thumbnails import FATAL, ThumbnailService
from iphonephotosync.app.workflow import PendingGroup
from iphonephotosync.domain.models import DeviceFile
from iphonephotosync.ui.progress_model import fmt_bytes

ICON = 150
KIND_LABEL = {"photo": "FOTO", "video": "VÍDEO", "live": "LIVE", "other": "ARCHIVO"}


def placeholder(text: str, sub: str = "") -> QPixmap:
    pm = QPixmap(ICON, ICON)
    pm.fill(QColor("#e6e6e6"))
    p = QPainter(pm)
    p.setPen(QColor("#777777"))
    f = QFont()
    f.setPointSize(9)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, text + (f"\n{sub}" if sub else ""))
    p.end()
    return pm


def pixmap_from_jpeg(data: bytes) -> QPixmap | None:
    img = QImage.fromData(data, "JPEG")
    if img.isNull():
        return None
    pm = QPixmap.fromImage(img).scaled(ICON, ICON, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    canvas = QPixmap(ICON, ICON)
    canvas.fill(QColor("#202020"))
    p = QPainter(canvas)
    p.drawPixmap((ICON - pm.width()) // 2, (ICON - pm.height()) // 2, pm)
    p.end()
    return canvas


class ThumbWorker(QThread):
    """Un solo hilo (la conexión con el iPhone es una sola): carga, de lo que se está viendo, lo más reciente."""
    loaded = Signal(int, object, str)        # fila, bytes JPEG o None, origen
    fatal = Signal(str)

    def __init__(self, service: ThumbnailService, files: dict[int, DeviceFile], parent=None):
        super().__init__(parent)
        self.service, self.files = service, files
        self._wanted: list[int] = []
        self._done: set[int] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    def want(self, rows: list[int]) -> None:
        with self._lock:
            self._wanted = [r for r in rows if r not in self._done]
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        while not self._stop:
            with self._lock:
                row = self._wanted.pop(0) if self._wanted else None
            if row is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if row in self._done:
                continue
            try:
                th = self.service.get(self.files[row])
            except FATAL as e:
                self.fatal.emit(str(e))
                return
            except Exception:  # noqa: BLE001 - nunca debe tumbar la interfaz
                th = None
            self._done.add(row)
            self.loaded.emit(row, th.data if th else None, th.origin if th else "")


class PendingGallery(QWidget):
    selectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: list[PendingGroup] = []
        self._worker: ThumbWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.lbl = QLabel("")
        b_all = QPushButton("Marcar todo")
        b_none = QPushButton("Quitar todo")
        b_all.clicked.connect(lambda: self.set_all(True))
        b_none.clicked.connect(lambda: self.set_all(False))
        top.addWidget(self.lbl, 1)
        top.addWidget(b_all)
        top.addWidget(b_none)
        lay.addLayout(top)
        self.view = QListWidget()
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setIconSize(QSize(ICON, ICON))
        self.view.setGridSize(QSize(ICON + 24, ICON + 74))
        self.view.setWordWrap(True)
        self.view.setUniformItemSizes(True)
        self.view.itemChanged.connect(lambda _i: self._on_change())
        lay.addWidget(self.view, 1)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self._tick = QTimer(self)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._request_visible)
        self._pm_cache: dict[int, bool] = {}

    # ------------------------------------------------------------------ carga
    def load(self, groups: list[PendingGroup], files: dict[str, DeviceFile], service: ThumbnailService | None) -> None:
        self.stop()
        self.groups = groups
        self.view.blockSignals(True)
        self.view.clear()
        for g in groups:
            star = "★ " if g.favorite else ""
            album = f"\nÁlbum: {g.album[:16]}{'…' if len(g.album) > 16 else ''}" if g.album else ""
            it = QListWidgetItem(QIcon(placeholder(KIND_LABEL.get(g.kind, "?"), "cargando…")),
                                 f"{star}{g.name}\n{fmt_bytes(g.size)}" +
                                 (f" · {len(g.members)} archivos" if len(g.members) > 1 else "") + album)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setToolTip(f"{g.name}\n{g.reason}" + (f"\nFecha de captura: {g.capture_dt.replace('T', ' ')}" if g.capture_dt else "") +
                          (f"\nSe guardará en el álbum: {g.album}" if g.album else "") + ("\nFavorito" if g.favorite else ""))
            self.view.addItem(it)
        self.view.blockSignals(False)
        self.note.setText("")
        self._on_change()
        if service is not None and groups:
            rowfiles = {i: files[g.leader] for i, g in enumerate(groups) if g.leader in files}
            self._worker = ThumbWorker(service, rowfiles, self)
            self._worker.loaded.connect(self._on_loaded)
            self._worker.fatal.connect(lambda m: self.note.setText(
                "Se perdió la conexión con el iPhone: no se pueden cargar más vistas previas. "
                "Puedes seguir eligiendo por nombre."))
            self._worker.start()
            self._tick.start()
            QTimer.singleShot(0, self._request_visible)

    def stop(self) -> None:
        self._tick.stop()
        w, self._worker = self._worker, None
        if w is not None:
            w.stop()
            w.wait(15000)

    def _visible_rows(self) -> list[int]:
        n = self.view.count()
        if n == 0:
            return []
        vp = self.view.viewport().rect()
        first = self.view.indexAt(QPoint(8, 8)).row()
        first = 0 if first < 0 else first
        rows = []
        for r in range(max(0, first - 6), n):                  # unas filas por encima: ya estarán listas al subir
            rect = self.view.visualItemRect(self.view.item(r))
            if rect.top() > vp.bottom() + ICON:                 # y una fila por debajo
                break
            rows.append(r)
        return rows

    def _request_visible(self) -> None:
        if self._worker is None:
            return
        rows = [r for r in self._visible_rows() if r not in self._pm_cache]
        self._worker.want(rows)

    def _on_loaded(self, row: int, data, origin: str) -> None:
        if row >= self.view.count():
            return
        self._pm_cache[row] = True
        item = self.view.item(row)
        pm = pixmap_from_jpeg(data) if data else None
        if pm is None:
            g = self.groups[row]
            pm = placeholder(KIND_LABEL.get(g.kind, "?"), "sin vista previa")
        self.view.blockSignals(True)
        item.setIcon(QIcon(pm))
        self.view.blockSignals(False)

    # ------------------------------------------------------------------ selección
    def set_all(self, on: bool) -> None:
        self.view.blockSignals(True)
        for i in range(self.view.count()):
            self.view.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.view.blockSignals(False)
        self._on_change()

    def selected_leaders(self) -> set[str]:
        return {g.leader for i, g in enumerate(self.groups) if self.view.item(i).checkState() == Qt.Checked}

    def _on_change(self) -> None:
        sel = [g for i, g in enumerate(self.groups) if self.view.item(i).checkState() == Qt.Checked]
        self.lbl.setText(f"Marcados {len(sel)} de {len(self.groups)} elementos ({fmt_bytes(sum(g.size for g in sel))}). "
                         "Desmarca lo que no quieras respaldar.")
        self.selectionChanged.emit()
