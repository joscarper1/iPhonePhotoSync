"""Índice de la biblioteca existente para NO duplicar lo que el usuario ya tiene.

SOLO LECTURA sobre la biblioteca existente: se recorre, se leen bytes para calcular hashes, y nada más.
Nunca se mueve, renombra ni borra un archivo existente.

Eficiencia: contenido idéntico implica tamaño idéntico, así que solo se calculan hashes de los archivos
existentes cuyo tamaño coincide con algún archivo del iPhone. Los hashes se cachean en el manifiesto
(clave: ruta + tamaño + fecha de modificación) para que las siguientes ejecuciones no relean.

Se excluyen: la carpeta propia de este programa, la papelera del usuario ("Papelera", $RECYCLE.BIN) y
carpetas del sistema: un archivo que solo está en la papelera NO cuenta como respaldado.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from iphonephotosync.database.manifest import Manifest
from iphonephotosync.domain.models import DeviceFile
from iphonephotosync.organizer.naming import to_extended
from iphonephotosync.ports.device import DeviceSource

DEFAULT_EXCLUDED_DIRS = frozenset({"papelera", "$recycle.bin", "system volume information"})
CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class ExtFile:
    rel: str            # ruta relativa (POSIX) a la carpeta base
    size: int
    mtime_ns: int


@dataclass
class IndexStats:
    scanned: int = 0
    excluded_dirs: int = 0
    candidates: int = 0          # archivos existentes con tamaño coincidente
    hashed: int = 0              # leídos ahora
    cache_hits: int = 0
    bytes_hashed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


class ExternalIndex:
    def __init__(self, base: Path, manifest: Manifest, *, exclude_paths: Iterable[Path] = (),
                 exclude_dir_names: Iterable[str] = DEFAULT_EXCLUDED_DIRS):
        self.base = Path(base)
        self.manifest = manifest
        self.exclude_paths = [os.path.normcase(os.path.abspath(p)) for p in exclude_paths]
        self.exclude_names = {n.lower() for n in exclude_dir_names}
        self.by_size: dict[int, list[ExtFile]] = {}
        self.by_sha1: dict[str, list[str]] = {}
        self.by_sha256: dict[str, list[str]] = {}
        self.in_trash_only_sizes = 0
        self.stats = IndexStats()
        self.listing: list[ExtFile] = []

    def _excluded(self, path: str, name: str) -> bool:
        if name.lower() in self.exclude_names:
            return True
        norm = os.path.normcase(os.path.abspath(path))
        return any(norm == p or norm.startswith(p + os.sep) for p in self.exclude_paths)

    def walk(self) -> Iterable[ExtFile]:
        stack = [str(self.base)]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(to_extended(Path(d))) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if self._excluded(os.path.join(d, e.name), e.name):
                                    self.stats.excluded_dirs += 1
                                else:
                                    stack.append(os.path.join(d, e.name))
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                rel = Path(os.path.join(d, e.name)).relative_to(self.base).as_posix()
                                yield ExtFile(rel, st.st_size, st.st_mtime_ns)
                        except OSError as ex:
                            self.stats.errors.append((e.name, str(ex)))
            except OSError as ex:
                self.stats.errors.append((d, str(ex)))

    def build(self, candidate_sizes: set[int], progress: Callable[[int, int], None] | None = None,
              should_stop: Callable[[], bool] = lambda: False,
              walk_progress: Callable[[int], None] | None = None,
              listing: list[ExtFile] | None = None) -> IndexStats:
        """Recorre la biblioteca y calcula (o recupera de la caché) los hashes de los archivos con
        tamaño candidato. ``walk_progress(n)``: archivos recorridos hasta ahora (no se conoce el total).
        ``listing``: listado guardado de un recorrido anterior; si se da, NO se recorre el disco. En ambos casos el
        listado usado queda en ``self.listing`` para poder guardarlo y reutilizarlo."""
        todo: list[ExtFile] = []
        self.listing = [] if listing is None else listing
        for f in (self.walk() if listing is None else listing):
            if listing is None:
                self.listing.append(f)
            self.stats.scanned += 1
            if walk_progress and self.stats.scanned % 1000 == 0:
                walk_progress(self.stats.scanned)
            if f.size > 0 and f.size in candidate_sizes:
                self.by_size.setdefault(f.size, []).append(f)
                todo.append(f)
        self.stats.candidates = len(todo)
        for i, f in enumerate(todo, 1):
            if should_stop():
                break
            hit = self.manifest.external_lookup(f.rel, f.size, f.mtime_ns)
            if hit:
                self.stats.cache_hits += 1
                sha1, sha256 = hit
            else:
                try:
                    sha1, sha256 = self._hash(self.base / f.rel)
                except OSError as ex:
                    self.stats.errors.append((f.rel, str(ex)))
                    continue
                self.manifest.external_store(f.rel, f.size, f.mtime_ns, sha1, sha256)
                self.stats.hashed += 1
                self.stats.bytes_hashed += f.size
            self.by_sha1.setdefault(sha1, []).append(f.rel)
            self.by_sha256.setdefault(sha256, []).append(f.rel)
            if progress and (i % 50 == 0 or i == len(todo)):
                progress(i, len(todo))
        return self.stats

    @staticmethod
    def _hash(path: Path) -> tuple[str, str]:
        h1, h256 = hashlib.sha1(), hashlib.sha256()
        with open(to_extended(path), "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                h1.update(block)
                h256.update(block)
        return h1.hexdigest(), h256.hexdigest()

    def find_by_sha256(self, sha256: str) -> str | None:
        v = self.by_sha256.get(sha256)
        return v[0] if v else None


@dataclass
class ExistingMatches:
    matches: dict[str, str] = field(default_factory=dict)       # ruta en el iPhone -> ruta relativa existente
    checked: int = 0                                             # archivos con tamaño candidato comparados
    unavailable_hash: int = 0                                    # el iPhone no ofreció hash: se decidirá tras copiar
    bytes_already_present: int = 0


def analyze_existing(source: DeviceSource, files: Iterable[DeviceFile], index: ExternalIndex,
                     progress: Callable[[int, int], None] | None = None) -> ExistingMatches:
    """Compara el SHA-1 que calcula el iPhone (sin transferir el archivo) con el de los archivos
    existentes del mismo tamaño. Coincidencia de tamaño + SHA-1 = ya respaldado."""
    out = ExistingMatches()
    cands = [f for f in files if f.size in index.by_size]
    for i, f in enumerate(cands, 1):
        out.checked += 1
        d = source.source_sha1(f.path)
        if d is None:
            out.unavailable_hash += 1
        else:
            hit = index.by_sha1.get(d.hex())
            if hit:
                # defensa extra: el archivo existente con ese hash debe tener también el MISMO tamaño
                same_size = {e.rel for e in index.by_size.get(f.size, [])}
                rel = next((r for r in hit if r in same_size), None)
                if rel:
                    out.matches[f.path] = rel
                    out.bytes_already_present += f.size
        if progress and (i % 25 == 0 or i == len(cands)):
            progress(i, len(cands))
    return out
