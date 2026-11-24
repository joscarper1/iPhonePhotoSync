"""Descarga ExifTool (oficial) a ``tools/exiftool/`` para pruebas y empaquetado. No se sube al repositorio.

* Windows : ``exiftool.exe`` + ``exiftool_files/`` desde exiftool.org (la versión más reciente publicada).
* Linux/macOS: la distribución Perl oficial (``exiftool`` + ``lib/``) del espejo oficial en GitHub, versión fija.
  Requiere ``perl`` (viene con macOS y casi todas las distribuciones de Linux).

Uso:  python tools/fetch_exiftool.py
ExifTool: © Phil Harvey, licencia Artistic/GPL. https://exiftool.org
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PERL_VERSION = "13.64"                      # versión validada con un iPhone real
DEST = Path(__file__).resolve().parent / "exiftool"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "iphone-backup-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def windows() -> None:
    ver = get("https://exiftool.org/ver.txt").decode().strip()
    z = zipfile.ZipFile(io.BytesIO(get(f"https://exiftool.org/exiftool-{ver}_64.zip")))
    tmp = DEST.with_name("_exiftool_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    z.extractall(tmp)
    root = next(p for p in tmp.rglob("exiftool(-k).exe")).parent
    shutil.rmtree(DEST, ignore_errors=True)
    DEST.mkdir(parents=True)
    shutil.copy2(root / "exiftool(-k).exe", DEST / "exiftool.exe")
    if (root / "exiftool_files").is_dir():
        shutil.copytree(root / "exiftool_files", DEST / "exiftool_files")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"ExifTool {ver} (Windows) en {DEST}")


def posix() -> None:
    url = f"https://github.com/exiftool/exiftool/archive/refs/tags/{PERL_VERSION}.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(get(url)), mode="r:gz") as t:
        top = t.getnames()[0].split("/")[0]
        shutil.rmtree(DEST, ignore_errors=True)
        DEST.mkdir(parents=True)
        for m in t.getmembers():
            rel = m.name[len(top) + 1:]
            if rel == "exiftool" or rel.startswith("lib/"):
                m.name = rel
                t.extract(m, DEST, filter="data")
    exe = DEST / "exiftool"
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"ExifTool {PERL_VERSION} (Perl) en {DEST}")


if __name__ == "__main__":
    windows() if os.name == "nt" else posix()
    sys.exit(0)
