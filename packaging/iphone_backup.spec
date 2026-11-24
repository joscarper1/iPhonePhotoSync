# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: se ejecuta desde la raíz del repositorio ->  pyinstaller packaging/iphone_backup.spec
# Antes: python tools/fetch_exiftool.py  (ExifTool se incluye dentro de la aplicación)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], []
for pkg in ("pymobiledevice3", "pillow_heif", "av"):          # importaciones dinámicas y bibliotecas nativas
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += [(str(ROOT / "src/iphone_backup/data/geonames.sqlite"), "iphone_backup/data")]
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
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="iphone-backup",
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="iphone-backup")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Respaldo de iPhone.app", bundle_identifier="org.iphonebackup.app",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.1.0"})
