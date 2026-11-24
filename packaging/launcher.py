"""Punto de entrada de la aplicación empaquetada (PyInstaller)."""
import sys

from iphonephotosync.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
