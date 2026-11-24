"""Motor de transferencia: copia verificada, incremental, recuperable y con pipeline.

Por archivo:  leer del iPhone  ‖  escribir en _Staging/*.part + SHA-256 + SHA-1   (dos hilos, solapados)  ->
              comparar SHA-1 con el del dispositivo  ->  releer el .part y comparar SHA-256  ->
              [hilo principal, EN ORDEN] metadatos definitivos -> nombre sin colisión -> manifiesto (intención)
              -> renombrar (sin sobrescribir) -> manifiesto (COMMITTED, diferido al siguiente grupo).

Concurrencia: ``workers`` archivos se copian/verifican a la vez (lecturas AFC concurrentes), pero la
confirmación (nombres, manifiesto, renombrado) la hace SIEMPRE el hilo principal en orden de envío, de modo que
los nombres con colisión son deterministas y el manifiesto (SQLite) no se comparte entre hilos.

Garantías (sin cambios):
  * El iPhone solo se LEE (puerto DeviceSource).
  * Nunca se sobrescribe un archivo existente; las colisiones se resuelven con un sufijo determinista.
  * Un archivo solo aparece en el destino final tras verificarse; los .part viven en _Staging.
  * COMMITTED solo tras el renombrado. Una transacción por grupo (la intención —ruta final, hashes, estado
    VERIFIED— se guarda ANTES de renombrar y los COMMITTED se difieren a la siguiente transacción); si el proceso
    cae en medio, el siguiente arranque reconcilia (sin duplicar) con ``pending_commit_items``.
  * Desconexión, disco lleno o cancelación detienen la sesión de forma segura y recuperable.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import queue
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from iphone_backup.applog import get_user_logger
from iphone_backup.database.manifest import Manifest
from iphone_backup.domain.filetypes import FileKind, classify
from iphone_backup.domain.models import DeviceFile, ItemStatus, SessionStatus, VerifyStatus
from iphone_backup.metadata.policy import MediaMetadata
from iphone_backup.organizer.collisions import NameResolutionError, resolve_group_names
from iphone_backup.organizer.naming import safe_join, sanitize_filename
from iphone_backup.organizer.planner import FolderResolver, OrganizerSettings
from iphone_backup.ports.device import (
    CHUNK_SIZE, DeviceDisconnected, DeviceError, DeviceLocked, DeviceNotFound, DeviceReadError, DeviceSource,
    TrustRequired, iter_chunks,
)
from iphone_backup.ports.geocoder import Geocoder
from iphone_backup.transfer.destination import Destination
from iphone_backup.transfer.fsops import RealFs
from iphone_backup.transfer.space import MIN_RESERVE, check_space, filesystem_name, has_room_for, oversized_for_fs

log = logging.getLogger("iphone_backup.transfer")

Extractor = Callable[[Path, DeviceFile, FileKind], MediaMetadata]

EXISTING_NOTE = "Ya existe en la biblioteca"
EXTERNAL_PREFIX = "@ext/"       # rel_path que apunta a un archivo EXISTENTE fuera de iPhone_Photos


class SessionOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEVICE_LOST = "device_lost"            # desconexión / bloqueo / sin confianza: reintentar
    DESTINATION_ERROR = "destination_error"  # disco lleno o error de escritura
    NO_SPACE = "no_space"                  # comprobación previa: no se copió nada
    BLOCKED = "blocked"                    # destino no reconocido u otra condición previa


class Cancelled(Exception):
    pass


class StopSession(Exception):
    def __init__(self, outcome: SessionOutcome, message: str):
        super().__init__(message)
        self.outcome, self.message = outcome, message


class HashMismatch(Exception):
    """El SHA-1 que calcula el iPhone no coincide con lo recibido (corrupción en tránsito)."""


class TransferCheckFailed(Exception):
    """Tamaño recibido distinto del esperado o verificación del destino fallida (reintentable)."""


@dataclass
class EngineConfig:
    reserve_bytes: int = MIN_RESERVE
    max_attempts: int = 2
    chunk_size: int = CHUNK_SIZE
    preserve_mtime: bool = True
    workers: int = 4                 # archivos copiados/verificados a la vez (1 = secuencial)
    queue_depth: int = 6             # bloques de 4 MB en vuelo entre lector y escritor de cada archivo


class EngineControl:
    """Cancelación (en cualquier momento, entre bloques) y pausa (entre archivos)."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def checkpoint(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def wait_if_paused(self) -> None:
        while not self._running.wait(0.1):
            if self._cancel.is_set():
                break
        self.checkpoint()


@dataclass
class SessionResult:
    outcome: SessionOutcome = SessionOutcome.COMPLETED
    session_id: int | None = None
    message: str = ""
    detected: int = 0
    copied: int = 0
    bytes_copied: int = 0
    skipped_incremental: int = 0     # ya respaldados (misma huella): no se leyeron del iPhone
    skipped_duplicate: int = 0       # contenido idéntico a algo ya respaldado
    skipped_existing: int = 0        # ya estaba en la biblioteca existente del usuario (no se copia)
    verified_full: int = 0           # SHA-1 del iPhone y SHA-256 del destino comprobados
    verified_dest_only: int = 0
    failed: int = 0
    quarantined: int = 0
    pending_quarantine: int = 0      # en cuarentena desde antes: requieren decisión del usuario
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def summary(self) -> str:
        return (f"{self.outcome.value}: detectados {self.detected}, copiados {self.copied} "
                f"({self.bytes_copied / 1e9:.2f} GB), omitidos {self.skipped_incremental} + duplicados "
                f"{self.skipped_duplicate} + ya existentes {self.skipped_existing}, verificados "
                f"{self.verified_full + self.verified_dest_only}, fallidos {self.failed}, "
                f"en cuarentena {self.quarantined}")


@dataclass
class _Staged:
    item_id: int
    f: DeviceFile
    part: Path
    sha256: str
    sha1: str
    verify: VerifyStatus
    meta: MediaMetadata | None = None


@dataclass
class _Job:
    item_id: int
    f: DeviceFile
    fut: Future


def _sha256_file(fs, path: Path, chunk: int) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with fs.open_read(path) as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


class BackupEngine:
    def __init__(self, source: DeviceSource, manifest: Manifest, dest: Destination, plan, *,
                 settings: OrganizerSettings | None = None, geocoder: Geocoder | None = None,
                 extractor: Extractor | None = None, config: EngineConfig | None = None,
                 control: EngineControl | None = None, on_event: Callable[..., None] | None = None, fs=None,
                 existing=None, external_index=None, albums=None):
        self.source, self.manifest, self.dest, self.plan = source, manifest, dest, plan
        self.albums = albums                    # AlbumIndex | None: álbumes del iPhone (raíz de carpetas) y favoritos
        self.existing = existing                # ExistingMatches: ya presentes en la biblioteca del usuario
        self.external_index = external_index    # ExternalIndex: comparación por SHA-256 tras copiar
        self.settings = settings or OrganizerSettings()
        self.resolver = FolderResolver(self.settings, geocoder)
        self.extractor = extractor
        self.config = config or EngineConfig()
        self.control = control or EngineControl()
        self.on_event = on_event or (lambda *a, **k: None)
        self.fs = fs or RealFs()
        self.ulog = get_user_logger()
        self.by_path: dict[str, DeviceFile] = {f.path: f for f in plan.scan.files}
        self._leader_dir: dict[str, tuple[str, ...]] = {}
        self._abort = threading.Event()                       # detiene a los trabajadores en vuelo
        self._pending_commit: list[int] = []                  # renombrados, con COMMITTED aún sin escribir
        self._recent_sha: dict[str, tuple[int, str]] = {}     # sha256 -> (item, rel) de lo pendiente de COMMITTED

    # ------------------------------------------------------------------ API
    def run(self) -> SessionResult:
        t0 = time.perf_counter()
        res = SessionResult(detected=len(self.plan.plans))
        try:
            self.dest.prepare()
            res.warnings += self.dest.bind(self.manifest.backup_id)
        except Exception as e:  # noqa: BLE001  (DestinationMismatch u OSError)
            res.outcome, res.message = SessionOutcome.BLOCKED, str(e)
            self.ulog.error("No se pudo preparar el destino: %s", e)
            return res
        try:
            info = self.source.info()
        except DeviceError as e:
            res.outcome, res.message = SessionOutcome.DEVICE_LOST, f"No se pudo leer el iPhone: {e}"
            return res
        self._recover()                      # antes de abrir la sesión: no debe marcar la nueva como interrumpida
        res.session_id = sid = self.manifest.start_session(info.udid)
        status = SessionStatus.COMPLETED
        try:
            pending = self._classify_pending(sid, info.udid, res)
            self._precheck(pending, res)
            self.on_event("start", total=len(pending), bytes=sum(self.by_path[p].size for p in pending))
            self._pipeline(sid, info.udid, pending, res)
        except Cancelled:
            res.outcome, status = SessionOutcome.CANCELLED, SessionStatus.CANCELLED
            res.message = "Cancelado por el usuario; lo ya copiado se conserva."
        except StopSession as s:
            res.outcome, res.message = s.outcome, s.message
            status = SessionStatus.INTERRUPTED if s.outcome is not SessionOutcome.NO_SPACE else SessionStatus.FAILED
            log.warning("Sesión detenida: %s (%s)", s.message, s.outcome.value)
        except BaseException:
            status = SessionStatus.FAILED      # error inesperado: se registra y se propaga
            raise
        finally:
            self._cleanup_staging()
            res.elapsed_s = round(time.perf_counter() - t0, 2)
            self.manifest.finish_session(sid, status)
            self.ulog.info("Sesión %s: %s", res.outcome.value, res.summary())
            self.on_event("finished", result=res)
        return res

    # ----------------------------------------------------------- pipeline
    def _pipeline(self, sid: int, udid: str, pending: list[str], res: SessionResult) -> None:
        """Envía grupos a los trabajadores (ventana de grupos en vuelo) y los confirma EN ORDEN en este hilo."""
        window = max(2, self.config.workers * 2)
        groups = iter(self._groups(pending))
        inflight: deque[list[_Job]] = deque()
        try:
            with ThreadPoolExecutor(max_workers=max(1, self.config.workers), thread_name_prefix="stage") as pool:
                try:
                    while True:
                        while len(inflight) < window:
                            nxt = next(groups, None)
                            if nxt is None:
                                break
                            self.control.wait_if_paused()
                            inflight.append(self._submit_group(pool, sid, udid, nxt[1]))
                        if not inflight:
                            break
                        self.control.checkpoint()        # cancelar también detiene lo ya enviado
                        self._finish_group(inflight.popleft(), res)
                except BaseException:
                    self._abort.set()        # los trabajadores en vuelo terminan en su siguiente bloque
                    for jobs in inflight:
                        for j in jobs:
                            j.fut.cancel()
                    raise
        finally:
            self._flush_commits()            # lo ya renombrado queda confirmado también al parar o cancelar

    def _checkpoint(self) -> None:
        self.control.checkpoint()
        if self._abort.is_set():
            raise Cancelled()

    def _submit_group(self, pool: ThreadPoolExecutor, sid: int, udid: str, members: list[str]) -> list[_Job]:
        jobs = []
        for path in members:
            f = self.by_path[path]
            item_id = self.manifest.upsert_discovered(sid, udid, f)[0]
            if self.manifest.get_item(item_id)["copy_status"] == ItemStatus.FAILED.value:
                self.manifest.transition(item_id, ItemStatus.DISCOVERED)
            self.on_event("file_start", item=item_id, path=f.name, size=f.size)
            jobs.append(_Job(item_id, f, pool.submit(self._stage_with_retries, sid, item_id, f)))
        return jobs

    # ------------------------------------------------------- recuperación
    def _recover(self) -> None:
        """Tras un cierre inesperado: limpia staging propio y reconcilia elementos VERIFIED."""
        self.manifest.recover_interrupted()
        self._cleanup_staging()
        for row in self.manifest.pending_commit_items():
            rel = row["rel_path"]
            ok = False
            if rel:
                target = self.dest.library.joinpath(*rel.split("/"))
                if self.fs.exists(target) and self.fs.getsize(target) == row["size"]:
                    digest, _ = _sha256_file(self.fs, target, self.config.chunk_size)
                    ok = digest == row["sha256"]
            if ok:
                self.manifest.transition(row["id"], ItemStatus.COMMITTED)
                self.ulog.info("Recuperado tras interrupción: %s", rel)
            else:
                self.manifest.reset_to_discovered(row["id"])

    def _cleanup_staging(self) -> None:
        for name in self.fs.listdir(self.dest.staging_dir):
            if name.endswith(".part"):      # solo nuestros temporales
                self.fs.remove(self.dest.staging_dir / name)

    # ----------------------------------------------------------- previos
    def _classify_pending(self, sid: int, udid: str, res: SessionResult) -> list[str]:
        pending: list[str] = []
        with self.manifest.transaction():                 # un solo fsync para registrar todos los elementos
            for path in sorted(self.plan.plans):
                f = self.by_path[path]
                item_id, _ = self.manifest.upsert_discovered(sid, udid, f)
                row = self.manifest.get_item(item_id)
                st = row["copy_status"]
                if st == ItemStatus.COMMITTED.value:
                    res.skipped_incremental += 1
                elif st == ItemStatus.SKIPPED_DUP.value:
                    if (row["error"] or "").startswith(EXISTING_NOTE):
                        res.skipped_existing += 1
                    else:
                        res.skipped_duplicate += 1
                elif st == ItemStatus.QUARANTINED.value:
                    res.pending_quarantine += 1
                elif self.existing is not None and path in self.existing.matches:
                    self._skip_existing(item_id, f, self.existing.matches[path], res)
                else:
                    pending.append(path)
        return pending

    def _skip_existing(self, item_id: int, f: DeviceFile, rel: str, res: SessionResult) -> None:
        """Ya está en la biblioteca del usuario: no se copia ni se toca. Estado SKIPPED_DUP con la ruta existente."""
        if self.manifest.get_item(item_id)["copy_status"] == ItemStatus.FAILED.value:
            self.manifest.transition(item_id, ItemStatus.DISCOVERED)
        self.manifest.transition(item_id, ItemStatus.SKIPPED_DUP, rel_path=f"{EXTERNAL_PREFIX}{rel}",
                                 error=f"{EXISTING_NOTE}: {rel}")
        res.skipped_existing += 1
        self.ulog.info("Ya existía en la biblioteca, no se copia: %s", f.name)

    def _precheck(self, pending: list[str], res: SessionResult) -> None:
        sizes = {p: self.by_path[p].size for p in pending}
        need = sum(sizes.values())
        report = check_space(self.dest.base, need, self.config.reserve_bytes)
        self.ulog.info(report.message())
        if not report.ok:
            raise StopSession(SessionOutcome.NO_SPACE, report.message())
        big = oversized_for_fs(filesystem_name(self.dest.base), sizes)
        if big:
            raise StopSession(SessionOutcome.NO_SPACE,
                              f"{len(big)} archivos superan el tamaño máximo del sistema de archivos del destino.")

    def _groups(self, pending: list[str]):
        g: dict[str, list[str]] = defaultdict(list)
        for p in pending:
            plan = self.plan.plans[p]
            g[plan.leader if plan.leader else p].append(p)
        for key in sorted(g):
            yield key, sorted(g[key], key=lambda p: (p != key, p))

    # ------------------------------------------------------------ grupo
    def _finish_group(self, jobs: list[_Job], res: SessionResult) -> None:
        """Hilo principal: recoge los resultados de los trabajadores, detecta duplicados y confirma el grupo."""
        staged: list[_Staged] = []
        for job in jobs:
            f, item_id = job.f, job.item_id
            try:
                st = job.fut.result()
            except Cancelled:
                raise
            except (DeviceDisconnected, DeviceLocked, DeviceNotFound, TrustRequired) as e:
                raise StopSession(SessionOutcome.DEVICE_LOST, f"Conexión con el iPhone perdida: {e}") from e
            except OSError as e:
                raise StopSession(SessionOutcome.DESTINATION_ERROR, self._os_message(e)) from e
            except HashMismatch as e:
                self._quarantine(item_id, f, str(e), res)
                continue
            except (DeviceReadError, TransferCheckFailed) as e:
                self._fail(item_id, f, str(e), res)
                continue
            # duplicado por contenido (ya confirmado, pendiente de confirmar, o ya presente en la biblioteca)
            dup = self.manifest.find_committed_by_sha256(st.sha256)
            dup_item, dup_rel = (dup["id"], dup["rel_path"]) if dup is not None else self._recent_sha.get(st.sha256, (None, None))
            ext_rel = self.external_index.find_by_sha256(st.sha256) if self.external_index else None
            if ext_rel is not None:
                with self.manifest.transaction():
                    self.manifest.transition(item_id, ItemStatus.STAGED, sha256=st.sha256, sha1_source=st.sha1)
                    self.manifest.transition(item_id, ItemStatus.SKIPPED_DUP, rel_path=f"{EXTERNAL_PREFIX}{ext_rel}",
                                             error=f"{EXISTING_NOTE}: {ext_rel}")
                self.fs.remove(st.part)
                res.skipped_existing += 1
                self.ulog.info("Ya existía en la biblioteca (SHA-256), no se copia: %s", f.name)
                continue
            if dup_item is not None and dup_item != item_id:
                with self.manifest.transaction():
                    self.manifest.transition(item_id, ItemStatus.STAGED, sha256=st.sha256, sha1_source=st.sha1)
                    self.manifest.transition(item_id, ItemStatus.SKIPPED_DUP, rel_path=dup_rel,
                                             error=f"Contenido idéntico al elemento {dup_item}")
                self.fs.remove(st.part)
                res.skipped_duplicate += 1
                self.ulog.info("Omitido por duplicado de contenido: %s", f.name)
                continue
            staged.append(st)
        if staged:
            self._commit_group(staged, res)

    @staticmethod
    def _os_message(e: OSError) -> str:
        if e.errno == errno.ENOSPC:
            return "El disco de destino se quedó sin espacio."
        return f"Error de escritura en el destino: {e}"

    def _fail(self, item_id: int, f: DeviceFile, msg: str, res: SessionResult) -> None:
        self.manifest.transition(item_id, ItemStatus.FAILED, error=msg[:500])
        res.failed += 1
        res.errors.append((f.path, msg))
        self.ulog.warning("No se pudo copiar %s: %s", f.name, msg)
        self.on_event("file_error", item=item_id, path=f.name, message=msg)

    def _quarantine(self, item_id: int, f: DeviceFile, msg: str, res: SessionResult) -> None:
        part = self.dest.staging_dir / f"i{item_id}.mismatch"
        if self.fs.exists(part):
            qdir = self.dest.quarantine_dir
            self.fs.makedirs(qdir)
            self.fs.rename_noreplace(part, qdir / f"{item_id}__{sanitize_filename(f.name)}.quarantine")
        # DISCOVERED -> STAGED -> QUARANTINED (ruta permitida por la máquina de estados)
        with self.manifest.transaction():
            self.manifest.transition(item_id, ItemStatus.STAGED)
            self.manifest.transition(item_id, ItemStatus.QUARANTINED, error=msg[:500])
        res.quarantined += 1
        res.errors.append((f.path, msg))
        self.ulog.warning("En cuarentena (hash no coincide) %s", f.name)
        self.on_event("file_error", item=item_id, path=f.name, message=msg)

    # ---------------------------------------------------------- staging (hilos de trabajo)
    def _stage_with_retries(self, sid: int, item_id: int, f: DeviceFile) -> _Staged:
        part = self.dest.staging_dir / f"s{sid}_i{item_id}.part"
        last: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                return self._copy_and_verify(item_id, f, part)
            except HashMismatch as e:
                last = e
                if attempt == self.config.max_attempts and self.fs.exists(part):
                    self.fs.rename_noreplace(part, self.dest.staging_dir / f"i{item_id}.mismatch")
                else:
                    self.fs.remove(part)
            except (DeviceReadError, TransferCheckFailed) as e:
                last = e
                self.fs.remove(part)
            except BaseException:
                self.fs.remove(part)
                raise
            log.info("Reintento %s/%s de %s: %s", attempt, self.config.max_attempts, f.name, last)
        assert last is not None
        raise last

    def _copy_and_verify(self, item_id: int, f: DeviceFile, part: Path) -> _Staged:
        if not has_room_for(self.dest.base, f.size, self.config.reserve_bytes):
            raise StopSession(SessionOutcome.NO_SPACE, "El destino se quedó sin espacio durante la copia.")
        s256, s1, n = hashlib.sha256(), hashlib.sha1(), 0
        q: queue.Queue = queue.Queue(maxsize=max(1, self.config.queue_depth))
        errors: list[BaseException] = []
        with self.source.open_read(f.path) as h, self.fs.open_write(part) as out:
            def writer() -> None:
                """Escribe y calcula hashes mientras el lector sigue pidiendo bloques al iPhone."""
                try:
                    while True:
                        chunk = q.get()
                        if chunk is None:
                            return
                        out.write(chunk)
                        s256.update(chunk)
                        s1.update(chunk)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    while q.get() is not None:      # vacía la cola para no bloquear al lector
                        pass

            wt = threading.Thread(target=writer, name="stage-writer", daemon=True)
            wt.start()
            try:
                for chunk in iter_chunks(h, self.config.chunk_size):
                    self._checkpoint()
                    if errors:
                        break
                    q.put(chunk)
                    n += len(chunk)
                    self.on_event("bytes", item=item_id, path=f.name, done=n, total=f.size)
            finally:
                q.put(None)
                wt.join()
            if errors:
                raise errors[0]
            self.fs.fsync(out)
        if n != f.size:
            raise TransferCheckFailed(f"Tamaño recibido {n} distinto del esperado {f.size}")
        src_sha1 = self.source.source_sha1(f.path)
        if src_sha1 is not None and src_sha1.hex() != s1.hexdigest():
            raise HashMismatch("El SHA-1 calculado por el iPhone no coincide con los datos recibidos")
        # verificación del destino: releer lo escrito en disco
        digest, size_on_disk = _sha256_file(self.fs, part, self.config.chunk_size)
        if size_on_disk != f.size or digest != s256.hexdigest():
            raise TransferCheckFailed("La copia en disco no coincide con lo leído del iPhone")
        verify = VerifyStatus.VERIFIED_FULL if src_sha1 is not None else VerifyStatus.VERIFIED_DEST_ONLY
        return _Staged(item_id, f, part, digest, s1.hexdigest(), verify)

    # ---------------------------------------------------------- commit (hilo principal)
    def _extract(self, st: _Staged) -> None:
        kind, _ = classify(st.f.ext)
        if self.extractor is None or kind not in (FileKind.PHOTO, FileKind.VIDEO):
            return
        try:
            st.meta = self.extractor(st.part, st.f, kind)
        except Exception as e:  # noqa: BLE001  el archivo se respalda igual con la carpeta del plan
            log.warning("Metadatos definitivos no disponibles para %s: %s", st.f.name, e)

    def _flush_commits(self) -> None:
        """Escribe los COMMITTED diferidos (archivos ya renombrados) en UNA transacción."""
        if not self._pending_commit:
            return
        items, self._pending_commit = self._pending_commit, []
        with self.manifest.transaction():
            for item_id in items:
                self.manifest.transition(item_id, ItemStatus.COMMITTED)
        self._recent_sha.clear()

    def _commit_group(self, staged: list[_Staged], res: SessionResult) -> None:
        for st in staged:
            self._extract(st)
        # carpeta de cada archivo: el líder decide; los seguidores usan la del líder
        dirs: dict[str, tuple[str, ...]] = {}
        resolved = {}
        for st in staged:
            plan = self.plan.plans[st.f.path]
            if plan.leader == st.f.path and st.meta is not None:
                r = self.resolver.resolve(st.meta, album=self.albums.primary(st.f.path) if self.albums else None)
                resolved[st.f.path] = r
                if r.rel_dir != plan.rel_dir:
                    log.warning("La carpeta definitiva de %s difiere de la prevista: se usa la definitiva", st.f.name)
                dirs[st.f.path] = r.rel_dir
                self._leader_dir[st.f.path] = r.rel_dir
            elif plan.leader and plan.leader in self._leader_dir:
                dirs[st.f.path] = self._leader_dir[plan.leader]
            else:
                dirs[st.f.path] = plan.rel_dir
        by_dir: dict[tuple[str, ...], list[_Staged]] = defaultdict(list)
        for st in staged:
            by_dir[dirs[st.f.path]].append(st)
        for rel_dir, members in by_dir.items():
            self._commit_bucket(rel_dir, members, resolved, res)

    def _commit_bucket(self, rel_dir, members: list[_Staged], resolved, res: SessionResult) -> None:
        target_dir = safe_join(self.dest.library, rel_dir)
        self.fs.makedirs(target_dir)
        originals = [sanitize_filename(m.f.name) for m in members]
        leader = members[0]
        pair_id = self.plan.pair_ids.get(leader.f.path)
        key = pair_id or leader.sha256
        taken = lambda n: self.fs.exists(target_dir / n)  # noqa: E731  (existencia real en disco, sin distinguir mayúsculas)
        try:
            names = resolve_group_names(originals, key, taken)
        except NameResolutionError as e:
            for m in members:
                self._fail(m.item_id, m.f, str(e), res)
                self.fs.remove(m.part)
            return
        planned = []
        for m, name in zip(members, names):
            rel = "/".join((*rel_dir, name))
            fields = dict(final_name=name, rel_path=rel)
            if m.meta is not None:
                fields.update(capture_dt=m.meta.capture_dt, date_source=m.meta.date_source.value,
                              tz_info=m.meta.tz_info, gps_lat=m.meta.gps_lat, gps_lon=m.meta.gps_lon)
            r = resolved.get(m.f.path)
            if r is not None and r.place is not None:
                fields.update(country=r.place.country, region=r.place.region, city=r.place.city)
            if m.f.path in self.plan.pair_ids:
                fields.update(pair_id=self.plan.pair_ids[m.f.path],
                              pair_role="still" if self.plan.plans[m.f.path].leader == m.f.path else "motion")
            planned.append((m, name, rel, fields))
        # UNA transacción: confirma lo pendiente del grupo anterior y registra la INTENCIÓN de este (hashes, ruta
        # final, estado VERIFIED) ANTES de renombrar. Si el proceso cae después, el arranque reconcilia.
        with self.manifest.transaction():
            self._flush_commits()
            for m, _name, _rel, fields in planned:
                self.manifest.transition(m.item_id, ItemStatus.STAGED, sha256=m.sha256, sha1_source=m.sha1)
                self.manifest.transition(m.item_id, ItemStatus.VERIFIED, verify_status=m.verify, **fields)
                if self.albums is not None:      # álbumes y favorito: se anotan; los acompañantes siguen a su líder
                    lead = self.plan.plans[m.f.path].leader or m.f.path
                    self.manifest.set_item_meta(m.item_id, self.albums.is_favorite(lead),
                                                list(self.albums.all_albums(lead)))
        for m, name, rel, _fields in planned:
            try:
                self.fs.rename_noreplace(m.part, target_dir / name)
            except FileExistsError:                              # carrera improbable: nunca se sobrescribe
                self._fail(m.item_id, m.f, "El nombre final apareció durante la copia", res)
                self.fs.remove(m.part)
                continue
            except OSError as e:
                raise StopSession(SessionOutcome.DESTINATION_ERROR, self._os_message(e)) from e
            if self.config.preserve_mtime:
                self.fs.set_mtime_ns(target_dir / name, m.f.mtime_ns)
            self._pending_commit.append(m.item_id)               # COMMITTED se escribe en la siguiente transacción
            self._recent_sha[m.sha256] = (m.item_id, rel)
            res.copied += 1
            res.bytes_copied += m.f.size
            if m.verify is VerifyStatus.VERIFIED_FULL:
                res.verified_full += 1
            else:
                res.verified_dest_only += 1
            self.ulog.info("Copiado y verificado: %s", rel)
            self.on_event("file_done", item=m.item_id, path=name, size=m.f.size)
