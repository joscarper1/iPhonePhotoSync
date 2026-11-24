"""Planificación de carpetas de destino (pura: no toca el disco ni el dispositivo).

Regla de grupo: un Live Photo confirmado (foto + MOV) y su .AAE se colocan JUNTOS en la carpeta que
determina la foto ("líder"). Un MOV sin foto es su propio líder. Elementos sin fecha van a
``Fecha desconocida``; sin GPS a ``Sin ubicación``.
"""
from __future__ import annotations

import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum

from iphonephotosync.domain.filetypes import FileKind
from iphonephotosync.domain.models import DeviceFile
from iphonephotosync.domain.place import Place, PlaceLevel
from iphonephotosync.metadata.policy import MediaMetadata
from iphonephotosync.organizer.naming import month_folder, sanitize_component
from iphonephotosync.ports.geocoder import Geocoder
from iphonephotosync.scanner.inventory import STILL_EXTS, ScanResult

TEMPLATES = {
    "Año/Mes/Ubicación": ("year", "month", "place"),
    "Año/Mes": ("year", "month"),
    "Año/Ubicación/Mes": ("year", "place", "month"),
    # el álbum del iPhone es la raíz; lo que no está en ningún álbum no lleva raíz (Año/Mes/Ubicación, como antes)
    "Álbum/Año/Mes/Ubicación": ("album", "year", "month", "place"),
}


class PlaceStatus(str, Enum):
    RESOLVED = "resolved"          # nombre de lugar obtenido
    NO_GPS = "no_gps"
    UNIDENTIFIED = "unidentified"  # hay GPS pero ningún lugar dentro de los radios
    DISABLED = "disabled"          # organización por GPS desactivada / plantilla sin ubicación


@dataclass(frozen=True)
class OrganizerSettings:
    template: str = "Año/Mes/Ubicación"
    use_gps: bool = True
    max_detail: PlaceLevel = PlaceLevel.CITY
    unknown_date_folder: str = "Fecha desconocida"
    no_location_folder: str = "Sin ubicación"
    unidentified_location_folder: str = "Ubicación no identificada"

    def tokens(self) -> tuple[str, ...]:
        if self.template not in TEMPLATES:
            raise ValueError(f"Plantilla desconocida: {self.template!r}. Opciones: {sorted(TEMPLATES)}")
        return TEMPLATES[self.template]


@dataclass(frozen=True)
class FolderPlan:
    device_path: str
    rel_dir: tuple[str, ...]
    leader: str | None            # ruta del archivo que decidió la carpeta
    year: int | None
    month: int | None
    place_status: PlaceStatus
    place_label: str | None
    date_unknown: bool
    album: str | None = None      # álbum del iPhone que actúa de raíz (solo con la plantilla "Álbum/…")


def group_leaders(scan: ScanResult, confirmed_pairs: set[tuple[str, str]] | None = None) -> dict[str, str | None]:
    """ruta -> ruta del líder (o None si es .AAE huérfano). Solo cubre fotos, vídeos y .AAE."""
    confirmed = confirmed_pairs or set()
    groups: dict[tuple[str, str], list[DeviceFile]] = defaultdict(list)
    for f in scan.files:
        if scan.kinds[f.path] in (FileKind.PHOTO, FileKind.VIDEO, FileKind.SIDECAR):
            groups[(f.folder, posixpath.splitext(f.name)[0].lower())].append(f)
    leader: dict[str, str | None] = {}
    for members in groups.values():
        stills = [m for m in members if m.ext in STILL_EXTS]
        videos = [m for m in members if scan.kinds[m.path] is FileKind.VIDEO]
        aaes = [m for m in members if scan.kinds[m.path] is FileKind.SIDECAR]
        others = [m for m in members if scan.kinds[m.path] is FileKind.PHOTO and m.ext not in STILL_EXTS]
        for m in others + (stills if len(stills) != 1 else []):
            leader[m.path] = m.path
        main = None
        if len(stills) == 1:
            main = stills[0]
            leader[main.path] = main.path
        for v in videos:
            leader[v.path] = main.path if main and (main.path, v.path) in confirmed else v.path
        if main is None and len(videos) == 1:
            main = videos[0]
        for a in aaes:
            leader[a.path] = main.path if main else None
    return leader


@dataclass(frozen=True)
class Resolved:
    rel_dir: tuple[str, ...]
    year: int | None
    month: int | None
    place_status: PlaceStatus
    place_label: str | None
    date_unknown: bool
    place: Place | None          # lugar completo (ciudad/región/país) para el manifiesto


class FolderResolver:
    """Decide la carpeta de destino a partir de los metadatos de UN archivo (líder del grupo).
    Se usa tanto para el plan previo (metadatos sondeados) como para la copia (metadatos definitivos)."""

    def __init__(self, settings: OrganizerSettings, geocoder: Geocoder | None):
        self._s, self._geo, self._tokens = settings, geocoder, settings.tokens()
        self._cache: dict[tuple[float, float], tuple[PlaceStatus, str | None, Place | None]] = {}

    def _place(self, m: MediaMetadata) -> tuple[PlaceStatus, str | None, Place | None]:
        s = self._s
        if "place" not in self._tokens or not s.use_gps:
            return PlaceStatus.DISABLED, None, None
        if not m.has_gps:
            return PlaceStatus.NO_GPS, s.no_location_folder, None
        key = (round(m.gps_lat, 5), round(m.gps_lon, 5))
        if key not in self._cache:
            p = self._geo.lookup(*key) if self._geo else None
            if p is None:
                self._cache[key] = (PlaceStatus.UNIDENTIFIED, s.unidentified_location_folder, None)
            else:
                self._cache[key] = (PlaceStatus.RESOLVED,
                                    sanitize_component(p.coarsened(s.max_detail).folder_name()), p)
        return self._cache[key]

    def resolve(self, m: MediaMetadata | None, album: str | None = None) -> Resolved:
        s = self._s
        root = [sanitize_component(album)] if album and "album" in self._tokens else []
        root = [r for r in root if r]
        if m is None:  # .AAE huérfano o sin metadatos del líder: nada que clasificar
            status = PlaceStatus.DISABLED if "place" not in self._tokens else PlaceStatus.NO_GPS
            label = s.no_location_folder if status is PlaceStatus.NO_GPS else None
            parts = root + [s.unknown_date_folder] + ([label] if label else [])
            return Resolved(tuple(parts), None, None, status, label, True, None)
        status, label, place = self._place(m)
        if m.capture_dt is None:
            parts = root + [s.unknown_date_folder] + ([label] if label else [])
            return Resolved(tuple(parts), None, None, status, label, True, place)
        year, month = int(m.capture_dt[:4]), int(m.capture_dt[5:7])
        values = {"year": f"{year:04d}", "month": month_folder(month), "place": label,
                  "album": root[0] if root else None}
        return Resolved(tuple(values[t] for t in self._tokens if values[t]), year, month, status, label, False, place)


def plan_folders(scan: ScanResult, metas: dict[str, MediaMetadata], settings: OrganizerSettings,
                 geocoder: Geocoder | None = None,
                 confirmed_pairs: set[tuple[str, str]] | None = None,
                 albums: dict[str, str] | None = None) -> dict[str, FolderPlan]:
    """``albums``: ruta del iPhone -> álbum principal. El álbum del LÍDER decide la raíz de todo el grupo."""
    leaders = group_leaders(scan, confirmed_pairs)

    resolver = FolderResolver(settings, geocoder)
    plans: dict[str, FolderPlan] = {}
    for path, lead in leaders.items():
        album = (albums or {}).get(lead) if lead else None
        r = resolver.resolve(metas.get(lead) if lead else None, album=album)
        plans[path] = FolderPlan(path, r.rel_dir, lead, r.year, r.month, r.place_status, r.place_label, r.date_unknown,
                                 album if "album" in settings.tokens() else None)
    return plans


def planned_name_collisions(scan: ScanResult, plans: dict[str, FolderPlan]) -> list[tuple[str, ...]]:
    """Grupos de archivos que caerían en la misma carpeta con el mismo nombre (se resolverán con sufijo)."""
    seen: dict[tuple[tuple[str, ...], str], list[str]] = defaultdict(list)
    by_path = {f.path: f for f in scan.files}
    for path, plan in plans.items():
        seen[(tuple(p.casefold() for p in plan.rel_dir), by_path[path].name.casefold())].append(path)
    return [tuple(sorted(v)) for v in seen.values() if len(v) > 1]


@dataclass
class Preview:
    tree: dict
    total: int
    unknown_date: int
    no_location: int
    unidentified_location: int
    name_collisions: int
    lines: list[str] = field(default_factory=list)


def build_preview(scan: ScanResult, plans: dict[str, FolderPlan]) -> Preview:
    """Árbol informativo (solo conteos); no escribe nada."""
    tree: dict = {}
    counts: Counter = Counter()
    for plan in plans.values():
        counts[plan.rel_dir] += 1
        node = tree
        for part in plan.rel_dir:
            node = node.setdefault(part, {})
    lines: list[str] = []

    def total(prefix: tuple[str, ...]) -> int:
        return sum(n for d, n in counts.items() if d[: len(prefix)] == prefix)

    def render(node: dict, prefix: tuple[str, ...], indent: str, depth: int) -> None:
        names = sorted(node)
        for i, name in enumerate(names):
            last = i == len(names) - 1
            path = prefix + (name,)
            label = f"{name} ({total(path)} elementos)"
            lines.append(("" if depth == 0 else indent + ("└── " if last else "├── ")) + label)
            render(node[name], path, indent + ("" if depth == 0 else ("    " if last else "│   ")), depth + 1)

    render(tree, (), "", 0)
    return Preview(
        tree=tree, total=len(plans),
        unknown_date=sum(1 for p in plans.values() if p.date_unknown),
        no_location=sum(1 for p in plans.values() if p.place_status is PlaceStatus.NO_GPS),
        unidentified_location=sum(1 for p in plans.values() if p.place_status is PlaceStatus.UNIDENTIFIED),
        name_collisions=len(planned_name_collisions(scan, plans)), lines=lines)
