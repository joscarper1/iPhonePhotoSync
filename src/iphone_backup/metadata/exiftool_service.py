"""Servicio ExifTool persistente (``-stay_open``): un único proceso para miles de archivos.

Solo LEE metadatos de copias locales (en staging); nunca escribe en los archivos
ni toca el iPhone. Solo se piden las etiquetas necesarias.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

# Etiquetas solicitadas (nombres sin grupo; la salida usa -G1 para distinguir origen).
REQUESTED_TAGS = [
    "DateTimeOriginal", "OffsetTimeOriginal", "SubSecTimeOriginal", "CreateDate", "OffsetTimeDigitized",
    "CreationDate",
    "GPSLatitude", "GPSLongitude", "GPSCoordinates", "GPSPosition",
    "ContentIdentifier", "Make", "Model",
]

_READY = b"{ready}"


class ExifToolError(Exception):
    pass


def default_exiftool_path() -> Path:
    """Orden de búsqueda: variable IPHONE_BACKUP_EXIFTOOL; la copia incluida en la app empaquetada; ``tools/exiftool``
    junto al proyecto; el ``exiftool`` del PATH (Linux/macOS: ``apt install libimage-exiftool-perl`` / ``brew install
    exiftool``). Si no aparece ninguno, devuelve la ruta del proyecto (el error dirá dónde se buscó)."""
    import shutil
    import sys
    env = os.environ.get("IPHONE_BACKUP_EXIFTOOL")
    if env:
        return Path(env)
    name = "exiftool.exe" if os.name == "nt" else "exiftool"
    bundled = getattr(sys, "_MEIPASS", None)
    candidates = []
    if bundled:
        candidates.append(Path(bundled) / "tools" / "exiftool" / name)
    candidates.append(Path(__file__).resolve().parents[3] / "tools" / "exiftool" / name)
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("exiftool")
    return Path(found) if found else candidates[-1]


class ExifToolService:
    def __init__(self, exe: Path | str | None = None, timeout: float = 60.0):
        self.exe = Path(exe) if exe else default_exiftool_path()
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> "ExifToolService":
        if not self.exe.is_file():
            raise ExifToolError(f"No se encontró ExifTool en {self.exe}. Instálalo (Linux: libimage-exiftool-perl; "
                                "macOS: brew install exiftool) o indica su ruta en IPHONE_BACKUP_EXIFTOOL.")
        self._proc = subprocess.Popen(
            [str(self.exe), "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            try:
                p.stdin.write(b"-stay_open\nFalse\n")
                p.stdin.flush()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                p.kill()
        if p:
            for s in (p.stdin, p.stdout):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass

    def __enter__(self) -> "ExifToolService":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def _execute(self, args: list[str]) -> tuple[bytes, bytes]:
        if not self._proc or self._proc.poll() is not None:
            raise ExifToolError("ExifTool no está en ejecución")
        payload = "\n".join(args + ["-execute", ""]).encode("utf-8")
        result: dict = {}

        def run():
            try:
                with self._lock:
                    self._proc.stdin.write(payload)
                    self._proc.stdin.flush()
                    out = bytearray()
                    while True:
                        line = self._proc.stdout.readline()
                        if not line:
                            raise ExifToolError("ExifTool terminó inesperadamente")
                        out += line
                        if line.strip() == _READY:
                            break
                    result["out"] = bytes(out[: -len(line)])
            except BaseException as e:  # noqa: BLE001
                result["err"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            self._proc.kill()
            raise ExifToolError(f"ExifTool no respondió en {self.timeout:.0f} s")
        if "err" in result:
            raise ExifToolError(str(result["err"]))
        return result["out"], b""

    def read(self, path: Path | str) -> dict[str, object]:
        """Etiquetas relevantes de un archivo local. Claves con grupo (p. ej. ``ExifIFD:DateTimeOriginal``).
        Valores numéricos sin formato (-n): coordenadas decimales, fechas crudas."""
        args = ["-json", "-G1", "-n", "-a", "-charset", "filename=utf8", "-api", "largefilesupport=1"]
        args += [f"-{t}" for t in REQUESTED_TAGS]
        args.append(str(path))
        out, _ = self._execute(args)
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            raise ExifToolError(f"Sin salida de ExifTool para el archivo")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ExifToolError(f"Salida de ExifTool inválida: {e}") from e
        tags = data[0] if data else {}
        tags.pop("SourceFile", None)
        return tags
