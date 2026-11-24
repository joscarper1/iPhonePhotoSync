"""Etapa 0 - Sonda de viabilidad del iPhone (SOLO LECTURA).

Qué hace:
  * Detecta el iPhone por USB (usbmuxd / Apple Mobile Device Service).
  * Enumera /DCIM por AFC y resume: extensiones, carpetas, parejas Live Photo
    (por nombre base), fotos editadas (IMG_E*) y sidecars .AAE.
  * Comprueba si /PhotoData existe (solo listdir).
  * Lee en memoria unas pocas muestras (no escribe fotos en disco), calcula
    SHA-256 y SHA-1 en streaming, compara el SHA-1 con el que calcula el propio
    iPhone (AFC get_file_hash) y mide la velocidad.
  * Comprueba que se puede leer el final de un MOV (para localizar el atom moov).
  * Compara el listado de DCIM antes y después para demostrar que nada cambió.

Garantías:
  * Todo el acceso al iPhone pasa por ``ReadOnlyAfc``, que SOLO expone
    listdir/stat/fopen('r')/pread/fclose/get_file_hash. No existen métodos de
    escritura, borrado ni renombrado.
  * El informe NO contiene nombres de archivo, UDID, nombre del dispositivo
    ni coordenadas: solo estadísticas.

Uso:
    python probe_device.py --out ..\\docs\\probe_result.json [--max-sample-mb 150]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import posixpath
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

CHUNK = 4 * 1024 * 1024


class ReadOnlyAfc:
    """Fachada de solo lectura sobre AfcService. No expone ninguna operación de escritura."""

    def __init__(self, afc):
        self._afc = afc

    async def listdir(self, path):
        return await self._afc.listdir(path)

    async def stat(self, path):
        return await self._afc.stat(path)

    async def open_read(self, path):
        return await self._afc.fopen(path, "r")

    async def pread(self, handle, size, offset):
        return await self._afc.pread(handle, size, offset)

    async def close(self, handle):
        await self._afc.fclose(handle)

    async def device_sha1(self, path):
        return await self._afc.get_file_hash(path)


async def list_dcim(ro: ReadOnlyAfc):
    """Devuelve lista de (ruta, stat) de todos los archivos en /DCIM."""
    items = []
    folders = await ro.listdir("/DCIM")
    for folder in sorted(folders):
        fpath = posixpath.join("/DCIM", folder)
        try:
            st = await ro.stat(fpath)
        except Exception:
            continue
        if st.get("st_ifmt") != "S_IFDIR":
            continue
        for name in sorted(await ro.listdir(fpath)):
            p = posixpath.join(fpath, name)
            try:
                items.append((p, await ro.stat(p)))
            except Exception as e:  # noqa: BLE001
                items.append((p, {"error": str(e)}))
    return items


def snapshot_digest(items) -> str:
    h = hashlib.sha256()
    for p, st in items:
        h.update(f"{p}|{st.get('st_size')}|{st.get('st_mtime')}\n".encode())
    return h.hexdigest()


async def stream_hash(ro: ReadOnlyAfc, path: str, size: int):
    """Lee el archivo completo en streaming. Devuelve (sha256, sha1, segundos)."""
    s256, s1 = hashlib.sha256(), hashlib.sha1()
    h = await ro.open_read(path)
    t0 = time.perf_counter()
    try:
        off = 0
        while off < size:
            chunk = await ro.pread(h, min(CHUNK, size - off), off)
            if not chunk:
                raise IOError(f"lectura corta en offset {off} de {size}")
            s256.update(chunk)
            s1.update(chunk)
            off += len(chunk)
    finally:
        await ro.close(h)
    return s256.hexdigest(), s1.hexdigest(), time.perf_counter() - t0


async def read_tail(ro: ReadOnlyAfc, path: str, size: int, n: int = 65536) -> int:
    h = await ro.open_read(path)
    try:
        data = await ro.pread(h, n, max(0, size - n))
        return len(data)
    finally:
        await ro.close(h)


def pick(items, pred):
    for p, st in items:
        if "error" not in st and pred(p, st):
            return p, st
    return None


async def sample(ro, label, found, max_bytes, report):
    entry = {"label": label, "found": found is not None}
    report["samples"].append(entry)
    if not found:
        return
    path, st = found
    size = int(st["st_size"])
    entry["ext"] = posixpath.splitext(path)[1].lower()
    entry["size_bytes"] = size
    if size > max_bytes:
        entry["skipped"] = f"mayor que --max-sample-mb ({size} bytes)"
        return
    try:
        a256, a1, ta = await stream_hash(ro, path, size)
        b256, b1, tb = await stream_hash(ro, path, size)
        dev1 = (await ro.device_sha1(path)).hex()
        entry.update(
            {
                "read_twice_identical_sha256": a256 == b256,
                "device_sha1_matches_streamed_sha1": dev1 == a1,
                "mb_per_s_first": round(size / 1e6 / ta, 2) if ta > 0 else None,
                "mb_per_s_second": round(size / 1e6 / tb, 2) if tb > 0 else None,
            }
        )
        if entry["ext"] in (".mov", ".mp4"):
            entry["tail_read_bytes"] = await read_tail(ro, path, size)
    except Exception as e:  # noqa: BLE001
        entry["error"] = f"{type(e).__name__}: {e}"


async def main(args) -> int:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    report: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "device": {},
        "dcim": {},
        "samples": [],
        "notes": [],
    }

    devices = await usbmux.list_devices()
    report["device"]["usbmux_devices_found"] = len(devices)
    if not devices:
        report["notes"].append("No se detectó ningún dispositivo por usbmux (¿cable, confianza, Apple Devices?).")
        _write(args.out, report)
        print("NO HAY DISPOSITIVO. Revisa cable, desbloqueo y 'Confiar'.")
        return 2

    lockdown = await create_using_usbmux(autopair=True, pair_timeout=60)
    async with lockdown:
        info = lockdown.all_values if hasattr(lockdown, "all_values") else {}
        report["device"].update(
            {
                "product_type": info.get("ProductType"),
                "ios_version": info.get("ProductVersion"),
                "device_class": info.get("DeviceClass"),
                "passcode_protected": info.get("PasswordProtected"),
            }
        )
        async with AfcService(lockdown) as afc:
            ro = ReadOnlyAfc(afc)
            t0 = time.perf_counter()
            before = await list_dcim(ro)
            report["dcim"]["enumeration_seconds"] = round(time.perf_counter() - t0, 2)
            report["dcim"]["folders"] = len({posixpath.dirname(p) for p, _ in before})
            report["dcim"]["entries"] = len(before)
            report["dcim"]["stat_errors"] = sum(1 for _, st in before if "error" in st)

            ext = Counter(posixpath.splitext(p)[1].lower() for p, _ in before)
            report["dcim"]["by_extension"] = dict(ext.most_common())
            total = sum(int(st.get("st_size", 0)) for _, st in before if "error" not in st)
            report["dcim"]["total_bytes"] = total

            by_dir_base = defaultdict(set)
            for p, st in before:
                d, f = posixpath.split(p)
                base, e = posixpath.splitext(f)
                by_dir_base[(d, base)].add(e.lower())
            stills = {".heic", ".jpg", ".jpeg"}
            live = sum(1 for exts in by_dir_base.values() if ".mov" in exts and exts & stills)
            report["dcim"]["name_pairs_still_plus_mov"] = live
            report["dcim"]["edited_candidates_IMG_E"] = sum(
                1 for p, _ in before if re.match(r"IMG_E\d+", posixpath.basename(p), re.I)
            )
            report["dcim"]["aae_sidecars"] = ext.get(".aae", 0)

            has_birth = sum(1 for _, st in before if "st_birthtime" in st)
            report["dcim"]["entries_with_birthtime"] = has_birth

            try:
                root = await ro.listdir("/")
                report["device"]["afc_root_entries"] = sorted(root)
                report["device"]["photodata_visible"] = "PhotoData" in root
                if "PhotoData" in root:
                    report["device"]["photodata_entries"] = sorted(await ro.listdir("/PhotoData"))[:40]
            except Exception as e:  # noqa: BLE001
                report["notes"].append(f"No se pudo listar la raíz AFC: {e}")

            max_bytes = args.max_sample_mb * 1024 * 1024
            heic = pick(before, lambda p, s: p.lower().endswith(".heic"))
            jpg = pick(before, lambda p, s: p.lower().endswith((".jpg", ".jpeg")))
            png = pick(before, lambda p, s: p.lower().endswith(".png"))
            mov = pick(before, lambda p, s: p.lower().endswith(".mov"))
            mp4 = pick(before, lambda p, s: p.lower().endswith(".mp4"))
            edited = pick(before, lambda p, s: re.match(r"IMG_E\d+", posixpath.basename(p), re.I) is not None)
            for label, found in [
                ("HEIC", heic), ("JPEG", jpg), ("PNG (captura?)", png),
                ("MOV", mov), ("MP4", mp4), ("Editada IMG_E*", edited),
            ]:
                await sample(ro, label, found, max_bytes, report)

            after = await list_dcim(ro)
            report["dcim"]["listing_digest_unchanged_after_probe"] = snapshot_digest(before) == snapshot_digest(after)
            report["dcim"]["entries_after"] = len(after)

    _write(args.out, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _write(path, report):
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-sample-mb", type=int, default=150)
    sys.exit(asyncio.run(main(ap.parse_args())))
