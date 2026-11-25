# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: se ejecuta desde la raíz del repositorio ->  pyinstaller packaging/iphonephotosync.spec
# Antes: python tools/fetch_exiftool.py  (ExifTool se incluye dentro de la aplicación)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], []
for pkg in ("pymobiledevice3", "pillow_heif"):                # importaciones dinámicas y bibliotecas nativas
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += [(str(ROOT / "src/iphonephotosync/data/geonames.sqlite"), "iphonephotosync/data")]
exif = ROOT / "tools/exiftool"
if exif.is_dir():
    datas += [(str(exif), "tools/exiftool")]
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "docs/THIRD_PARTY.md"), ".")]

a = Analysis(
    [str(ROOT / "packaging/launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Paquete ligero: sin ffmpeg (av), sin numpy (solo pruebas) ni la consola interactiva (jedi/IPython: 5.500 archivos)
    excludes=["tkinter", "pytest", "av", "numpy", "jedi", "parso", "IPython"],
    noarchive=False,
)


def _keep(entry):
    """Quita de Qt el renderizador de software (20 MB) y las traducciones que no son del español."""
    name = entry[0].replace("\\", "/").lower()
    if name.endswith("opengl32sw.dll"):
        return False
    if "/translations/" in name and "_es" not in name:
        return False
    return True


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="iphonephotosync",
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="iphonephotosync")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="iPhonePhotoSync.app", bundle_identifier="io.github.joscarper1.iphonephotosync",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.1.0"})
