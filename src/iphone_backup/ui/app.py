"""Arranque de la interfaz.

    python -m iphone_backup.ui                      # iPhone real por USB
    python -m iphone_backup.ui --simulate CARPETA   # modo simulación con archivos de muestra (rotulado en pantalla)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="iphone_backup.ui")
    ap.add_argument("--simulate", metavar="CARPETA", help="Usa una carpeta con DCIM/ como iPhone simulado")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from iphone_backup.app.workflow import UserSettings, Workflow
    from iphone_backup.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    settings = UserSettings.load()
    sim = Path(args.simulate) if args.simulate else None
    win = MainWindow(Workflow(settings, simulate=sim), settings, simulated=sim is not None, save_settings=sim is None)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
