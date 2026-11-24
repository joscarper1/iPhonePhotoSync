"""Etapa 3 - Validación de metadatos contra el iPhone real (SOLO LECTURA, resultados AGREGADOS).

1. Sondea fecha/GPS/ContentIdentifier de TODA la biblioteca leyendo solo cabeceras (fotos) o ftyp+moov (vídeos).
2. Confirma los Live Photos por ContentIdentifier.
3. Compara el sondeo con la lectura del archivo COMPLETO en una muestra (equivalencia).
El informe no contiene nombres de archivo, fechas ni coordenadas: solo conteos.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from iphone_backup.device.afc_source import AfcDeviceSource
from iphone_backup.domain.filetypes import FileKind, classify
from iphone_backup.metadata.device_probe import probe_tags
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.metadata.live import confirm_pair
from iphone_backup.metadata.policy import interpret
from iphone_backup.ports.device import DeviceError, iter_chunks
from iphone_backup.scanner import scan


def key(m):
    return (m.capture_dt, m.date_source.value, m.tz_info, m.gps_lat, m.gps_lon, m.content_identifier)


def main(out: Path, sample_n: int, max_full_mb: int) -> int:
    rep: dict = {"photos": Counter(), "videos": Counter(), "errors": Counter(), "live": Counter(), "equivalence": Counter()}
    tmp = Path(tempfile.mkdtemp(prefix="probe_meta_"))
    months = set()
    try:
        with AfcDeviceSource().connect() as src, ExifToolService() as et:
            r = scan(src)
            t0 = time.perf_counter()
            cid: dict[str, str | None] = {}
            meta: dict[str, object] = {}
            for f in r.files:
                kind, _ = classify(f.ext)
                if kind not in (FileKind.PHOTO, FileKind.VIDEO):
                    continue
                bucket = rep["photos" if kind is FileKind.PHOTO else "videos"]
                try:
                    tags = probe_tags(src, f, et, tmp)
                    m = interpret(tags or {}, kind, mtime_ns=f.mtime_ns)
                except DeviceError as e:
                    rep["errors"][f"{kind.value}:{type(e).__name__}"] += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    rep["errors"][f"{kind.value}:{type(e).__name__}"] += 1
                    continue
                meta[f.path] = m
                cid[f.path] = m.content_identifier
                bucket["total"] += 1
                bucket[f"date:{m.date_source.value}"] += 1
                bucket["tz_declared" if m.tz_info else "tz_missing"] += 1
                bucket["gps" if m.has_gps else "no_gps"] += 1
                if m.gps_note:
                    bucket["gps_discarded"] += 1
                if m.capture_dt:
                    months.add(m.capture_dt[:7])
                if m.content_identifier:
                    bucket["has_content_identifier"] += 1
            rep["probe_seconds"] = round(time.perf_counter() - t0, 1)
            rep["distinct_year_months"] = len(months)
            rep["distinct_years"] = len({m[:4] for m in months})

            # Live Photos
            paired = set()
            for p in r.live_candidates:
                v = confirm_pair(cid.get(p.still.path), cid.get(p.motion.path))
                rep["live"]["confirmed" if v.confirmed else f"unconfirmed: {v.reason}"] += 1
                paired.update((p.still.path, p.motion.path))
            rep["live"]["stills_with_cid_but_no_name_pair"] = sum(
                1 for f in r.files if f.ext in (".heic", ".heif", ".jpg", ".jpeg") and cid.get(f.path) and f.path not in paired)
            rep["live"]["movs_with_cid_but_no_name_pair"] = sum(
                1 for f in r.files if f.ext == ".mov" and cid.get(f.path) and f.path not in paired)

            # Equivalencia sondeo vs archivo completo (muestra determinista)
            cands = [f for f in r.files if f.path in meta and f.size <= max_full_mb * 1024 * 1024]
            step = max(1, len(cands) // sample_n)
            for f in cands[::step][:sample_n]:
                kind, _ = classify(f.ext)
                dst = tmp / f"full{f.ext}"
                try:
                    with src.open_read(f.path) as h, open(dst, "wb") as o:
                        for c in iter_chunks(h):
                            o.write(c)
                    full = interpret(et.read(dst), kind, mtime_ns=f.mtime_ns)
                except Exception as e:  # noqa: BLE001
                    rep["equivalence"][f"error:{type(e).__name__}"] += 1
                    continue
                finally:
                    dst.unlink(missing_ok=True)
                same = key(full) == key(meta[f.path])
                rep["equivalence"][f"{f.ext}:{'igual' if same else 'DIFERENTE'}"] += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        rep["temp_removed"] = not tmp.exists()
    rep = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in rep.items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--max-full-mb", type=int, default=40)
    a = ap.parse_args()
    sys.exit(main(a.out, a.sample, a.max_full_mb))
