"""Informe de seguridad para liberar espacio en el iPhone: ¿qué NO está a salvo?

SOLO LECTURA: no borra ni modifica nada (ni del iPhone ni del destino). El usuario borra desde la app Fotos.

Enfoque por COMPLEMENTO: se enumera lo que NO está respaldado de forma segura (las excepciones); lo demás, visto en el
iPhone en el momento del informe, sí lo está. Salvaguardas:
  * El informe vale solo para lo que existía en el iPhone en ``generated_at``. Lo posterior NO está cubierto.
  * Comprobación de cuadre: seguros + excepciones == total de fotos/vídeos visto. Si no cuadra, ``consistent`` es False.
  * Un elemento es seguro solo si: está COMMITTED con verificación completa y su copia en el disco sigue íntegra
    (SHA-256 releído), o ya existía idéntico en la biblioteca del usuario (el archivo sigue ahí con el mismo
    tamaño/fecha que cuando se hasheó), o es un duplicado de contenido de algo respaldado.
  * Excepciones adicionales: foto/vídeo con .AAE (editado: el original sin editar NO se respalda), pareja Live Photo
    incompleta, archivo nuevo o cambiado desde el respaldo.
"""
from __future__ import annotations

import csv
import os
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from iphonephotosync.database.manifest import Manifest
from iphonephotosync.domain.filetypes import FileKind, classify
from iphonephotosync.domain.models import DeviceFile, ItemStatus, VerifyStatus
from iphonephotosync.organizer.naming import UnsafePath, safe_join
from iphonephotosync.ports.device import DeviceSource
from iphonephotosync.scanner import scan
from iphonephotosync.transfer.destination import Destination
from iphonephotosync.transfer.engine import EXTERNAL_PREFIX, _sha256_file
from iphonephotosync.transfer.fsops import RealFs

REASONS = {
    "NOT_COPIED": "No se ha copiado (pendiente o no procesado)",
    "FAILED": "La copia falló",
    "QUARANTINED": "En cuarentena: el hash no coincidió",
    "CHANGED_OR_NEW": "Es nuevo o cambió después del respaldo",
    "EDITED": "Editado: el original sin editar NO está respaldado",
    "PAIR_INCOMPLETE": "Su pareja Live Photo no está a salvo",
    "DEST_MISSING": "La copia en el disco falta",
    "DEST_CORRUPT": "La copia en el disco no es íntegra",
    "EXISTING_GONE": "El archivo equivalente de tu biblioteca ya no está o cambió",
    "PARTIAL_VERIFY": "Verificación parcial (sin hash del iPhone)",
}


@dataclass(frozen=True)
class Exc:
    path: str
    name: str
    kind: str
    capture: str
    size: int
    reason: str


@dataclass
class SafetyReport:
    generated_at: str
    udid: str
    total_media: int = 0
    safe: int = 0
    safe_bytes: int = 0
    exceptions: list[Exc] = field(default_factory=list)
    ignored_system_files: int = 0
    newest_covered: str | None = None

    @property
    def consistent(self) -> bool:
        return self.safe + len(self.exceptions) == self.total_media

    @property
    def exception_bytes(self) -> int:
        return sum(e.size for e in self.exceptions)

    def by_reason(self) -> Counter:
        return Counter(e.reason for e in self.exceptions)


def _local_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


def build_safety_report(source: DeviceSource, dest: Destination, manifest: Manifest, *,
                        recheck_disk: bool = True, fs=None,
                        progress: Callable[[int, int], None] | None = None) -> SafetyReport:
    fs = fs or RealFs()
    info = source.info()
    result = scan(source)
    rep = SafetyReport(datetime.now(timezone.utc).isoformat(timespec="seconds"), info.udid)
    by_path = {f.path: f for f in result.files}

    # ediciones: foto/vídeo con .AAE del mismo nombre base en la misma carpeta
    aae_keys = {(f.folder, posixpath.splitext(f.name)[0].lower()) for f in result.files if f.ext == ".aae"}

    media = [f for f in result.files if result.kinds[f.path] in (FileKind.PHOTO, FileKind.VIDEO)]
    rep.total_media = len(media)
    rep.ignored_system_files = sum(1 for f in result.files if result.kinds[f.path] is FileKind.IGNORED)

    verified_cache: dict[str, bool] = {}

    def disk_ok(rel: str, size: int, sha256: str) -> str | None:
        """None si la copia del disco está íntegra; si no, el código de motivo."""
        if rel in verified_cache:
            return None if verified_cache[rel] else "DEST_CORRUPT"
        try:
            target = safe_join(dest.library, rel.split("/"))
        except UnsafePath:
            return "DEST_MISSING"
        if not fs.exists(target):
            return "DEST_MISSING"
        if fs.getsize(target) != size:
            verified_cache[rel] = False
            return "DEST_CORRUPT"
        if recheck_disk:
            digest, _ = _sha256_file(fs, target, 4 * 1024 * 1024)
            ok = digest == sha256
        else:
            ok = True
        verified_cache[rel] = ok
        return None if ok else "DEST_CORRUPT"

    reason_of: dict[str, str] = {}      # ruta -> código de excepción
    for i, f in enumerate(media, 1):
        reason_of_f = _classify(f, info.udid, manifest, dest, disk_ok, fs)
        if reason_of_f is None and (f.folder, posixpath.splitext(f.name)[0].lower()) in aae_keys:
            reason_of_f = "EDITED"
        if reason_of_f:
            reason_of[f.path] = reason_of_f
        if progress and (i % 100 == 0 or i == len(media)):
            progress(i, len(media))

    # integridad de parejas Live Photo: si una mitad no está a salvo, la otra tampoco
    for pair in result.live_candidates:
        a, b = reason_of.get(pair.still.path), reason_of.get(pair.motion.path)
        if a and not b:
            reason_of[pair.motion.path] = "PAIR_INCOMPLETE"
        elif b and not a:
            reason_of[pair.still.path] = "PAIR_INCOMPLETE"

    for f in media:
        code = reason_of.get(f.path)
        if code is None:
            rep.safe += 1
            rep.safe_bytes += f.size
            when = _local_iso(f.mtime_ns)
            rep.newest_covered = max(rep.newest_covered or when, when)
        else:
            row = manifest.find_by_fingerprint(info.udid, f.path, f.size, f.mtime_ns)
            cap = (row["capture_dt"] if row and row["capture_dt"] else None)
            rep.exceptions.append(Exc(f.path, f.name, classify(f.ext)[0].value,
                                      cap.replace("T", " ") if cap else _local_iso(f.mtime_ns) + " (archivo)",
                                      f.size, code))
    rep.exceptions.sort(key=lambda e: (e.reason, e.capture, e.name))
    return rep


def _classify(f: DeviceFile, udid: str, manifest: Manifest, dest: Destination, disk_ok, fs) -> str | None:
    row = manifest.find_by_fingerprint(udid, f.path, f.size, f.mtime_ns)
    if row is None:
        return "CHANGED_OR_NEW"
    st = row["copy_status"]
    if st == ItemStatus.FAILED.value:
        return "FAILED"
    if st == ItemStatus.QUARANTINED.value:
        return "QUARANTINED"
    if st == ItemStatus.COMMITTED.value:
        if row["verify_status"] != VerifyStatus.VERIFIED_FULL.value:
            return "PARTIAL_VERIFY"
        return disk_ok(row["rel_path"], row["size"], row["sha256"])
    if st == ItemStatus.SKIPPED_DUP.value:
        rel = row["rel_path"] or ""
        if rel.startswith(EXTERNAL_PREFIX):
            return _existing_ok(rel[len(EXTERNAL_PREFIX):], f, dest, manifest, fs)
        target = manifest._conn.execute(
            "SELECT size, sha256 FROM items WHERE rel_path=? AND copy_status='committed' LIMIT 1", (rel,)).fetchone()
        if target is None:
            return "NOT_COPIED"
        return disk_ok(rel, target["size"], target["sha256"])
    return "NOT_COPIED"


def _existing_ok(rel: str, f: DeviceFile, dest: Destination, manifest: Manifest, fs) -> str | None:
    """El archivo equivalente de la biblioteca del usuario debe seguir ahí con el mismo tamaño y fecha con
    los que se hasheó (la caché del manifiesto), y coincidir en tamaño con el del iPhone."""
    p = dest.base.joinpath(*rel.split("/"))
    try:
        st = os.stat(p)
    except OSError:
        return "EXISTING_GONE"
    if st.st_size != f.size or manifest.external_lookup(rel, st.st_size, st.st_mtime_ns) is None:
        return "EXISTING_GONE"
    return None


def write_report(rep: SafetyReport, out_dir: Path) -> tuple[Path, Path]:
    """Escribe un CSV con las excepciones y un resumen de texto. Devuelve (csv, txt)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path, txt_path = out_dir / f"no_borrar_{stamp}.csv", out_dir / f"resumen_liberar_espacio_{stamp}.txt"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:       # utf-8-sig: Excel abre bien los acentos
        w = csv.writer(fh)
        w.writerow(["archivo", "tipo", "fecha_captura", "tamano_bytes", "motivo"])
        for e in rep.exceptions:
            w.writerow([e.name, e.kind, e.capture, e.size, REASONS[e.reason]])
    gb = lambda n: f"{n / 1e9:.2f} GB"
    lines = [
        "INFORME PARA LIBERAR ESPACIO EN EL iPHONE (solo lectura: esta herramienta no borra nada)",
        f"Generado: {rep.generated_at} (UTC)",
        "",
        f"Fotos y vídeos vistos en el iPhone: {rep.total_media}",
        f"  A salvo (respaldados y verificados): {rep.safe}  ({gb(rep.safe_bytes)})",
        f"  NO están a salvo / revisar antes de borrar: {len(rep.exceptions)}  ({gb(rep.exception_bytes)})",
        f"Cuadre (a salvo + excepciones = total): {'OK' if rep.consistent else 'NO CUADRA: NO uses este informe'}",
        "",
        "Excepciones por motivo:",
        *[f"  {n:5}  {REASONS[r]}" for r, n in rep.by_reason().most_common()],
        "",
        "CÓMO USARLO",
        f"  1. SOLO cubre lo que existía en el iPhone al generar el informe. NO borres nada tomado o recibido después.",
        f"     (La foto más reciente cubierta tiene fecha de archivo {rep.newest_covered}.)",
        "  2. Todo lo que NO figure en el CSV 'no_borrar' (de lo anterior a esa fecha) está a salvo.",
        "  3. Las fotos del CSV se ubican en la app Fotos por fecha/hora (y por el nombre IMG_xxxx en 'Info').",
        "     Las EDITADAS se ven con el filtro 'Editadas' de la biblioteca.",
        "  4. Borra desde la app Fotos (van a 'Eliminados recientemente' 30 días). Después ejecuta de nuevo el",
        "     comando 'verify' para confirmar que tu respaldo sigue íntegro.",
        "  5. Recomendación: antes de borrar, ten una SEGUNDA copia del HDD (un solo disco puede fallar).",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, txt_path
