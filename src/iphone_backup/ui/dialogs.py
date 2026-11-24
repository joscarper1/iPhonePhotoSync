"""Diálogos: configuración e historial."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from iphone_backup.app.workflow import LOG_LEVELS, TEMPLATES_UI, UserSettings
from iphone_backup.ui.progress_model import fmt_bytes

DETAIL = [("País", "COUNTRY"), ("Región", "REGION"), ("Ciudad", "CITY")]


class SettingsDialog(QDialog):
    """Solo opciones que el programa realmente aplica. Lo no configurable se informa, no se simula."""

    def __init__(self, settings: UserSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración")
        self.setMinimumWidth(520)
        self._s = settings
        form = QFormLayout()
        self.dest = QLineEdit(settings.dest)
        pick = QPushButton("Elegir…")
        pick.clicked.connect(self._pick)
        row = QHBoxLayout()
        row.addWidget(self.dest)
        row.addWidget(pick)
        form.addRow("Carpeta raíz de destino", row)
        self.template = QComboBox()
        self.template.addItems(TEMPLATES_UI)
        self.template.setCurrentText(settings.template)
        form.addRow("Plantilla de carpetas", self.template)
        self.gps = QCheckBox("Organizar por ubicación GPS")
        self.gps.setChecked(settings.use_gps)
        form.addRow("", self.gps)
        self.by_album = QCheckBox("Usar el álbum del iPhone como carpeta raíz (Álbum > Año > Mes > Lugar)")
        self.by_album.setChecked(settings.by_album)
        self.by_album.setToolTip("Lo que está en un álbum creado por ti se guarda dentro de la carpeta de ese álbum. "
                                 "Si está en varios álbumes se guarda una sola copia, en el primero por orden alfabético. "
                                 "Lo que no está en ningún álbum no lleva carpeta raíz. Los favoritos se anotan en el registro.")
        form.addRow("", self.by_album)
        self.detail = QComboBox()
        for label, key in DETAIL:
            self.detail.addItem(label, key)
        self.detail.setCurrentIndex(max(0, [k for _, k in DETAIL].index(settings.max_detail)))
        form.addRow("Nivel de geolocalización", self.detail)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(0, 200)
        self.radius.setSuffix(" km")
        self.radius.setValue(settings.merge_radius_km)
        form.addRow("Agrupar ciudades cercanas hasta", self.radius)
        self.photos = QCheckBox("Fotografías (HEIC, JPEG, PNG)")
        self.photos.setChecked(settings.include_photos)
        self.videos = QCheckBox("Vídeos (MOV, MP4)")
        self.videos.setChecked(settings.include_videos)
        form.addRow("Incluir", self.photos)
        form.addRow("", self.videos)
        self.logs = QComboBox()
        self.logs.addItems(LOG_LEVELS)
        self.logs.setCurrentText(settings.log_level)
        form.addRow("Detalle de registros", self.logs)
        self.workers = QSpinBox()
        self.workers.setRange(1, 6)
        self.workers.setValue(settings.workers)
        form.addRow("Archivos en paralelo", self.workers)
        self.stale = QSpinBox()
        self.stale.setRange(1, 24 * 30)
        self.stale.setSuffix(" h")
        self.stale.setValue(settings.full_scan_after_hours)
        form.addRow("Sugerir escaneo completo después de", self.stale)
        fixed = QLabel(
            "Siempre activos (no se pueden desactivar, protegen tus datos):\n"
            "• Verificación SHA-256 de cada copia.\n"
            "• Nunca se sobrescribe un archivo existente; los duplicados se omiten.\n"
            "• Se conservan los nombres originales.\n"
            "• La geocodificación es local y sin Internet (GeoNames, CC-BY 4.0).\n"
            "• El iPhone solo se lee: nunca se modifica ni se borra nada en él.")
        fixed.setWordWrap(True)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(fixed)
        lay.addWidget(box)

    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Carpeta de destino del respaldo", self.dest.text() or str(Path.home()))
        if d:
            self.dest.setText(d)

    def result_settings(self) -> UserSettings:
        s = self._s
        return UserSettings(dest=self.dest.text().strip(), template=self.template.currentText(),
                            use_gps=self.gps.isChecked(), max_detail=self.detail.currentData(),
                            merge_radius_km=self.radius.value(), include_photos=self.photos.isChecked(),
                            include_videos=self.videos.isChecked(), log_level=self.logs.currentText(),
                            workers=self.workers.value(), full_scan_after_hours=self.stale.value(),
                            by_album=self.by_album.isChecked())


class HistoryDialog(QDialog):
    def __init__(self, rows: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Historial de respaldos")
        self.resize(720, 360)
        t = QTableWidget(len(rows), 6)
        t.setHorizontalHeaderLabels(["Sesión", "Inicio", "Fin", "Estado", "Copiados", "Con errores"])
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        names = {"completed": "Completada", "running": "En curso", "interrupted": "Interrumpida",
                 "cancelled": "Cancelada", "failed": "Con fallo"}
        for i, r in enumerate(rows):
            vals = [str(r["id"]), (r["started"] or "")[:19].replace("T", " "), (r["finished"] or "—")[:19].replace("T", " "),
                    names.get(r["status"], r["status"]), f'{r["copied"]} ({fmt_bytes(r["bytes"])})', str(r["failed"])]
            for j, v in enumerate(vals):
                t.setItem(i, j, QTableWidgetItem(v))
        t.horizontalHeader().setStretchLastSection(True)
        t.resizeColumnsToContents()
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(t if rows else QLabel("Todavía no hay respaldos en este destino."))
        lay.addWidget(box)
