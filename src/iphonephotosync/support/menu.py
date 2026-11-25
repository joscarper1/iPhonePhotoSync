"""Integración con una ventana principal Qt: añade Ayuda > «Apoyar este proyecto» solo si hay algo que mostrar."""
from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow

from iphonephotosync.support.config import SupportConfig, build_model


def install_support_menu(window: QMainWindow, config: SupportConfig) -> QAction | None:
    """Devuelve la acción creada o ``None`` (y no toca la ventana) si está deshabilitado o sin proveedores válidos."""
    model = build_model(config)
    if not model.available:
        return None
    from iphonephotosync.support.dialog import SupportDialog

    action = QAction(model.texts["menu_item"], window)
    action.setObjectName("actionSupport")
    window._support_dialog = None

    def show() -> None:
        dlg = SupportDialog(model, window)
        if dlg.isModal():
            dlg.exec()
        else:
            window._support_dialog = dlg                        # referencia viva mientras la ventana no es modal
            dlg.setAttribute(dlg.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()

    action.triggered.connect(show)
    window.menuBar().addMenu(model.texts["menu_help"]).addAction(action)
    return action
