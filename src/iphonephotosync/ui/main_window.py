"""Ventana principal: Principal -> Escaneo -> Respaldo -> Resumen. Todo el trabajo pesado va en hilos."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from iphonephotosync.app.workflow import (
    FinalSummary, ScanInvalid, ScanReport, UserSettings, Workflow, friendly, normalize_dest,
)
from iphonephotosync.support import SupportConfig, install_support_menu
from iphonephotosync.support_config import SUPPORT
from iphonephotosync.transfer.engine import EngineControl
from iphonephotosync.ui.dialogs import HistoryDialog, SettingsDialog
from iphonephotosync.ui.gallery import PendingGallery
from iphonephotosync.ui.phases import PhasePanel
from iphonephotosync.ui.progress_model import ProgressModel, fmt_bytes, fmt_eta

PAGE_HOME, PAGE_SCAN, PAGE_BACKUP, PAGE_SUMMARY = range(4)
OUTCOMES = {
    "completed": "Respaldo completado", "cancelled": "Respaldo cancelado (lo ya copiado se conserva)",
    "device_lost": "Se perdió la conexión con el iPhone (puedes reanudar)", "no_space": "Sin espacio en el destino",
    "destination_error": "Error en el destino", "blocked": "No se pudo iniciar", "failed": "Terminó con un fallo",
}


class Task(QThread):
    """Ejecuta ``fn(task)`` fuera del hilo de la interfaz y devuelve el resultado por señal."""
    finished_ok = Signal(object)
    failed = Signal(object)
    progress = Signal(int, int)
    event = Signal(str, object)
    stage = Signal(str)
    phase = Signal(str, int, int, str, bool)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.finished_ok.emit(self._fn(self))
        except BaseException as e:  # noqa: BLE001 - se informa en pantalla, nunca se pierde en el hilo
            self.failed.emit(e)


def _card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    return box, lay


class MainWindow(QMainWindow):
    def __init__(self, workflow: Workflow, settings: UserSettings, *, simulated: bool = False, save_settings=True,
                 support: SupportConfig | None = None):
        super().__init__()
        self.wf, self.settings = workflow, settings
        self._save = save_settings
        fixed, _note = normalize_dest(settings.dest)             # destino guardado dentro de la biblioteca: se corrige
        if fixed != settings.dest:
            self.settings = self.wf.settings = UserSettings(**{**settings.__dict__, "dest": fixed})
            if save_settings:
                try:
                    self.settings.save()
                except OSError:
                    pass
        self.setWindowTitle("iPhonePhotoSync" +(" — MODO SIMULACIÓN (archivos de muestra)" if simulated else ""))
        self.resize(980, 700)
        self._task: Task | None = None
        self._model = ProgressModel()
        self._control: EngineControl | None = None
        self._device_ok = False
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_scan())
        self.stack.addWidget(self._build_backup())
        self.stack.addWidget(self._build_summary())
        # Ayuda > Apoyar este proyecto: solo aparece si hay algún proveedor con URL válida (si no, no hay menú alguno)
        self.support_action = install_support_menu(self, support or SupportConfig.from_dict(SUPPORT))
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self.refresh_home()
        QTimer.singleShot(0, self.check_device)

    # ===================================================================== 7.1 principal
    def _build_home(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        title = QLabel("<h2>Respaldo de fotos y vídeos del iPhone</h2>")
        lay.addWidget(title)
        dev, dl = _card("iPhone")
        self.lbl_device = QLabel("Buscando iPhone…")
        self.lbl_trust = QLabel("")
        dl.addWidget(self.lbl_device)
        dl.addWidget(self.lbl_trust)
        self.lbl_snapshot = QLabel("")
        self.lbl_snapshot.setWordWrap(True)
        dl.addWidget(self.lbl_snapshot)
        # Solo hay UN botón principal de escaneo; elegir el modo es automático. Este enlace aparece únicamente
        # cuando hay un escaneo reciente (el único caso en que forzar uno completo es una decisión real del usuario).
        self.btn_full = QPushButton("Hacer un escaneo completo ahora")
        self.btn_full.setFlat(True)
        self.btn_full.setCursor(Qt.PointingHandCursor)
        self.btn_full.setStyleSheet("text-align:left; color:#1a5fb4; text-decoration:underline; border:none;")
        self.btn_full.setToolTip("Vuelve a recorrer tu biblioteca del destino para encontrar contenido que hayas agregado.")
        self.btn_full.clicked.connect(lambda: self.start_scan(force_full=True))
        self.btn_full.setVisible(False)
        dl.addWidget(self.btn_full)
        b = QPushButton("Buscar de nuevo")
        b.clicked.connect(self.check_device)
        dl.addWidget(b)
        lay.addWidget(dev)
        dst, ll = _card("Disco de destino")
        self.lbl_dest = QLabel("")
        self.lbl_free = QLabel("")
        ll.addWidget(self.lbl_dest)
        ll.addWidget(self.lbl_free)
        b2 = QPushButton("Elegir destino…")
        b2.clicked.connect(self.choose_dest)
        ll.addWidget(b2)
        lay.addWidget(dst)
        row = QHBoxLayout()
        self.btn_scan = QPushButton("Escanear iPhone")
        self.btn_scan.clicked.connect(lambda: self.start_scan())
        self.btn_start = QPushButton("Iniciar respaldo")
        self.btn_start.clicked.connect(self.start_backup)
        self.btn_start.setEnabled(False)
        for b in (self.btn_scan, self.btn_start):
            b.setMinimumHeight(40)
            row.addWidget(b)
        lay.addLayout(row)
        self.lbl_why = QLabel("")
        self.lbl_why.setWordWrap(True)
        lay.addWidget(self.lbl_why)
        self.phases = PhasePanel(Workflow.PHASES)          # las fases se ven desde antes de empezar
        lay.addWidget(self.phases)
        lay.addStretch(1)
        row2 = QHBoxLayout()
        bh = QPushButton("Historial de respaldos")
        bh.clicked.connect(self.show_history)
        bs = QPushButton("Configuración")
        bs.clicked.connect(self.show_settings)
        row2.addWidget(bh)
        row2.addWidget(bs)
        row2.addStretch(1)
        lay.addLayout(row2)
        return w

    def refresh_home(self) -> None:
        d = self.settings.dest
        self.lbl_dest.setText(d or "Sin elegir")
        free = ""
        if d:
            from iphonephotosync.transfer.space import free_bytes
            try:
                f, _ = free_bytes(d)
                free = f"Espacio libre: {fmt_bytes(f)}"
            except OSError:
                free = "El destino no está disponible"
        self.lbl_free.setText(free)
        self._update_start()

    def _update_start(self) -> None:
        ok, why = self.wf.can_start()
        self.btn_start.setEnabled(ok)
        self.btn_scan.setEnabled(self._device_ok and self._task is None)
        self.lbl_why.setText("" if ok else why)
        self._refresh_snapshot_label()

    def _refresh_snapshot_label(self) -> None:
        snap = self.wf.snapshot_info() if self._device_ok else None
        fresh = snap is not None and not self.wf.is_stale(snap)
        self.btn_full.setVisible(fresh and self.btn_scan.isEnabled())       # solo cuando es una decisión real
        if not self._device_ok:
            self.lbl_snapshot.setText("")
        elif snap is None:
            self.lbl_snapshot.setText("Aún no hay un escaneo guardado de este iPhone: el primero será completo.")
        else:
            hours = snap.age_hours()
            ago = f"{hours:.0f} h" if hours >= 1 else f"{hours * 60:.0f} min"
            txt = f"Último escaneo completo de este iPhone: hace {ago} ({snap.saved_at.astimezone():%d/%m/%Y %H:%M})."
            if not fresh:
                txt += " Ya pasó mucho tiempo: al escanear te preguntaré si quieres uno completo."
            self.lbl_snapshot.setText(txt)

    def check_device(self) -> None:
        if self._task is not None:
            return
        self.gallery.stop()
        self.lbl_device.setText("Buscando iPhone…")
        self.btn_scan.setEnabled(False)
        t = Task(lambda _t: self.wf.connect(), self)
        t.finished_ok.connect(self._device_found)
        t.failed.connect(lambda e: self._device_found(None, str(e)))
        self._run(t)

    def _device_found(self, st, err: str = "") -> None:
        self._task = None
        self._device_ok = bool(st and st.connected)
        if st is None:
            self.lbl_device.setText(f"Error: {err}")
            self.lbl_trust.setText("")
        elif st.connected:
            self.lbl_device.setText(f"Conectado: {st.model or 'iPhone'} (iOS {st.ios or '?'})")
            self.lbl_trust.setText("Autorizado: este equipo es de confianza")
        else:
            self.lbl_device.setText("iPhone no disponible")
            self.lbl_trust.setText(st.message)
        self._update_start()

    def choose_dest(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Carpeta de destino del respaldo", self.settings.dest or str(Path.home()))
        if d:
            self._apply_settings(UserSettings(**{**self.settings.__dict__, "dest": d}))

    def show_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self._apply_settings(dlg.result_settings())

    def _apply_settings(self, s: UserSettings) -> None:
        fixed, note = normalize_dest(s.dest)
        if note:
            s = UserSettings(**{**s.__dict__, "dest": fixed})
            QMessageBox.information(self, "Destino ajustado", note)
        changed = s != self.settings
        self.settings = s
        self.wf.settings = s
        if self._save:
            try:
                s.save()
            except OSError:
                pass
        if changed:
            self.wf.plan = self.wf.report = None          # el plan depende de la configuración: hay que reescanear
        self.refresh_home()

    def show_history(self) -> None:
        HistoryDialog(self.wf.history(), self).exec()

    # ===================================================================== 7.2 resultado: qué falta respaldar
    def _build_scan(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("<h2>¿Qué falta por respaldar?</h2>"))
        self.lbl_headline = QLabel("")
        self.lbl_headline.setWordWrap(True)
        lay.addWidget(self.lbl_headline)
        why, wl = _card("Por qué hay que respaldarlos")
        self.reasons = QTableWidget(0, 3)
        self.reasons.setHorizontalHeaderLabels(["Motivo", "Archivos", "Tamaño"])
        self.reasons.setEditTriggers(QTableWidget.NoEditTriggers)
        self.reasons.horizontalHeader().setStretchLastSection(True)
        self.reasons.setMaximumHeight(170)
        wl.addWidget(self.reasons)
        lay.addWidget(why)
        self.gallery_card, gl = _card("Elige qué respaldar")
        self.gallery = PendingGallery()
        self.gallery.selectionChanged.connect(self._selection_changed)
        gl.addWidget(self.gallery)
        lay.addWidget(self.gallery_card, 1)
        self.lbl_already = QLabel("")
        self.lbl_already.setWordWrap(True)
        lay.addWidget(self.lbl_already)
        self.lbl_space = QLabel("")
        self.lbl_space.setWordWrap(True)
        lay.addWidget(self.lbl_space)
        self.unreadable_list = QListWidget()
        self.unreadable_list.setMaximumHeight(110)
        self.unreadable_list.setVisible(False)
        self.lbl_unreadable = QLabel("")
        self.lbl_unreadable.setVisible(False)
        lay.addWidget(self.lbl_unreadable)
        lay.addWidget(self.unreadable_list)
        btns = QHBoxLayout()
        back = QPushButton("Volver")
        back.clicked.connect(self._back_home)
        self.btn_start2 = QPushButton("Iniciar respaldo")
        self.btn_start2.setMinimumHeight(40)
        self.btn_start2.clicked.connect(self.start_backup)
        btns.addWidget(back)
        btns.addStretch(1)
        btns.addWidget(self.btn_start2)
        lay.addLayout(btns)
        return w

    def _back_home(self) -> None:
        self.gallery.stop()
        self.stack.setCurrentIndex(PAGE_HOME)

    def _ask_full(self, snap) -> str | None:
        """Pasadas las horas configuradas desde el último escaneo completo, el usuario decide: 'full', 'quick' o None."""
        box = QMessageBox(self)
        box.setWindowTitle("¿Hacer un escaneo completo?")
        box.setIcon(QMessageBox.Question)
        box.setText(f"El último escaneo completo de este iPhone fue hace {snap.age_hours():.0f} h "
                    f"({snap.saved_at.astimezone():%d/%m/%Y %H:%M}).")
        box.setInformativeText(
            "Un escaneo completo vuelve a recorrer tu biblioteca del destino para encontrar contenido nuevo "
            "y tarda más. El escaneo rápido reutiliza el listado guardado: lo nuevo del iPhone sí se detecta, "
            "pero lo que hayas agregado a tu biblioteca después no se tendrá en cuenta.")
        full = box.addButton("Escaneo completo (recomendado)", QMessageBox.AcceptRole)
        quick = box.addButton("Usar el guardado (rápido)", QMessageBox.ActionRole)
        box.addButton("Cancelar", QMessageBox.RejectRole)
        box.setDefaultButton(full)
        box.exec()
        return "full" if box.clickedButton() is full else "quick" if box.clickedButton() is quick else None

    def start_scan(self, force_full: bool = False) -> None:
        if self._task is not None:
            return
        mode = "full"
        if not force_full:
            snap = self.wf.snapshot_info()
            if snap is not None:
                if self.wf.is_stale(snap):
                    mode = self._ask_full(snap)
                    if mode is None:
                        return
                else:
                    mode = "quick"
        self._scan(mode)

    def _scan(self, mode: str) -> None:
        self.gallery.stop()                      # un escaneo nuevo invalida la galería anterior
        self.phases.reset()
        self.btn_scan.setEnabled(False)
        self.btn_full.setVisible(False)
        self.lbl_why.setText("Escaneando (solo lectura). Son 4 fases; el escaneo " +
                             ("rápido reutiliza el listado guardado de tu biblioteca." if mode == "quick" else
                              "completo recorre tu biblioteca y puede tardar unos minutos."))

        def job(t: Task):
            return self.wf.scan(phase=lambda *a: t.phase.emit(*a), mode=mode)

        t = Task(job, self)
        t.phase.connect(self.phases.update_phase)
        t.finished_ok.connect(self._scan_done)
        t.failed.connect(self._scan_failed)
        self._run(t)

    def _scan_failed(self, e) -> None:
        self._task = None
        self.phases.reset()
        self._device_ok = False
        QMessageBox.warning(self, "No se pudo escanear", friendly(e) if not isinstance(e, ScanInvalid) else str(e))
        self.lbl_why.setText(friendly(e))
        self.check_device()

    def _scan_done(self, r: ScanReport) -> None:
        self._task = None
        if not r.compared:
            head = (f"Tu iPhone tiene <b>{r.items}</b> fotos y vídeos ({fmt_bytes(r.total_bytes)}). "
                    "Elige un disco de destino para saber cuáles ya tienes respaldados.")
            self.btn_start2.setText("Iniciar respaldo")
        elif r.pending_files == 0:
            head = (f"<b>Todo está respaldado.</b> Tu iPhone tiene {r.items} fotos y vídeos y todos ya están en tu "
                    "destino con el mismo contenido. No hay nada que copiar.")
            self.btn_start2.setText("Nada que respaldar")
        else:
            head = (f"Tienes <b>{r.pending_files}</b> archivos en tu iPhone ({fmt_bytes(r.pending_bytes)}) que "
                    f"<b>aún no tienes respaldados</b>. Los otros {r.backed_up} archivos ya están a salvo en tu destino.")
            self.btn_start2.setText(f"Respaldar {r.pending_files} archivos ({fmt_bytes(r.pending_bytes)})")
        self.lbl_headline.setText(head)
        self.reasons.setRowCount(len(r.reasons))
        for i, (label, n, size) in enumerate(r.reasons):
            for j, v in enumerate((label, str(n), fmt_bytes(size))):
                self.reasons.setItem(i, j, QTableWidgetItem(v))
        self.reasons.resizeColumnsToContents()
        self.reasons.parentWidget().setVisible(bool(r.reasons))
        parts = []
        if r.compared:
            parts.append(f"Ya respaldados: {r.backed_up_manifest} copiados por esta herramienta, "
                         f"{r.backed_up_library} ya estaban en tu biblioteca, "
                         f"{r.backed_up_duplicate} con contenido repetido.")
        if r.compared and r.pending_files and r.albums_status in ("ok", "cached"):
            parts.append(f"De lo pendiente, {r.album_items} elementos están en álbumes tuyos (se guardarán dentro de la "
                         f"carpeta de su álbum) y {r.favorite_items} son favoritos (★, se anotan en el registro).")
        parts += r.compare_notes
        self.lbl_already.setText("  ".join(parts))
        extra = [r.space_message] if r.space_message else []
        if r.oversized:
            extra.append(f"{len(r.oversized)} archivos superan el límite del sistema de archivos ({r.fs_name}).")
        self.lbl_space.setText("  ".join(extra))
        self.lbl_unreadable.setText(f"<b>No se pudieron leer {r.unreadable} elementos del iPhone:</b>")
        self.lbl_unreadable.setVisible(bool(r.unreadable_detail))
        self.unreadable_list.clear()
        self.unreadable_list.addItems(r.unreadable_detail)
        self.unreadable_list.setVisible(bool(r.unreadable_detail))
        self.stack.setCurrentIndex(PAGE_SCAN)
        self._update_start()
        self.btn_start2.setEnabled(self.btn_start.isEnabled())
        groups = self.wf.pending_groups() if r.compared else []
        self.gallery_card.setVisible(bool(groups))
        if groups:
            files = {f.path: f for f in self.wf.plan.scan.files}
            self.gallery.load(groups, files, self._make_thumbs())
            self._selection_changed()

    def _make_thumbs(self):
        """Servicio de miniaturas (solo lectura). Si no se pudiera crear, la galería funciona sin imágenes."""
        try:
            return self.wf.thumbnail_service()
        except Exception:  # noqa: BLE001
            return None

    def _selection_changed(self) -> None:
        """Los botones reflejan exactamente lo que se va a copiar con lo marcado."""
        r = self.wf.report
        if r is None or not r.compared or not self.wf.pending_groups():
            return
        sel = self.gallery.selected_leaders()
        self.wf.set_selection(None if len(sel) == len(self.gallery.groups) else sel)
        n, size = self.wf.selection_totals()
        txt = f"Respaldar {n} archivos ({fmt_bytes(size)})" if n else "Nada marcado"
        self.btn_start2.setText(txt)
        self.btn_start.setText("Iniciar respaldo")
        ok, _why = self.wf.can_start()
        self.btn_start2.setEnabled(ok)
        self.btn_start.setEnabled(ok)

    # ===================================================================== 7.4 respaldo
    def _build_backup(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("<h2>Respaldo en curso</h2>"))
        self.lbl_stage = QLabel("")
        lay.addWidget(self.lbl_stage)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.lbl_counts = QLabel("")
        self.lbl_bytes = QLabel("")
        self.lbl_speed = QLabel("")
        self.lbl_current = QLabel("")
        for x in (self.lbl_counts, self.lbl_bytes, self.lbl_speed, self.lbl_current):
            lay.addWidget(x)
        lay.addWidget(QLabel("Errores y advertencias:"))
        self.log = QListWidget()
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.btn_pause = QPushButton("Pausar")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_cancel = QPushButton("Cancelar de forma segura")
        self.btn_cancel.clicked.connect(self.cancel_backup)
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        lay.addLayout(row)
        self.lbl_note = QLabel("Cancelar conserva lo ya copiado y deja el trabajo listo para reanudar. "
                               "Nunca se borra nada válido ni se modifica el iPhone.")
        self.lbl_note.setWordWrap(True)
        lay.addWidget(self.lbl_note)
        return w

    def start_backup(self) -> None:
        if self._task is not None:
            return
        self.gallery.stop()                      # la conexión con el iPhone pasa a ser solo del respaldo
        ok, why = self.wf.can_start()
        if not ok:
            QMessageBox.information(self, "No se puede iniciar", why)
            return
        self._model = ProgressModel()
        self._control = EngineControl()
        self.log.clear()
        self.bar.setValue(0)
        self.lbl_stage.setText("Preparando…")
        self.btn_pause.setText("Pausar")
        self.btn_pause.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.stack.setCurrentIndex(PAGE_BACKUP)

        def job(t: Task):
            return self.wf.run_backup(lambda kind, **k: t.event.emit(kind, k), control=self._control,
                                      on_stage=t.stage.emit)

        t = Task(job, self)
        t.event.connect(self._on_event)
        t.stage.connect(self.lbl_stage.setText)
        t.finished_ok.connect(self._backup_done)
        t.failed.connect(self._backup_failed)
        self._run(t)
        self._timer.start()

    def _on_event(self, kind: str, k: dict) -> None:
        self._model.event(kind, **k)
        if kind == "file_error":
            self.log.addItem(f"Error: {k.get('path', '')}: {k.get('message', '')}")
            self.log.scrollToBottom()
        elif kind == "start":
            self.lbl_stage.setText("Copiando y verificando (SHA-256)…")

    def _tick(self) -> None:
        m = self._model
        self.bar.setValue(int(m.percent * 10))
        self.bar.setFormat(f"{m.percent:.1f} %")
        self.lbl_counts.setText(f"Archivos: {m.files} de {m.total_files}")
        self.lbl_bytes.setText(f"Datos: {fmt_bytes(m.done)} de {fmt_bytes(m.total_bytes)}")
        sp = m.speed()
        paused = self._control is not None and not self._control._running.is_set()
        self.lbl_speed.setText("En pausa" if paused else
                               f"Velocidad: {sp / 1e6:.1f} MB/s  |  Tiempo restante: {fmt_eta(m.eta())}" if sp else
                               "Velocidad: calculando…")
        self.lbl_current.setText(f"Elemento actual: {m.current}" if m.current else "")

    def toggle_pause(self) -> None:
        if not self._control:
            return
        if self._control._running.is_set():
            self._control.pause()
            self.btn_pause.setText("Reanudar")
        else:
            self._control.resume()
            self.btn_pause.setText("Pausar")

    def cancel_backup(self) -> None:
        if not self._control:
            return
        self._control.cancel()
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.lbl_stage.setText("Cancelando de forma segura… termina el archivo en curso y conserva lo copiado.")

    def _backup_failed(self, e) -> None:
        self._task = None
        self._timer.stop()
        self._device_ok = False
        QMessageBox.warning(self, "El respaldo se detuvo", friendly(e) if not isinstance(e, ScanInvalid) else str(e))
        self.stack.setCurrentIndex(PAGE_HOME)
        self.wf.plan = self.wf.report = None
        self.check_device()

    # ===================================================================== 7.5 resumen
    def _build_summary(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_outcome = QLabel("")
        lay.addWidget(self.lbl_outcome)
        self.sum_grid = QGridLayout()
        lay.addLayout(self.sum_grid)
        lay.addWidget(QLabel("Errores y advertencias:"))
        self.sum_errors = QListWidget()
        lay.addWidget(self.sum_errors, 1)
        row = QHBoxLayout()
        b1 = QPushButton("Abrir carpeta de registros")
        b1.clicked.connect(self._open_logs)
        b2 = QPushButton("Volver al inicio")
        b2.clicked.connect(self._home_again)
        row.addWidget(b1)
        row.addStretch(1)
        row.addWidget(b2)
        lay.addLayout(row)
        self._log_dir = ""
        return w

    def _backup_done(self, s: FinalSummary) -> None:
        self._task = None
        self._timer.stop()
        self._tick()
        self._log_dir = s.log_dir
        self.lbl_outcome.setText(f"<h2>{OUTCOMES.get(s.outcome, s.outcome)}</h2>" + (f"<p>{s.message}</p>" if s.message else ""))
        rows = [("Elementos detectados", s.detected), ("Copiados", s.copied),
                ("Ya existentes en tu biblioteca (omitidos)", s.skipped_existing),
                ("Ya respaldados antes (omitidos)", s.skipped_incremental),
                ("Duplicados de contenido (omitidos)", s.skipped_duplicate), ("Verificados", s.verified),
                ("Con errores", s.failed), ("En cuarentena", s.quarantined),
                ("Sin fecha de captura", s.unknown_date), ("Sin GPS", s.without_gps),
                ("Duración", f"{s.elapsed_s / 60:.1f} min")]
        while self.sum_grid.count():
            self.sum_grid.takeAt(0).widget().deleteLater()
        for i, (k, v) in enumerate(rows):
            self.sum_grid.addWidget(QLabel(k + ":"), i // 2, (i % 2) * 2)
            self.sum_grid.addWidget(QLabel(f"<b>{v}</b>"), i // 2, (i % 2) * 2 + 1)
        self.sum_errors.clear()
        for p, m in s.errors:
            self.sum_errors.addItem(f"{p.rsplit('/', 1)[-1]}: {m}")
        for wmsg in s.warnings:
            self.sum_errors.addItem(f"Aviso: {wmsg}")
        self.stack.setCurrentIndex(PAGE_SUMMARY)

    def _open_logs(self) -> None:
        if self._log_dir and os.path.isdir(self._log_dir):
            from iphonephotosync.transfer.platform_fs import open_folder
            open_folder(self._log_dir)

    def _home_again(self) -> None:
        self.gallery.stop()
        self.wf.plan = self.wf.report = None             # tras copiar, el plan quedó obsoleto: hay que reescanear
        self.stack.setCurrentIndex(PAGE_HOME)
        self.refresh_home()
        self.check_device()

    # ===================================================================== infraestructura
    def _run(self, t: Task) -> None:
        self._task = t
        t.finished.connect(t.deleteLater)
        t.start()

    def closeEvent(self, ev) -> None:
        self.gallery.stop()
        t = self._task
        if t is not None and t.isRunning():
            if self._control is not None and self.stack.currentIndex() == PAGE_BACKUP:
                if QMessageBox.question(self, "Respaldo en curso",
                                        "Hay un respaldo en curso. ¿Cancelar de forma segura y salir?\n"
                                        "Lo ya copiado se conserva y podrás reanudar.") != QMessageBox.Yes:
                    ev.ignore()
                    return
                self._control.cancel()
            t.wait(120_000)
        self.wf.disconnect()
        ev.accept()
