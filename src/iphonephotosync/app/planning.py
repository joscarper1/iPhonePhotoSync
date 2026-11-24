"""Caso de uso: escanear, leer metadatos, confirmar Live Photos, geolocalizar y planificar carpetas.

SOLO LECTURA: no escribe en el iPhone ni en el destino. Devuelve un plan y una vista previa.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from iphonephotosync.domain.filetypes import FileKind
from iphonephotosync.metadata.device_probe import make_tmp_dir, probe_tags
from iphonephotosync.metadata.exiftool_service import ExifToolService
from iphonephotosync.metadata.live import confirm_pair
from iphonephotosync.metadata.policy import MediaMetadata, interpret
from iphonephotosync.organizer.planner import FolderPlan, OrganizerSettings, Preview, build_preview, plan_folders
from iphonephotosync.ports.device import (
    DeviceDisconnected, DeviceError, DeviceLocked, DeviceNotFound, DeviceSource, TrustRequired,
)
from iphonephotosync.ports.geocoder import Geocoder
from iphonephotosync.scanner import ScanResult, scan

from pathlib import Path


@dataclass
class PlanResult:
    scan: ScanResult
    metas: dict[str, MediaMetadata]
    confirmed_pairs: set[tuple[str, str]]
    pair_ids: dict[str, str]                       # ruta -> pair_id (Live Photos confirmados)
    unconfirmed: dict[str, int] = field(default_factory=dict)   # motivo -> cantidad
    plans: dict[str, FolderPlan] = field(default_factory=dict)
    preview: Preview | None = None
    probe_errors: dict[str, str] = field(default_factory=dict)  # ruta -> error


def subset_plan(plan: PlanResult, n: int, exclude: set[str] | None = None) -> PlanResult:
    """Muestra representativa de ``n`` grupos (determinista): repartida de forma uniforme y garantizando,
    si existen, un Live Photo, un vídeo suelto, un HEIC, un archivo sin fecha y un PNG.
    ``exclude``: rutas que no se copiarán (p. ej. ya existentes); los grupos totalmente excluidos no se eligen."""
    from dataclasses import replace

    skip = exclude or set()
    groups: dict[str, list[str]] = {}
    for path, p in plan.plans.items():
        groups.setdefault(p.leader or path, []).append(path)
    keys = sorted(k for k, members in groups.items() if any(m not in skip for m in members))
    if n >= len(keys):
        return plan if not skip else replace(plan, plans={p: v for p, v in plan.plans.items()
                                                           if (v.leader or p) in set(keys)})
    chosen: list[str] = [keys[i * len(keys) // n] for i in range(n)]

    def cat(key: str) -> set[str]:
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        c = set()
        if key in plan.pair_ids:
            c.add("live")
        if ext in ("mov", "mp4") and key not in plan.pair_ids:
            c.add("video")
        if ext in ("heic", "heif"):
            c.add("heic")
        if ext == "png":
            c.add("png")
        if plan.plans[key].date_unknown:
            c.add("unknown_date")
        return c

    have = set().union(*(cat(k) for k in chosen)) if chosen else set()
    for want in ("live", "video", "heic", "unknown_date", "png"):
        if want not in have:
            for k in keys:
                if k not in chosen and want in cat(k):
                    chosen.append(k)
                    have |= cat(k)
                    break
    keep = {p for k in chosen for p in groups[k]}
    return replace(plan, plans={p: v for p, v in plan.plans.items() if p in keep})


def build_plan(source: DeviceSource, et: ExifToolService, geocoder: Geocoder | None,
               settings: OrganizerSettings, use_mtime_fallback: bool = False,
               progress: Callable[[int, int], None] | None = None,
               tag_cache: dict[str, dict] | None = None,
               albums: dict[str, str] | None = None) -> PlanResult:
    """``tag_cache``: huella (tamaño:mtime) -> etiquetas ExifTool ya leídas. Evita reabrir en el iPhone archivos
    sin cambios al reanudar (cada apertura gasta un recurso del servicio AFC). Se rellena con lo nuevo."""
    result = scan(source)
    metas: dict[str, MediaMetadata] = {}
    errors: dict[str, str] = {}
    media = [f for f in result.files if result.kinds[f.path] in (FileKind.PHOTO, FileKind.VIDEO)]
    with make_tmp_dir() as td:
        for i, f in enumerate(media, 1):
            try:
                fp = f"{f.path}|{f.fingerprint}"
                if tag_cache is not None and fp in tag_cache:
                    tags = tag_cache[fp]
                else:
                    tags = probe_tags(source, f, et, Path(td))
                    if tag_cache is not None and tags is not None:
                        tag_cache[fp] = tags
                metas[f.path] = interpret(tags or {}, result.kinds[f.path], mtime_ns=f.mtime_ns,
                                          use_mtime_fallback=use_mtime_fallback)
            except (DeviceDisconnected, DeviceLocked, DeviceNotFound, TrustRequired):
                raise                       # el iPhone dejó de responder: un plan incompleto sería engañoso
            except DeviceError as e:
                errors[f.path] = f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001  (p. ej. ExifToolError): el elemento se reporta, no se descarta
                errors[f.path] = f"{type(e).__name__}: {e}"
            if progress:
                progress(i, len(media))

    confirmed: set[tuple[str, str]] = set()
    pair_ids: dict[str, str] = {}
    unconfirmed: dict[str, int] = {}
    for pair in result.live_candidates:
        a, b = metas.get(pair.still.path), metas.get(pair.motion.path)
        v = confirm_pair(a.content_identifier if a else None, b.content_identifier if b else None)
        if v.confirmed:
            confirmed.add((pair.still.path, pair.motion.path))
            pair_ids[pair.still.path] = pair_ids[pair.motion.path] = v.pair_id
        else:
            unconfirmed[v.reason] = unconfirmed.get(v.reason, 0) + 1

    plans = plan_folders(result, metas, settings, geocoder, confirmed, albums)
    return PlanResult(result, metas, confirmed, pair_ids, unconfirmed, plans, build_preview(result, plans), errors)
