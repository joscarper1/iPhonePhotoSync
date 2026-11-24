"""Flujo de la aplicación (sin Qt): conectar -> escanear -> respaldar.

La interfaz (``ui/``) solo llama a esta clase desde hilos de trabajo y pinta lo que devuelve; así toda la
lógica es comprobable sin ventana. El iPhone solo se lee. Nada se copia hasta ``run_backup``.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from iphone_backup.domain.filetypes import FileKind
from iphone_backup.domain.place import PlaceLevel
from iphone_backup.ports.device import (
    DeviceDisconnected, DeviceError, DeviceLocked, DeviceNotFound, DeviceSource, TrustRequired,
)

from iphone_backup.transfer.platform_fs import device_not_found_hint

HINTS = {
    DeviceNotFound: device_not_found_hint(),             # el texto depende del sistema operativo
    TrustRequired: "Desbloquea el iPhone y pulsa 'Confiar' cuando aparezca el aviso.",
    DeviceLocked: "El iPhone está bloqueado: desbloquéalo y vuelve a intentarlo.",
    DeviceDisconnected: "Se perdió la conexión con el iPhone. Revisa el cable y que siga desbloqueado.",
}
TEMPLATES_UI = ["Año/Mes/Ubicación"]          # las plantillas disponibles las define el organizador
ALBUM_TEMPLATE = "Álbum/Año/Mes/Ubicación"
LOG_LEVELS = ["WARNING", "INFO", "DEBUG"]


def friendly(e: BaseException) -> str:
    for cls, text in HINTS.items():
        if isinstance(e, cls):
            return text
    return f"Error de dispositivo: {e}"


def normalize_dest(path: str) -> tuple[str, str]:
    """El destino es la carpeta que CONTIENE ``iPhone_Photos``. Si el usuario eligió la propia biblioteca (tiene
    ``_Manifest/backup_id.json``), se sube un nivel: de lo contrario se crearía una biblioteca anidada vacía,
    sin el registro de lo ya respaldado, y todo parecería nuevo. Devuelve (ruta, aviso)."""
    if path:
        p = Path(path)
        if (p / "_Manifest" / "backup_id.json").is_file():
            return str(p.parent), f"Elegiste la carpeta de la biblioteca ({p.name}); se usará su carpeta madre: {p.parent}"
    return path, ""


# --------------------------------------------------------------------------- configuración
def settings_path() -> Path:
    import os
    base = os.environ.get("IPHONE_BACKUP_HOME") or str(Path.home() / ".iphone_backup")
    return Path(base) / "settings.json"


@dataclass
class UserSettings:
    """Valores predeterminados que priorizan conservación y seguridad. Solo se ofrecen opciones REALES:
    la verificación SHA-256 y 'nunca sobrescribir' no son opcionales en el motor y se muestran como informativas."""
    dest: str = ""
    template: str = "Año/Mes/Ubicación"
    use_gps: bool = True
    max_detail: str = "CITY"                 # COUNTRY | REGION | CITY
    merge_radius_km: float = 30.0
    include_photos: bool = True
    include_videos: bool = True
    log_level: str = "WARNING"
    workers: int = 4
    full_scan_after_hours: int = 24          # pasado este tiempo desde el último escaneo completo, se pregunta
    by_album: bool = True                    # el álbum del iPhone es la raíz: Álbum/Año/Mes/Ubicación

    @classmethod
    def load(cls, path: Path | None = None) -> "UserSettings":
        p = path or settings_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def save(self, path: Path | None = None) -> None:
        p = path or settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def organizer(self):
        from iphone_backup.organizer.planner import OrganizerSettings
        template = ALBUM_TEMPLATE if self.by_album else self.template
        return OrganizerSettings(template=template, use_gps=self.use_gps, max_detail=PlaceLevel[self.max_detail])


# --------------------------------------------------------------------------- datos para las pantallas
@dataclass
class DeviceState:
    connected: bool
    state: str                       # ok | not_found | trust | locked | error
    message: str
    model: str | None = None
    ios: str | None = None


@dataclass
class ScanReport:
    items: int = 0                   # fotos + vídeos (los .AAE acompañan a su foto)
    photos: int = 0
    videos: int = 0
    live_photos: int = 0
    total_bytes: int = 0
    with_gps: int = 0
    without_gps: int = 0
    unknown_date: int = 0
    unreadable: int = 0              # no legibles en el listado + sin metadatos legibles
    unreadable_detail: list[str] = field(default_factory=list)
    ignored: int = 0
    free_bytes: int | None = None
    required_bytes: int = 0
    space_ok: bool = True
    space_message: str = ""
    fs_name: str | None = None
    oversized: list[str] = field(default_factory=list)
    preview_lines: list[str] = field(default_factory=list)
    sample: list[tuple[str, str, str]] = field(default_factory=list)   # (archivo, carpeta destino, fecha/fuente)
    cached_metadata: int = 0
    # --- cruce con lo ya respaldado (lo único que le importa al usuario: ¿qué falta respaldar?)
    compared: bool = False                    # False si no hay destino elegido: no se puede saber
    pending_files: int = 0
    pending_bytes: int = 0
    reasons: list[tuple[str, int, int]] = field(default_factory=list)   # (motivo, archivos, bytes)
    backed_up: int = 0
    backed_up_manifest: int = 0               # ya copiados por esta herramienta
    backed_up_library: int = 0                # ya existían en la biblioteca del destino (mismo contenido)
    backed_up_duplicate: int = 0              # contenido idéntico a otro ya respaldado
    quarantined: int = 0
    compare_notes: list[str] = field(default_factory=list)
    snapshot_saved: bool = False              # este escaneo recorrió la biblioteca y guardó su instantánea
    albums_status: str = ""                   # off | ok | cached | unavailable
    album_items: int = 0                      # elementos pendientes que pertenecen a un álbum del usuario
    favorite_items: int = 0                   # elementos pendientes marcados como favoritos


@dataclass
class FinalSummary:
    outcome: str
    detected: int
    copied: int
    skipped_existing: int
    skipped_incremental: int
    skipped_duplicate: int
    verified: int
    failed: int
    quarantined: int
    unknown_date: int
    without_gps: int
    elapsed_s: float
    message: str
    errors: list[tuple[str, str]]
    warnings: list[str]
    log_dir: str = ""


@dataclass
class PendingGroup:
    leader: str                      # ruta que identifica al elemento (la foto; en un Live Photo, la foto fija)
    name: str
    kind: str                        # photo | video | live | other
    members: list[str]               # el elemento y sus acompañantes (vídeo del Live Photo, .AAE)
    size: int
    reason: str
    capture_dt: str | None = None
    album: str | None = None         # álbum del iPhone donde se guardará (raíz de la carpeta)
    favorite: bool = False


def _neg(s: str | None) -> str:
    """Clave para ordenar fechas de más reciente a más antigua sin parsear (formato ISO)."""
    return "" if s is None else "".join(chr(0x10FFFF - ord(c)) for c in s)


class ScanInvalid(Exception):
    """No se puede iniciar el respaldo con el estado actual (sin escaneo, destino inválido, sin espacio…)."""


class Workflow:
    def __init__(self, settings: UserSettings, *, simulate: Path | None = None,
                 source_factory: Callable[[], DeviceSource] | None = None):
        self.settings = settings
        self._simulate = simulate
        self._factory = source_factory
        self.source: DeviceSource | None = None
        self.plan = None
        self.report: ScanReport | None = None
        self._lock = threading.Lock()
        self.control = None
        self._existing = None                    # coincidencias con la biblioteca existente (del último escaneo)
        self._pending_paths: set[str] = set()
        self._listing = None                     # listado de la biblioteca usado/generado por el último escaneo
        self._pending_reason: dict[str, str] = {}
        self._albums = None                      # AlbumIndex del último escaneo (None si no hay / no se pudo leer)
        self.selected: set[str] | None = None    # None = respaldar todo lo pendiente

    # ------------------------------------------------------------------ conexión
    def _open(self) -> DeviceSource:
        if self._factory:
            return self._factory()
        if self._simulate:
            from iphone_backup.device.simulated import SimulatedDeviceSource
            return SimulatedDeviceSource(self._simulate)
        from iphone_backup.device.afc_source import AfcDeviceSource
        return AfcDeviceSource().connect()

    def disconnect(self) -> None:
        src, self.source = self.source, None
        close = getattr(src, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001 - cerrar nunca debe romper la interfaz
                pass

    def connect(self) -> DeviceState:
        self.disconnect()
        self.plan = self.report = None                  # un escaneo previo ya no es válido con otra conexión
        try:
            self.source = self._open()
            info = self.source.info()
        except DeviceError as e:
            self.source = None
            kind = ("trust" if isinstance(e, TrustRequired) else "locked" if isinstance(e, DeviceLocked)
                    else "not_found" if isinstance(e, DeviceNotFound) else "error")
            return DeviceState(False, kind, friendly(e))
        return DeviceState(True, "ok", "Conectado y autorizado", info.product_type, info.ios_version)

    def snapshot_info(self):
        """Instantánea guardada de ESTE iPhone para el destino actual (None si no hay o no es válida), y si
        ya es hora de sugerir un escaneo completo."""
        from iphone_backup.app.snapshots import SnapshotStore
        from iphone_backup.transfer.destination import Destination
        if self.source is None or not self.settings.dest:
            return None
        try:
            udid = self.source.info().udid
            return SnapshotStore(Destination(Path(self.settings.dest)).manifest_dir).info(udid, self.settings.dest)
        except DeviceError:
            return None

    def is_stale(self, snap, now=None) -> bool:
        return snap is None or snap.age_hours(now) >= self.settings.full_scan_after_hours

    # ------------------------------------------------------------------ escaneo (solo lectura)
    def _geocoder(self):
        if not self.settings.use_gps:
            return None
        from iphone_backup.geolocation.offline import OfflineGeocoder
        try:
            return OfflineGeocoder(language="es", max_detail=PlaceLevel[self.settings.max_detail],
                                   parent_radius_km=self.settings.merge_radius_km)
        except FileNotFoundError:
            return None

    def _filter(self, plan):
        """Aplica 'incluir/excluir tipos' dejando fuera del plan los grupos que el usuario no quiere."""
        s = self.settings
        if s.include_photos and s.include_videos:
            return plan
        drop = set()
        for path in plan.plans:
            kind = plan.scan.kinds.get(path)
            leader_kind = plan.scan.kinds.get(plan.plans[path].leader or path, kind)
            if leader_kind is FileKind.PHOTO and not s.include_photos:
                drop.add(path)
            elif leader_kind is FileKind.VIDEO and not s.include_videos:
                drop.add(path)
        return replace(plan, plans={p: v for p, v in plan.plans.items() if p not in drop})

    REASONS = {
        "new": "Nuevos: todavía no están en tu respaldo",
        "retry": "No se pudieron copiar la vez anterior (se reintentan)",
        "interrupted": "Quedaron a medias en un intento anterior",
        "changed": "Cambiaron en el iPhone desde el último respaldo",
    }

    PHASES = [
        ("albums", "Leer tus álbumes y favoritos del iPhone"),
        ("device", "Leer tu iPhone (fotos, vídeos y fechas)"),
        ("manifest", "Comparar con lo que esta herramienta ya respaldó"),
        ("library", "Buscar en tu biblioteca existente del destino"),
        ("compare", "Confirmar con el iPhone, sin copiar nada"),
    ]

    def _crosscheck(self, plan, r: ScanReport, files: dict, stage: Callable[[str], None],
                    phase: Callable[[str, int, int, str, bool], None], mode: str = "full") -> set[str]:
        """Fase final del escaneo: cruza lo del iPhone con lo ya respaldado (manifiesto + biblioteca existente del
        destino) y deja en ``r`` solo lo que de verdad falta. Devuelve las rutas que NO hay que copiar.
        Escribe únicamente estado propio en ``_Manifest`` (índice de hashes, reutilizable en la siguiente vez);
        no toca ni el iPhone ni los archivos de la biblioteca."""
        from iphone_backup.app.snapshots import SnapshotStore
        from iphone_backup.database.manifest import Manifest
        from iphone_backup.library.external_index import ExternalIndex, analyze_existing
        from iphone_backup.transfer.destination import Destination
        from iphone_backup.transfer.engine import EXISTING_NOTE

        stage("Comparando con lo que ya tienes respaldado…")
        dest = Destination(Path(self.settings.dest))
        dest.prepare()
        mf = Manifest(dest.manifest_path)
        self._existing = None
        phase("manifest", 0, 0, "Leyendo el registro del respaldo…", False)
        try:
            states = mf.device_states(self.source.info().udid)
            pending: dict[str, str] = {}
            for p in sorted(plan.plans):
                f = files[p]
                known = states.get(p, [])
                exact = next((st for sz, mt, st, note in known if sz == f.size and mt == f.mtime_ns), None)
                note = next((n for sz, mt, st, n in known if sz == f.size and mt == f.mtime_ns), None)
                if exact == "committed":
                    r.backed_up_manifest += 1
                elif exact == "skipped_dup":
                    if (note or "").startswith(EXISTING_NOTE):
                        r.backed_up_library += 1
                    else:
                        r.backed_up_duplicate += 1
                elif exact == "quarantined":
                    r.quarantined += 1
                elif exact == "failed":
                    pending[p] = "retry"
                elif exact is not None:
                    pending[p] = "interrupted"
                elif any(st == "committed" for _, _, st, _ in known):
                    pending[p] = "changed"
                else:
                    pending[p] = "new"
            phase("manifest", len(plan.plans), len(plan.plans),
                  f"{len(plan.plans) - len(pending)} ya respaldados, {len(pending)} por confirmar", True)
            info = self.source.info()
            store = SnapshotStore(dest.manifest_dir)
            snap = store.info(info.udid, str(dest.base))
            listing = store.load_listing(info.udid) if (mode == "quick" and snap) else None
            self._listing = None
            if mode == "full" or pending:          # un escaneo completo siempre recorre y guarda el listado
                stage("Buscando en tu biblioteca existente lo que ya está respaldado (solo lectura)…")
                index = ExternalIndex(dest.base, mf, exclude_paths=[dest.library])
                if listing is not None:
                    phase("library", 0, 0, "Usando el índice guardado…", False)
                else:
                    phase("library", 0, 0, "Recorriendo carpetas…", False)
                index.build({files[p].size for p in pending},
                            progress=lambda i, n: phase("library", i, n, f"Calculando huellas {i} de {n}", False),
                            walk_progress=lambda n: phase("library", 0, 0, f"Recorriendo carpetas: {n:,} archivos vistos", False),
                            listing=listing)
                st = index.stats
                self._listing = index.listing
                if listing is not None:
                    phase("library", 1, 1, f"Índice guardado hace {snap.age_hours():.0f} h: {st.scanned:,} archivos "
                                           "(no se recorrió el disco)", True)
                    r.compare_notes.append(f"Se usó el índice de tu biblioteca guardado hace {snap.age_hours():.0f} h; "
                                           "lo agregado a tu biblioteca después no se tiene en cuenta hasta un escaneo completo.")
                else:
                    store.save(info.udid, info.product_type, str(dest.base), len(files), index.listing)
                    phase("library", 1, 1, f"{st.scanned:,} archivos recorridos; {st.cache_hits} huellas ya guardadas, "
                                           f"{st.hashed} calculadas ahora", True)
                    r.snapshot_saved = True
            if pending:
                stage(f"Comparando {len(pending)} archivos con el iPhone (sin copiarlos)…")
                ex = analyze_existing(self.source, [files[p] for p in pending], index,
                                      progress=lambda i, n: phase("compare", i, n, f"{i} de {n} archivos", False))
                phase("compare", 1, 1, f"{len(ex.matches)} ya estaban en tu biblioteca", True)
                self._existing = ex
                for p in list(pending):
                    if p in ex.matches:
                        del pending[p]
                        r.backed_up_library += 1
                if ex.unavailable_hash:
                    r.compare_notes.append(f"{ex.unavailable_hash} archivos sin hash del iPhone: se compararán tras copiarlos.")
            else:
                if self._listing is None:
                    phase("library", 1, 1, "No hace falta: todo está ya registrado", True)
                phase("compare", 1, 1, "No hace falta", True)
        finally:
            mf.close()
        r.compared = True
        r.pending_files = len(pending)
        r.pending_bytes = sum(files[p].size for p in pending)
        for code, label in self.REASONS.items():
            paths = [p for p, why in pending.items() if why == code]
            if paths:
                r.reasons.append((label, len(paths), sum(files[p].size for p in paths)))
        r.backed_up = r.backed_up_manifest + r.backed_up_library + r.backed_up_duplicate
        if r.quarantined:
            r.compare_notes.append(f"{r.quarantined} archivos en cuarentena desde antes: requieren tu decisión.")
        self._pending_paths = set(pending)
        self._pending_reason = dict(pending)
        return set(plan.plans) - set(pending)

    # ------------------------------------------------------------------ elegir qué respaldar (galería)
    def pending_groups(self) -> list[PendingGroup]:
        """Lo pendiente agrupado como lo ve el usuario: un Live Photo (foto + vídeo + .AAE) es UN elemento.
        Más recientes primero; sin fecha al final."""
        from iphone_backup.organizer.planner import group_leaders
        plan = self.plan
        if plan is None or not self._pending_reason:
            return []
        files = {f.path: f for f in plan.scan.files}
        leaders = group_leaders(plan.scan, plan.confirmed_pairs)
        groups: dict[str, PendingGroup] = {}
        for p, why in self._pending_reason.items():
            lead = leaders.get(p) or p
            g = groups.get(lead)
            if g is None:
                lf = files.get(lead)
                kind = ("video" if plan.scan.kinds.get(lead) is FileKind.VIDEO else
                        "photo" if plan.scan.kinds.get(lead) is FileKind.PHOTO else "other")
                m = plan.metas.get(lead)
                lp = plan.plans.get(lead)
                g = groups[lead] = PendingGroup(lead, lf.name if lf else lead.rsplit("/", 1)[-1], kind, [], 0,
                                                self.REASONS.get(why, why), (m.capture_dt if m else None),
                                                lp.album if lp else None,
                                                bool(self._albums and self._albums.is_favorite(lead)))
            g.members.append(p)
            g.size += files[p].size
            if any(p == a or p == b for a, b in plan.confirmed_pairs):
                g.kind = "live"
        return sorted(groups.values(), key=lambda g: (g.capture_dt is None, _neg(g.capture_dt), g.name))

    def set_selection(self, leaders: set[str] | None) -> None:
        """``None`` = todo lo pendiente. Un subconjunto = solo esos elementos (los demás se quedan pendientes y
        volverán a aparecer en el siguiente escaneo; no se marcan ni se ocultan)."""
        self.selected = None if leaders is None else set(leaders)

    def selection_totals(self) -> tuple[int, int]:
        """(archivos, bytes) que se copiarían con la selección actual."""
        groups = self.pending_groups()
        if self.selected is not None:
            groups = [g for g in groups if g.leader in self.selected]
        return sum(len(g.members) for g in groups), sum(g.size for g in groups)

    def _selected_plan(self):
        """El plan que se entrega al motor: todo lo ya respaldado + SOLO los grupos pendientes elegidos."""
        plan = self.plan
        if self.selected is None:
            return plan
        keep = set(plan.plans) - set(self._pending_reason)
        for g in self.pending_groups():
            if g.leader in self.selected:
                keep.update(g.members)
        return replace(plan, plans={p: v for p, v in plan.plans.items() if p in keep})

    def scan(self, progress: Callable[[int, int], None] | None = None,
             stage: Callable[[str], None] | None = None,
             phase: Callable[[str, int, int, str, bool], None] | None = None,
             mode: str = "full") -> ScanReport:
        """Lee metadatos, construye el plan y lo cruza con lo ya respaldado, todo en un único escaneo. NO modifica
        el iPhone ni la biblioteca; en el destino solo guarda estado propio (caché de metadatos e índice).

        ``mode="full"``: recorre la biblioteca del destino y guarda su listado como instantánea de este iPhone.
        ``mode="quick"``: reutiliza el listado guardado (si lo hay y es del mismo destino) y se salta el recorrido.
        El listado del iPhone y el registro del respaldo SIEMPRE se leen de nuevo: así se detecta lo nuevo.

        ``phase(clave, hecho, total, detalle, terminada)`` informa cada fase de ``PHASES`` con su propio avance
        (total 0 = no se conoce: barra indeterminada)."""
        stage = stage or (lambda _m: None)
        phase = phase or (lambda *a: None)
        if self.source is None:
            raise ScanInvalid("Primero conecta el iPhone.")
        stage("Leyendo álbumes y favoritos del iPhone (solo lectura)…")
        albums_index, album_notes, albums_status = self._read_albums(mode, phase)
        self._albums = albums_index
        album_map = {p: albums_index.primary(p) for p in albums_index.albums} if albums_index else None
        stage("Leyendo el iPhone (solo lectura)…")
        phase("device", 0, 0, "Listando el iPhone…", False)

        def device_progress(i: int, n: int) -> None:
            phase("device", i, n, f"{i} de {n} archivos", False)
            if progress:
                progress(i, n)
        from iphone_backup.app.planning import build_plan
        from iphone_backup.metadata.exiftool_service import ExifToolService
        from iphone_backup.transfer.space import check_space, filesystem_name, oversized_for_fs, required_bytes

        if self.source is None:
            raise ScanInvalid("Primero conecta el iPhone.")
        s = self.settings
        cache_path = self._cache_path()
        cache: dict = {}
        if cache_path:
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
        cached0 = len(cache)
        try:
            with ExifToolService() as et:
                plan = build_plan(self.source, et, self._geocoder(), s.organizer(), progress=device_progress,
                                  tag_cache=cache, albums=album_map)
        finally:
            if cache_path and len(cache) != cached0:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(cache), encoding="utf-8")
                    tmp.replace(cache_path)
                except OSError:
                    pass
        plan = self._filter(plan)
        phase("device", len(plan.plans), len(plan.plans),
              f"{len(plan.plans)} archivos leídos ({cached0} metadatos ya estaban guardados)", True)
        sc = plan.scan
        planned = set(plan.plans)
        files = {f.path: f for f in sc.files}
        media = [p for p in planned if sc.kinds.get(p) in (FileKind.PHOTO, FileKind.VIDEO)]
        r = ScanReport()
        r.albums_status = albums_status
        r.compare_notes.extend(album_notes)
        r.photos = sum(1 for p in media if sc.kinds[p] is FileKind.PHOTO)
        r.videos = sum(1 for p in media if sc.kinds[p] is FileKind.VIDEO)
        r.items = r.photos + r.videos
        r.live_photos = len(plan.confirmed_pairs)
        sizes = {p: files[p].size for p in planned if p in files}
        r.total_bytes = sum(sizes.values())
        r.with_gps = sum(1 for p in media if plan.metas.get(p) and plan.metas[p].gps_lat is not None)
        r.without_gps = len(media) - r.with_gps
        r.unknown_date = sum(1 for p in planned if plan.plans[p].date_unknown)
        r.unreadable = len(sc.unreadable) + len(plan.probe_errors)
        r.unreadable_detail = [f"{p.rsplit('/', 1)[-1]}: {m}" for p, m in (list(sc.unreadable) + list(plan.probe_errors.items()))[:200]]
        r.ignored = int(sc.summary().get("ignored", 0))
        r.preview_lines = list(plan.preview.lines) if plan.preview else []
        r.cached_metadata = cached0
        r.sample = self._sample(plan, 40)
        skip: set[str] = set()
        if s.dest and Path(s.dest).exists():
            skip = self._crosscheck(plan, r, files, stage, phase, mode)
        else:
            for key, _label in self.PHASES[1:]:
                phase(key, 1, 1, "Elige un destino para poder comparar", True)
        if s.dest:
            base = Path(s.dest)
            need = required_bytes({p: files[p].size for p in planned if p in files}, planned,
                                  already_backed_up=skip) if r.compared else \
                required_bytes({p: files[p].size for p in planned if p in files}, planned)
            r.required_bytes = need
            rep = check_space(base, need)
            r.free_bytes, r.space_ok, r.space_message = rep.free, rep.ok, rep.message()
            r.fs_name = filesystem_name(base)
            r.oversized = oversized_for_fs(r.fs_name, sizes)
        self.plan, self.report = plan, r
        self.selected = None                     # un escaneo nuevo parte de "todo pendiente"
        groups = self.pending_groups()
        r.album_items = sum(1 for g in groups if g.album)
        r.favorite_items = sum(1 for g in groups if g.favorite)
        return r

    def _read_albums(self, mode: str, phase):
        """Fase 1 del escaneo: álbumes y favoritos. Si no se pueden leer, el flujo sigue SIN álbumes y lo avisa."""
        from iphone_backup.app.albums import AlbumsUnavailable, load_albums
        from iphone_backup.app.snapshots import SnapshotStore
        from iphone_backup.transfer.destination import Destination
        s = self.settings
        if not s.by_album:
            phase("albums", 1, 1, "Desactivado en Configuración", True)
            return None, [], "off"
        store = udid = None
        if s.dest and Path(s.dest).exists():
            store = SnapshotStore(Destination(Path(s.dest)).manifest_dir)
            udid = self.source.info().udid
            snap = store.info(udid, s.dest)
            if mode == "quick" and snap:
                cached = store.load_albums(udid)
                if cached is not None:
                    phase("albums", 1, 1, f"Usando los álbumes guardados hace {snap.age_hours():.0f} h: "
                                          f"{cached.user_albums} álbumes, {len(cached.favorites)} favoritos", True)
                    return cached, [f"Álbumes y favoritos de hace {snap.age_hours():.0f} h; los cambios recientes "
                                    "se verán en un escaneo completo."], "cached"
        phase("albums", 0, 0, "Copiando la base de Fotos del iPhone (puede tardar unos segundos)…", False)
        try:
            idx = load_albums(self.source)
        except (DeviceDisconnected, DeviceLocked, DeviceNotFound, TrustRequired):
            raise
        except (AlbumsUnavailable, DeviceError) as e:
            phase("albums", 1, 1, "No disponible: se guardará sin álbumes", True)
            return None, [f"No se pudieron leer los álbumes ({e}). Se usa la estructura sin álbum."], "unavailable"
        if store and udid:
            try:
                store.save_albums(udid, idx)
            except OSError:
                pass
        phase("albums", 1, 1, f"{idx.user_albums} álbumes, {len(idx.favorites)} favoritos", True)
        return idx, [], "ok"

    def _sample(self, plan, n: int) -> list[tuple[str, str, str]]:
        keys = sorted(plan.plans)
        step = max(1, len(keys) // n)
        out = []
        for p in keys[::step][:n]:
            m = plan.metas.get(p)
            fp = plan.plans[p]
            when = (m.capture_dt.replace("T", " ") + f" ({m.date_source.value})") if m and m.capture_dt else "sin fecha de captura"
            out.append((p.rsplit("/", 1)[-1], "/".join(fp.rel_dir), when))
        return out

    def _cache_path(self) -> Path | None:
        if not self.settings.dest:
            return None
        from iphone_backup.transfer.destination import Destination
        d = Destination(Path(self.settings.dest))
        return d.manifest_dir / "metadata_cache.json" if d.manifest_dir.parent.exists() else None

    # ------------------------------------------------------------------ respaldo
    def can_start(self) -> tuple[bool, str]:
        if self.source is None:
            return False, "Conecta el iPhone."
        if self.plan is None or self.report is None:
            return False, "Escanea el iPhone antes de iniciar el respaldo."
        if not self.settings.dest:
            return False, "Elige el disco de destino."
        if not Path(self.settings.dest).exists():
            return False, "El destino no está disponible."
        if not self.report.space_ok:
            return False, "No hay espacio suficiente en el destino."
        if self.report.oversized:
            return False, "Hay archivos que superan el límite del sistema de archivos del destino."
        if not self.plan.plans:
            return False, "No hay nada que respaldar con los filtros actuales."
        if self.report.compared and self.report.pending_files == 0:
            return False, "Todo lo de tu iPhone ya está respaldado: no hay nada que copiar."
        if self.selected is not None and not self.selected:
            return False, "No marcaste nada para respaldar."
        return True, ""

    def run_backup(self, on_event: Callable[..., None], control=None,
                   on_stage: Callable[[str], None] | None = None) -> FinalSummary:
        from iphone_backup.applog import setup_logging
        from iphone_backup.database.manifest import Manifest
        from iphone_backup.metadata.exiftool_service import ExifToolService
        from iphone_backup.metadata.extractor import exiftool_extractor
        from iphone_backup.transfer.destination import Destination
        from iphone_backup.transfer.engine import BackupEngine, EngineConfig, EngineControl, SessionOutcome

        ok, why = self.can_start()
        if not ok:
            raise ScanInvalid(why)
        stage = on_stage or (lambda _m: None)
        s, plan = self.settings, self._selected_plan()       # solo lo que el usuario dejó marcado
        self.control = control or EngineControl()
        dest = Destination(Path(s.dest))
        dest.prepare()
        setup_logging(dest.logs_dir, s.log_level)
        manifest = Manifest(dest.manifest_path)
        try:
            from iphone_backup.library.external_index import ExternalIndex
            # el cruce con la biblioteca ya se hizo en el escaneo: aquí solo se reutiliza su resultado; el índice
            # (con los hashes en caché) se necesita para detectar duplicados de contenido durante la copia
            stage("Preparando el índice de tu biblioteca (usa la caché del escaneo)…")
            index = ExternalIndex(dest.base, manifest, exclude_paths=[dest.library])
            by_path = {f.path: f for f in plan.scan.files}
            index.build({by_path[p].size for p in self._pending_paths if p in by_path}, listing=self._listing)
            existing = self._existing
            stage("Copiando y verificando…")
            with ExifToolService() as et:
                engine = BackupEngine(self.source, manifest, dest, plan, settings=s.organizer(),
                                      geocoder=self._geocoder(), extractor=exiftool_extractor(et),
                                      config=EngineConfig(workers=s.workers), control=self.control,
                                      on_event=on_event, existing=existing, external_index=index,
                                      albums=self._albums)
                res = engine.run()
        finally:
            manifest.close()
        rep = self.report
        done = res.outcome is SessionOutcome.COMPLETED
        return FinalSummary(
            outcome=res.outcome.value, detected=res.detected, copied=res.copied,
            skipped_existing=res.skipped_existing, skipped_incremental=res.skipped_incremental,
            skipped_duplicate=res.skipped_duplicate, verified=res.verified_full + res.verified_dest_only,
            failed=res.failed, quarantined=res.quarantined,
            unknown_date=rep.unknown_date if done else 0, without_gps=rep.without_gps if done else 0,
            elapsed_s=res.elapsed_s, message=res.message, errors=list(res.errors), warnings=list(res.warnings),
            log_dir=str(dest.logs_dir))

    # ------------------------------------------------------------------ miniaturas
    def thumbnail_service(self):
        """Servicio de miniaturas de este iPhone; la caché vive en ``_Manifest/thumbs`` del destino."""
        from iphone_backup.app.thumbnails import ThumbnailService
        from iphone_backup.transfer.destination import Destination
        cache = (Destination(Path(self.settings.dest)).manifest_dir / "thumbs") if self.settings.dest else None
        udid = ""
        try:
            udid = self.source.info().udid
        except DeviceError:
            pass
        return ThumbnailService(self.source, cache, udid)

    # ------------------------------------------------------------------ historial
    def history(self) -> list[dict]:
        """Sesiones anteriores, leídas del manifiesto en solo lectura."""
        import sqlite3
        if not self.settings.dest:
            return []
        from iphone_backup.transfer.destination import Destination
        mp = Destination(Path(self.settings.dest)).manifest_path
        if not mp.is_file():
            return []
        con = sqlite3.connect(f"file:{mp.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id, started_at, finished_at, status FROM sessions ORDER BY id DESC").fetchall()
            counts = {r["session_id"]: r for r in con.execute(
                "SELECT session_id, SUM(copy_status='committed') ok, SUM(copy_status='failed') bad, "
                "COALESCE(SUM(CASE WHEN copy_status='committed' THEN size END),0) b FROM items GROUP BY session_id")}
            return [{"id": r["id"], "started": r["started_at"], "finished": r["finished_at"], "status": r["status"],
                     "copied": (counts[r["id"]]["ok"] if r["id"] in counts else 0) or 0,
                     "failed": (counts[r["id"]]["bad"] if r["id"] in counts else 0) or 0,
                     "bytes": (counts[r["id"]]["b"] if r["id"] in counts else 0) or 0} for r in rows]
        finally:
            con.close()
