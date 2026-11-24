"""Punto de entrada de la aplicación empaquetada (PyInstaller)."""
import sys

from iphone_backup.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
