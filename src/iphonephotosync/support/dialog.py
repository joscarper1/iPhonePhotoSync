"""Diálogo «Apoyar este proyecto» (PySide6). Solo abre enlaces ya validados por ``build_model``; no hace nada más."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from iphonephotosync.support.config import Link, SupportModel
from iphonephotosync.support.providers import ONE_TIME, RECURRING
from iphonephotosync.support.styles import QSS


def _open_external(url: str) -> bool:
    return QDesktopServices.openUrl(QUrl(url))          # navegador del sistema; sin redirecciones propias


def _label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.PlainText)                       # los textos configurables nunca se interpretan como HTML
    if name:
        lb.setObjectName(name)
    return lb


class SupportDialog(QDialog):
    def __init__(self, model: SupportModel, parent: QWidget | None = None,
                 opener: Callable[[str], object] = _open_external):
        super().__init__(parent)
        self._model, self._opener = model, opener
        t, cfg = model.texts, model.config
        self.setObjectName("supportDialog")
        self.setWindowTitle(t["menu_item"])
        self.setModal(cfg.display_mode == "modal")
        self.setMinimumWidth(380)
        self.setMaximumWidth(560)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        head = QHBoxLayout()
        icon = _label("♡", "supportIcon", wrap=False)
        icon.setAccessibleName("")
        head.addWidget(icon)
        if cfg.project_name:
            head.addWidget(_label(cfg.project_name, "supportProject"), 1)
        else:
            head.addStretch(1)
        lay.addLayout(head)
        lay.addWidget(_label(model.heading, "supportTitle"))
        lay.addWidget(_label(model.body))
        self.buttons: list[QPushButton] = []
        sections = {ONE_TIME: t["one_time"], RECURRING: t["recurring"]}
        for kind in model.kinds:                                 # solo las modalidades con enlaces: sin huecos
            lay.addWidget(_label(sections[kind], "supportSection"))
            for link in model.links_for(kind):
                lay.addWidget(self._button(link, sections[kind]))
        lay.addWidget(_label(t["open_hint"].format(provider="/".join(dict.fromkeys(lk.provider.label for lk in model.links))),
                             "supportMuted"))
        if cfg.show_footer_message:
            lay.addWidget(_label(t["footer"]))
            lay.addWidget(_label(t["privacy"], "supportMuted"))
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.button(QDialogButtonBox.Close).setText(t["close"])
        box.rejected.connect(self.reject)                        # el botón y Escape cierran
        lay.addWidget(box)
        if self.buttons:
            self.buttons[0].setFocus()

    def _button(self, link: Link, section: str) -> QPushButton:
        p = link.provider
        b = QPushButton(f"{p.icon}  {p.label}" if self._model.config.show_provider_logos else p.label)
        b.setProperty("class", "provider")
        b.setAccessibleName(f"{section}: {p.label}")
        b.setToolTip(link.url)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _=False, u=link.url: self._opener(u))
        self.buttons.append(b)
        return b
