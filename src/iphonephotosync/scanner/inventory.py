"""Inventario: clasifica, cuenta y detecta candidatos a Live Photo por nombre.

Un par (still + MOV con mismo nombre base en la misma carpeta) es solo un CANDIDATO: se confirma
en la Etapa 3 con el ContentIdentifier. El escáner no escribe nada ni lee contenido de archivos.
"""
from __future__ import annotations

import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from iphonephotosync.domain.filetypes import FileKind, classify
from iphonephotosync.domain.models import DeviceFile
from iphonephotosync.ports.device import DeviceSource

STILL_EXTS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
MOTION_EXT = ".mov"
SIDECAR_EXT = ".aae"


@dataclass(frozen=True)
class LivePairCandidate:
    still: DeviceFile
    motion: DeviceFile


@dataclass
class ScanResult:
    files: list[DeviceFile]
    kinds: dict[str, FileKind]                       # ruta -> tipo
    live_candidates: list[LivePairCandidate]
    ambiguous_pairs: list[tuple[str, ...]]           # nombres base con >1 still o >1 mov
    sidecars_by_still: dict[str, str]                # ruta del still -> ruta del .aae
    orphan_sidecars: list[str]
    unreadable: list[tuple[str, str]]                # (ruta, motivo)
    counts: Counter = field(default_factory=Counter)
    ignored_by_ext: Counter = field(default_factory=Counter)
    by_ext: Counter = field(default_factory=Counter)
    total_bytes: int = 0
    bytes_by_kind: Counter = field(default_factory=Counter)

    def summary(self) -> dict:
        """Resumen sin nombres de archivo (apto para compartir)."""
        return {
            "entries": len(self.files),
            "photos": self.counts[FileKind.PHOTO],
            "videos": self.counts[FileKind.VIDEO],
            "sidecars_aae": self.counts[FileKind.SIDECAR],
            "ignored": self.counts[FileKind.IGNORED],
            "ignored_by_ext": dict(self.ignored_by_ext),
            "by_extension": dict(self.by_ext.most_common()),
            "live_photo_candidates": len(self.live_candidates),
            "ambiguous_name_groups": len(self.ambiguous_pairs),
            "aae_attached": len(self.sidecars_by_still),
            "aae_orphan": len(self.orphan_sidecars),
            "unreadable": len(self.unreadable),
            "total_bytes": self.total_bytes,
            "bytes_by_kind": {k.value: v for k, v in self.bytes_by_kind.items()},
        }


def scan(source: DeviceSource) -> ScanResult:
    files = list(source.list_files())
    kinds: dict[str, FileKind] = {}
    counts, ignored_by_ext, by_ext, bytes_by_kind = Counter(), Counter(), Counter(), Counter()
    total = 0
    groups: dict[tuple[str, str], list[DeviceFile]] = defaultdict(list)

    for f in files:
        kind, _ = classify(f.ext)
        kinds[f.path] = kind
        counts[kind] += 1
        by_ext[f.ext or "(sin extensión)"] += 1
        bytes_by_kind[kind] += f.size
        total += f.size
        if kind is FileKind.IGNORED:
            ignored_by_ext[f.ext or "(sin extensión)"] += 1
        base = posixpath.splitext(f.name)[0].lower()
        groups[(f.folder, base)].append(f)

    live: list[LivePairCandidate] = []
    ambiguous: list[tuple[str, ...]] = []
    sidecar_of: dict[str, str] = {}
    orphans: list[str] = []

    for (folder, base), members in sorted(groups.items()):
        stills = [m for m in members if m.ext in STILL_EXTS]
        motions = [m for m in members if m.ext == MOTION_EXT]
        aaes = [m for m in members if m.ext == SIDECAR_EXT]
        if stills and motions:
            if len(stills) == 1 and len(motions) == 1:
                live.append(LivePairCandidate(stills[0], motions[0]))
            else:
                ambiguous.append(tuple(sorted(m.path for m in stills + motions)))
        for a in aaes:
            if len(stills) == 1:
                sidecar_of[stills[0].path] = a.path
            else:
                orphans.append(a.path)

    return ScanResult(files=files, kinds=kinds, live_candidates=live, ambiguous_pairs=ambiguous,
                      sidecars_by_still=sidecar_of, orphan_sidecars=orphans,
                      unreadable=list(source.listing_errors()), counts=counts,
                      ignored_by_ext=ignored_by_ext, by_ext=by_ext, total_bytes=total,
                      bytes_by_kind=bytes_by_kind)
