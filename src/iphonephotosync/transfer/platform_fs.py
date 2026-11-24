"""Detalles del sistema operativo aislados en un solo lugar (Windows, Linux y macOS).

Windows es la plataforma verificada con un iPhone real; Linux y macOS usan los mismos caminos de código y se
comprueban en la integración continua (ver README). Todo aquí es de SOLO LECTURA o de nombres/rutas.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# nombre normalizado que entiende space.FS_MAX_FILE (límite de tamaño por archivo)
_FS_ALIASES = {"vfat": "FAT32", "msdos": "FAT32", "fat32": "FAT32", "fat": "FAT32", "exfat": "EXFAT",
               "ntfs": "NTFS", "ntfs3": "NTFS", "apfs": "APFS", "hfs": "HFS+", "ext4": "EXT4", "btrfs": "BTRFS",
               "xfs": "XFS"}


def _normalize(fs: str) -> str:
    return _FS_ALIASES.get(fs.strip().lower(), fs.strip().upper())


def _mounts_linux() -> list[tuple[str, str]]:
    """[(punto de montaje, tipo)] de /proc/self/mountinfo."""
    out = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                parts, rparts = left.split(), right.split()
                if len(parts) > 4 and rparts:
                    out.append((parts[4].replace("\\040", " "), rparts[0]))
    except OSError:
        pass
    return out


def _mounts_mac() -> list[tuple[str, str]]:
    """[(punto de montaje, tipo)] de la salida de ``mount``: ``/dev/disk4s1 on /Volumes/X (exfat, local, ...)``."""
    out = []
    try:
        text = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        if " on " in line and line.rstrip().endswith(")") and " (" in line:
            mp = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
            fs = line.rsplit(" (", 1)[1].split(",")[0]
            out.append((mp, fs))
    return out


def posix_filesystem_name(path: Path | str) -> str | None:
    """Tipo de sistema de archivos del volumen que contiene ``path`` en Linux/macOS; None si no se sabe."""
    mounts = _mounts_linux() if IS_LINUX else _mounts_mac() if IS_MAC else []
    target = os.path.realpath(str(path))
    best: tuple[int, str] | None = None
    for mp, fs in mounts:
        if target == mp or target.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > best[0]:
                best = (len(mp), fs)
    return _normalize(best[1]) if best else None


def candidate_roots() -> list[Path]:
    """Raíces donde buscar un disco de respaldo conectado (para reencontrarlo aunque cambie de letra o de punto)."""
    if IS_WINDOWS:
        return [Path(f"{c}:\\") for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    pool = [Path("/Volumes")] if IS_MAC else [Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")]
    roots: list[Path] = []
    for base in pool:
        try:
            if base.is_dir():
                roots += [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            pass
    return roots


def open_folder(path: str) -> bool:
    """Abre una carpeta en el explorador del sistema. Devuelve False si no se pudo."""
    try:
        if IS_WINDOWS:
            os.startfile(path)  # noqa: S606 - carpeta propia del usuario
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        return False


def device_not_found_hint() -> str:
    if IS_WINDOWS:
        return "No se detecta el iPhone. Revisa el cable USB y que Apple Devices (o iTunes) esté instalado."
    if IS_MAC:
        return "No se detecta el iPhone. Revisa el cable USB y desbloquéalo; si pregunta, pulsa 'Confiar'."
    return ("No se detecta el iPhone. Revisa el cable USB y que el servicio usbmuxd esté instalado y en marcha "
            "(paquete 'usbmuxd'); desbloquea el iPhone y pulsa 'Confiar'.")
