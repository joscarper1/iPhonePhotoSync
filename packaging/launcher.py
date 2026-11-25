"""Punto de entrada de la aplicación empaquetada (PyInstaller)."""
import sys


def _selfcheck() -> int:
    """Importa lo que necesita el acceso al iPhone; detecta módulos que faltan en el paquete."""
    try:
        from pymobiledevice3 import exceptions  # noqa: F401
        from pymobiledevice3.lockdown import create_using_usbmux  # noqa: F401
        from pymobiledevice3.services.afc import AfcService  # noqa: F401
        from iphonephotosync.device import afc_source  # noqa: F401
        from iphonephotosync.app import workflow, thumbnails  # noqa: F401
        import PIL, pillow_heif  # noqa: F401,E401
    except Exception as e:                      # noqa: BLE001
        print(f"SELFCHECK FALLO: {type(e).__name__}: {e}")
        return 1
    print("SELFCHECK OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    from iphonephotosync.ui.app import main
    sys.exit(main())
