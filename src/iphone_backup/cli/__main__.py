"""CLI. Por ahora: ``scan`` (solo lectura, no copia nada).

    python -m iphone_backup.cli scan [--simulate CARPETA] [--json salida.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from iphone_backup.ports.device import DeviceError, DeviceLocked, DeviceNotFound, TrustRequired
from iphone_backup.scanner import scan

from iphone_backup.transfer.platform_fs import device_not_found_hint

HINTS = {
    DeviceNotFound: device_not_found_hint(),             # el texto depende del sistema operativo
    TrustRequired: "Desbloquea el iPhone y pulsa 'Confiar' cuando aparezca el aviso.",
    DeviceLocked: "El iPhone está bloqueado: desbloquéalo y vuelve a intentarlo.",
}


def _open_source(simulate: str | None):
    if simulate:
        from iphone_backup.device.simulated import SimulatedDeviceSource
        return SimulatedDeviceSource(Path(simulate))
    from iphone_backup.device.afc_source import AfcDeviceSource
    return AfcDeviceSource().connect()


def cmd_scan(args) -> int:
    try:
        source = _open_source(args.simulate)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    try:
        info = source.info()
        t0 = time.perf_counter()
        result = scan(source)
        secs = time.perf_counter() - t0
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    s = result.summary()
    s["scan_seconds"] = round(secs, 2)
    s["device"] = {"product_type": info.product_type, "ios_version": info.ios_version}
    print(f"Dispositivo: {info.product_type} (iOS {info.ios_version})")
    print(f"Elementos: {s['entries']}  |  Fotos: {s['photos']}  Vídeos: {s['videos']}  "
          f"AAE: {s['sidecars_aae']}  Ignorados: {s['ignored']}")
    print(f"Candidatos a Live Photo (por nombre): {s['live_photo_candidates']}  "
          f"Ambiguos: {s['ambiguous_name_groups']}")
    print(f"Tamaño total: {s['total_bytes'] / 1e9:.2f} GB  |  No legibles: {s['unreadable']}  |  {secs:.1f} s")
    if s["ignored_by_ext"]:
        print(f"Tipos ignorados (no se copiarán): {s['ignored_by_ext']}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_preview(args) -> int:
    """Vista previa del árbol de carpetas. SOLO LECTURA: no copia ni escribe nada en el destino."""
    from iphone_backup.app.planning import build_plan
    from iphone_backup.domain.place import PlaceLevel
    from iphone_backup.geolocation.offline import OfflineGeocoder
    from iphone_backup.metadata.exiftool_service import ExifToolService
    from iphone_backup.organizer.planner import OrganizerSettings

    settings = OrganizerSettings(template=args.template, use_gps=not args.no_gps,
                                 max_detail=PlaceLevel[args.detail.upper()])
    try:
        source = _open_source(args.simulate)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    geocoder = None
    if settings.use_gps:
        try:
            geocoder = OfflineGeocoder(language=args.lang, max_detail=settings.max_detail,
                                       parent_radius_km=args.merge_radius)
        except FileNotFoundError as e:
            print(f"Aviso: {e}. Los elementos con GPS irán a 'Ubicación no identificada'.", file=sys.stderr)
    try:
        with ExifToolService() as et:
            plan = build_plan(source, et, geocoder, settings)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    pv = plan.preview
    for line in pv.lines:
        print(line)
    print(f"\nTotal: {pv.total} | Fecha desconocida: {pv.unknown_date} | Sin ubicación: {pv.no_location} | "
          f"Ubicación no identificada: {pv.unidentified_location} | Colisiones de nombre a resolver: {pv.name_collisions}")
    print(f"Live Photos confirmados: {len(plan.confirmed_pairs)} | Errores de lectura de metadatos: {len(plan.probe_errors)}")
    if args.dest:
        from iphone_backup.transfer.space import check_space, filesystem_name, oversized_for_fs, required_bytes
        sizes = {f.path: f.size for f in plan.scan.files}
        need = required_bytes(sizes, plan.plans.keys())
        report = check_space(args.dest, need)
        print(report.message())
        big = oversized_for_fs(filesystem_name(args.dest), sizes)
        if big:
            print(f"ATENCIÓN: {len(big)} archivos superan el límite del sistema de archivos del destino y no se podrán copiar.")
        if not report.ok:
            return 3
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({
            "total": pv.total, "unknown_date": pv.unknown_date, "no_location": pv.no_location,
            "unidentified_location": pv.unidentified_location, "name_collisions": pv.name_collisions,
            "live_confirmed": len(plan.confirmed_pairs), "probe_errors": len(plan.probe_errors),
            "folders": {" / ".join(k): v for k, v in _folder_counts(plan).items()},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_backup(args) -> int:
    """Copia verificada al destino. REQUIERE ``--yes`` (confirmación explícita). El iPhone solo se lee."""
    import signal

    from iphone_backup.app.planning import build_plan, subset_plan
    from iphone_backup.applog import setup_logging
    from iphone_backup.database.manifest import Manifest
    from iphone_backup.domain.place import PlaceLevel
    from iphone_backup.geolocation.offline import OfflineGeocoder
    from iphone_backup.metadata.exiftool_service import ExifToolService
    from iphone_backup.metadata.extractor import exiftool_extractor
    from iphone_backup.organizer.planner import OrganizerSettings
    from iphone_backup.transfer.destination import Destination
    from iphone_backup.transfer.engine import BackupEngine, EngineConfig, EngineControl, SessionOutcome
    from iphone_backup.transfer.space import check_space, filesystem_name, oversized_for_fs, required_bytes

    dest = Destination(Path(args.dest))
    settings = OrganizerSettings(template=args.template, use_gps=not args.no_gps,
                                 max_detail=PlaceLevel[args.detail.upper()])
    try:
        source = _open_source(args.simulate)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    geocoder = None
    if settings.use_gps:
        try:
            geocoder = OfflineGeocoder(language=args.lang, max_detail=settings.max_detail,
                                       parent_radius_km=args.merge_radius)
        except FileNotFoundError as e:
            print(f"Aviso: {e}. Los elementos con GPS irán a 'Ubicación no identificada'.", file=sys.stderr)
    control = EngineControl()
    signal.signal(signal.SIGINT, lambda *_: (print("\nCancelando de forma segura…"), control.cancel()))
    manifest = None
    try:
        dest.prepare()
        setup_logging(dest.logs_dir, args.log_level)
        with ExifToolService() as et:
            print("Leyendo metadatos y preparando el plan (solo lectura)…")
            import json as _json
            cache_path = dest.manifest_path.parent / "metadata_cache.json"
            try:
                tag_cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                tag_cache = {}
            cached0 = len(tag_cache)
            try:
                plan = build_plan(source, et, geocoder, settings, tag_cache=tag_cache,
                                  progress=lambda i, n: print(f"\r  metadatos {i}/{n}", end="", flush=True) if i % 200 == 0 or i == n else None)
            finally:                         # también si el iPhone se corta a mitad: lo ya leído no se repite
                if len(tag_cache) != cached0:
                    tmp = cache_path.with_suffix(".tmp")
                    tmp.write_text(_json.dumps(tag_cache), encoding="utf-8")
                    tmp.replace(cache_path)
            print(f"\n  ({cached0} de los metadatos ya estaban en caché local; no se reabrieron en el iPhone)")
            manifest = Manifest(dest.manifest_path)
            existing = index = None
            if not args.no_existing_check:
                from iphone_backup.library.external_index import ExternalIndex, analyze_existing
                print("Buscando en tu biblioteca existente lo que ya está respaldado (solo lectura)…")
                index = ExternalIndex(dest.base, manifest, exclude_paths=[dest.library])
                wanted = {f.size for f in plan.scan.files if f.path in plan.plans}
                st = index.build(wanted, progress=lambda i, n: print(f"\r  hashes {i}/{n}", end="", flush=True))
                print(f"\n  Biblioteca: {st.scanned} archivos recorridos; {st.candidates} con tamaño coincidente "
                      f"({st.cache_hits} en caché, {st.hashed} leídos = {st.bytes_hashed / 1e9:.2f} GB)")
                by_path = {f.path: f for f in plan.scan.files}
                existing = analyze_existing(source, [by_path[p] for p in plan.plans], index,
                                            progress=lambda i, n: print(f"\r  comparando con el iPhone {i}/{n}", end="", flush=True))
                print(f"\n  Ya existen en tu biblioteca: {len(existing.matches)} archivos "
                      f"({existing.bytes_already_present / 1e9:.2f} GB): NO se copiarán.")
                if existing.unavailable_hash:
                    print(f"  {existing.unavailable_hash} sin hash del iPhone: se compararán tras copiarlos.")
            skip = set(existing.matches) if existing else set()
            if args.sample:
                plan = subset_plan(plan, args.sample, exclude=skip)
            sizes = {f.path: f.size for f in plan.scan.files}
            need = required_bytes(sizes, plan.plans.keys(), already_backed_up=skip)
            report = check_space(dest.base, need)
            print(f"Destino: {dest.library}  (sistema de archivos: {filesystem_name(dest.base)})")
            print(f"Se procesarán {len(plan.plans)} archivos; por copiar {need / 1e9:.2f} GB. {report.message()}")
            if oversized_for_fs(filesystem_name(dest.base), sizes):
                print("Hay archivos que superan el límite del sistema de archivos del destino.", file=sys.stderr)
                return 3
            if not report.ok:
                return 3
            if not args.yes:
                print("\nNo se copió nada. Para ejecutar la copia añade --yes (confirmación explícita).")
                return 0
            live: list = []          # LiveBar se crea con los totales reales del evento "start"

            def on_event(kind, **k):
                from iphone_backup.cli.progress import LiveBar
                if kind == "start":
                    live.append(LiveBar(k["total"], k["bytes"]))
                    print(f"\nCopiando {k['total']} archivos ({k['bytes'] / 1e9:.2f} GB). "
                          f"Ctrl+C cancela de forma segura. Progreso en vivo desde otra terminal: "
                          f"python -m iphone_backup.cli status --dest \"{dest.base}\" --watch")
                elif live:
                    live[0].event(kind, **k)

            engine = BackupEngine(source, manifest, dest, plan, settings=settings, geocoder=geocoder,
                                  extractor=exiftool_extractor(et), config=EngineConfig(workers=args.workers),
                                  control=control,
                                  on_event=on_event, existing=existing, external_index=index)
            res = engine.run()
        print(f"\n{res.summary()}  ({res.elapsed_s:.0f} s)")
        for w in res.warnings:
            print("Aviso:", w)
        if res.message:
            print(res.message)
        for path, msg in res.errors[:20]:
            print(f"  ERROR {path.rsplit('/', 1)[-1]}: {msg}")
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps({
                "outcome": res.outcome.value, "detected": res.detected, "copied": res.copied,
                "bytes_copied": res.bytes_copied, "skipped_incremental": res.skipped_incremental,
                "skipped_duplicate": res.skipped_duplicate, "skipped_existing": res.skipped_existing,
                "verified_full": res.verified_full,
                "verified_dest_only": res.verified_dest_only, "failed": res.failed, "quarantined": res.quarantined,
                "pending_quarantine": res.pending_quarantine, "elapsed_s": res.elapsed_s,
                "errors": [m for _, m in res.errors]}, indent=2, ensure_ascii=False), encoding="utf-8")
        if res.outcome is SessionOutcome.COMPLETED:
            return 0 if res.failed == 0 and res.quarantined == 0 else 1
        return {SessionOutcome.DEVICE_LOST: 2, SessionOutcome.NO_SPACE: 3}.get(res.outcome, 4)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    finally:
        if manifest:
            manifest.close()
        close = getattr(source, "close", None)
        if close:
            close()


def cmd_status(args) -> int:
    """Progreso de la copia (en curso o la última), leído del manifiesto en solo lectura."""
    from iphone_backup.app.status import read_status
    from iphone_backup.cli.progress import render_status
    from iphone_backup.transfer.destination import Destination

    mp = Destination(Path(args.dest)).manifest_path
    if not mp.is_file():
        print(f"No hay manifiesto en {mp}", file=sys.stderr)
        return 2
    try:
        while True:
            text = render_status(read_status(mp))
            if args.watch:
                print("\033[2J\033[H" if sys.stdout.isatty() else "", end="")
            print(text, flush=True)
            if not args.watch:
                return 0
            print("\n(Ctrl+C para salir; la copia no se ve afectada)", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_verify(args) -> int:
    """Releer lo copiado y comprobarlo contra el manifiesto (y el iPhone). Solo lectura."""
    from iphone_backup.database.manifest import Manifest
    from iphone_backup.transfer.destination import Destination
    from iphone_backup.transfer.verify import verify_library

    dest = Destination(Path(args.dest))
    if not dest.manifest_path.is_file():
        print(f"No hay manifiesto en {dest.manifest_path}", file=sys.stderr)
        return 2
    source = None
    if args.device or args.simulate:
        try:
            source = _open_source(args.simulate)
        except DeviceError as e:
            print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
            return 2
    try:
        with Manifest(dest.manifest_path) as mf:
            rep = verify_library(dest, mf, source, progress=lambda i, n: print(f"\r  verificando {i}/{n}", end="", flush=True))
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    print(f"\nVerificados: {rep.ok}/{rep.checked} | Faltan: {len(rep.missing)} | Tamaño distinto: "
          f"{len(rep.size_mismatch)} | Hash distinto: {len(rep.hash_mismatch)}")
    if source is not None:
        print(f"Comparados con el SHA-1 actual del iPhone: {rep.device_checked} | "
              f"cambiados en el iPhone: {len(rep.device_changed)} | sin hash: {rep.device_unavailable}")
    for label, items in (("FALTA", rep.missing), ("TAMAÑO", rep.size_mismatch), ("HASH", rep.hash_mismatch)):
        for x in items[:20]:
            print(f"  {label}: {x}")
    return 0 if rep.clean else 1


def cmd_deletion_report(args) -> int:
    """Informe de lo que NO está a salvo para poder liberar espacio. NO borra nada (solo lectura)."""
    from iphone_backup.database.manifest import Manifest
    from iphone_backup.transfer.destination import Destination
    from iphone_backup.transfer.safety_report import REASONS, build_safety_report, write_report

    dest = Destination(Path(args.dest))
    if not dest.manifest_path.is_file():
        print(f"No hay manifiesto en {dest.manifest_path}", file=sys.stderr)
        return 2
    try:
        source = _open_source(args.simulate)
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    try:
        with Manifest(dest.manifest_path) as mf:
            rep = build_safety_report(source, dest, mf, recheck_disk=not args.no_recheck,
                                      progress=lambda i, n: print(f"\r  analizando {i}/{n}", end="", flush=True))
    except DeviceError as e:
        print(HINTS.get(type(e), f"Error de dispositivo: {e}"), file=sys.stderr)
        return 2
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    csv_path, txt_path = write_report(rep, dest.logs_dir)
    print(f"\nFotos y vídeos vistos: {rep.total_media} | A salvo: {rep.safe} ({rep.safe_bytes / 1e9:.2f} GB) | "
          f"NO a salvo: {len(rep.exceptions)} | Cuadre: {'OK' if rep.consistent else 'NO CUADRA'}")
    for r, n in rep.by_reason().most_common():
        print(f"  {n:5}  {REASONS[r]}")
    print(f"Resumen: {txt_path}\nLista de excepciones (no borrar): {csv_path}")
    return 0 if rep.consistent else 1


def _folder_counts(plan) -> dict:
    from collections import Counter
    return Counter(p.rel_dir for p in plan.plans.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="iphone_backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="Vista previa del árbol de carpetas (solo lectura)")
    pv.add_argument("--simulate", help="Carpeta con DCIM/ para simular un iPhone")
    pv.add_argument("--template", default="Año/Mes/Ubicación",
                    choices=["Año/Mes/Ubicación", "Año/Mes", "Año/Ubicación/Mes"])
    pv.add_argument("--no-gps", action="store_true", help="No organizar por ubicación")
    pv.add_argument("--detail", default="city", choices=["city", "region", "country"])
    pv.add_argument("--lang", default="es", choices=["es", "en"])
    pv.add_argument("--dest", help="Carpeta/disco de destino: comprueba espacio libre y sistema de archivos")
    pv.add_argument("--merge-radius", type=float, default=30.0,
                    help="Km: una ciudad pequeña se une a una mayor cercana (área metropolitana)")
    pv.add_argument("--json", help="Guardar resumen (conteos por carpeta) en JSON")
    pv.set_defaults(func=cmd_preview)

    stt = sub.add_parser("status", help="Progreso de la copia en curso o la última (solo lectura)")
    stt.add_argument("--dest", required=True)
    stt.add_argument("--watch", action="store_true", help="Refrescar en vivo")
    stt.add_argument("--interval", type=float, default=5.0)
    stt.set_defaults(func=cmd_status)

    dr = sub.add_parser("deletion-report", help="Qué NO está a salvo en el iPhone (no borra nada)")
    dr.add_argument("--dest", required=True)
    dr.add_argument("--simulate", help="Carpeta con DCIM/ para simular un iPhone")
    dr.add_argument("--no-recheck", action="store_true", help="No releer el disco (solo existencia y tamaño)")
    dr.set_defaults(func=cmd_deletion_report)

    vf = sub.add_parser("verify", help="Verifica lo ya respaldado (SHA-256 del disco; opcional SHA-1 del iPhone)")
    vf.add_argument("--dest", required=True)
    vf.add_argument("--device", action="store_true", help="Comparar también con el SHA-1 actual del iPhone")
    vf.add_argument("--simulate", help="Carpeta con DCIM/ para simular un iPhone")
    vf.set_defaults(func=cmd_verify)

    bk = sub.add_parser("backup", help="Copia verificada e incremental al destino (el iPhone solo se lee)")
    bk.add_argument("--dest", required=True, help="Carpeta base del destino (se crea <dest>/iPhone_Photos)")
    bk.add_argument("--yes", action="store_true", help="Confirma la copia. Sin esto solo se muestra el plan")
    bk.add_argument("--sample", type=int, default=0, help="Copiar solo una muestra representativa de N grupos")
    bk.add_argument("--simulate", help="Carpeta con DCIM/ para simular un iPhone")
    bk.add_argument("--workers", type=int, default=4, help="Archivos copiados/verificados a la vez (1 = secuencial)")
    bk.add_argument("--no-existing-check", action="store_true",
                    help="No buscar duplicados en la biblioteca existente del destino")
    bk.add_argument("--template", default="Año/Mes/Ubicación",
                    choices=["Año/Mes/Ubicación", "Año/Mes", "Año/Ubicación/Mes"])
    bk.add_argument("--no-gps", action="store_true")
    bk.add_argument("--detail", default="city", choices=["city", "region", "country"])
    bk.add_argument("--lang", default="es", choices=["es", "en"])
    bk.add_argument("--merge-radius", type=float, default=30.0)
    bk.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    bk.add_argument("--json", help="Guardar el resumen de la sesión en JSON")
    bk.set_defaults(func=cmd_backup)
    p = sub.add_parser("scan", help="Inventario de solo lectura del iPhone")
    p.add_argument("--simulate", help="Carpeta con DCIM/ para simular un iPhone")
    p.add_argument("--json", help="Guardar resumen anónimo en JSON")
    p.set_defaults(func=cmd_scan)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
