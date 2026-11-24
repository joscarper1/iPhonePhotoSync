"""Manifiesto SQLite del respaldo.

Decisiones:
  * Vive en el destino (``_Manifest/manifest.sqlite``) para viajar con el HDD.
  * ``journal_mode=DELETE`` y ``synchronous=FULL``: más seguro que WAL ante una
    desconexión brusca de un disco USB.
  * Invariante: un elemento solo pasa a COMMITTED con verificación positiva, SHA-256 y ruta.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from iphone_backup.domain.filetypes import classify
from iphone_backup.domain.models import (
    DeviceFile, ItemStatus, SessionStatus, VerifyStatus, check_transition,
)

SCHEMA_VERSION = 2

# v2: caché de hashes de archivos EXISTENTES en la biblioteca del usuario (solo lectura; sirve para no duplicar)
_DDL_V2 = """
CREATE TABLE IF NOT EXISTS external_files (
    path     TEXT NOT NULL,          -- relativa a la carpeta base del destino
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha1     TEXT NOT NULL,
    sha256   TEXT NOT NULL,
    PRIMARY KEY (path, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS idx_external_sha1 ON external_files(sha1);
CREATE INDEX IF NOT EXISTS idx_external_sha256 ON external_files(sha256)
"""

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_udid TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL
);

CREATE TABLE items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    device_udid     TEXT NOT NULL,
    device_path     TEXT NOT NULL,          -- identificador del elemento en el dispositivo
    original_name   TEXT NOT NULL,
    final_name      TEXT,
    ext             TEXT NOT NULL,
    mime            TEXT,
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    sha256          TEXT,
    sha1_source     TEXT,
    capture_dt      TEXT,                   -- hora local tal como está escrita, sin convertir
    date_source     TEXT,
    tz_info         TEXT,
    gps_lat         REAL,
    gps_lon         REAL,
    country         TEXT,
    region          TEXT,
    city            TEXT,
    rel_path        TEXT,                   -- ruta relativa dentro del destino
    imported_at     TEXT,
    copy_status     TEXT NOT NULL,
    verify_status   TEXT NOT NULL,
    pair_id         TEXT,                   -- Live Photo: identificador común
    pair_role       TEXT,
    error           TEXT,
    UNIQUE (device_udid, device_path, size, mtime_ns)
);
CREATE INDEX idx_items_sha256 ON items(sha256);
CREATE INDEX idx_items_status ON items(copy_status);
CREATE INDEX idx_items_pair   ON items(pair_id);
"""

_UPDATABLE = frozenset({
    "final_name", "mime", "sha256", "sha1_source", "capture_dt", "date_source", "tz_info",
    "gps_lat", "gps_lon", "country", "region", "city", "rel_path", "imported_at",
    "verify_status", "pair_id", "pair_role", "error",
})

_GOOD_VERIFY = (VerifyStatus.VERIFIED_FULL.value, VerifyStatus.VERIFIED_DEST_ONLY.value)


class ManifestError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._tx_depth = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)  # transacciones explícitas
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._ensure_item_meta()

    def _ensure_item_meta(self) -> None:
        """Tabla auxiliar (idempotente, sin cambiar la versión del esquema): álbumes y favorito de cada elemento."""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS item_meta ("
            " item_id INTEGER PRIMARY KEY REFERENCES items(id),"
            " favorite INTEGER NOT NULL DEFAULT 0,"
            " albums TEXT)")                      # JSON: lista de álbumes del iPhone; el primero es donde se guardó

    def set_item_meta(self, item_id: int, favorite: bool, albums: list[str]) -> None:
        import json
        self._conn.execute("INSERT OR REPLACE INTO item_meta (item_id, favorite, albums) VALUES (?,?,?)",
                           (item_id, 1 if favorite else 0, json.dumps(albums, ensure_ascii=False) if albums else None))

    def get_item_meta(self, item_id: int) -> tuple[bool, list[str]]:
        import json
        r = self._conn.execute("SELECT favorite, albums FROM item_meta WHERE item_id=?", (item_id,)).fetchone()
        return (bool(r["favorite"]), json.loads(r["albums"]) if r["albums"] else []) if r else (False, [])

    # --- ciclo de vida ---
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Transacción explícita. Es RE-ENTRANTE: una transacción interna se une a la externa y solo la
        más externa hace COMMIT/ROLLBACK (permite agrupar varias operaciones en un único fsync)."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield
        except BaseException:
            self._tx_depth = 0
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._tx_depth = 0
            self._conn.execute("COMMIT")

    def _init_schema(self) -> None:
        has_meta = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            with self.transaction():
                for stmt in _DDL.split(";\n"):
                    if stmt.strip():
                        self._conn.execute(stmt)
                self._apply_v2()
                self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                self._conn.execute("INSERT INTO meta VALUES ('backup_id', ?)", (str(uuid.uuid4()),))
        else:
            v = int(self.get_meta("schema_version"))
            if v > SCHEMA_VERSION:
                raise ManifestError(f"Manifiesto de versión {v} más nuevo que la soportada ({SCHEMA_VERSION})")
            if v < 2:
                with self.transaction():
                    self._apply_v2()
                    self._conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    def _apply_v2(self) -> None:
        for stmt in _DDL_V2.split(";\n"):
            if stmt.strip():
                self._conn.execute(stmt)

    # --- caché de la biblioteca existente ---
    def external_lookup(self, path: str, size: int, mtime_ns: int) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT sha1, sha256 FROM external_files WHERE path=? AND size=? AND mtime_ns=?",
            (path, size, mtime_ns)).fetchone()
        return (row["sha1"], row["sha256"]) if row else None

    def external_store(self, path: str, size: int, mtime_ns: int, sha1: str, sha256: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO external_files VALUES (?,?,?,?,?)",
                           (path, size, mtime_ns, sha1, sha256))

    # --- meta ---
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    @property
    def backup_id(self) -> str:
        return self.get_meta("backup_id")  # type: ignore[return-value]

    # --- sesiones ---
    def start_session(self, device_udid: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO sessions (device_udid, started_at, status) VALUES (?,?,?)",
            (device_udid, _now(), SessionStatus.RUNNING.value),
        )
        return int(cur.lastrowid)

    def finish_session(self, session_id: int, status: SessionStatus) -> None:
        self._conn.execute(
            "UPDATE sessions SET finished_at=?, status=? WHERE id=?", (_now(), status.value, session_id)
        )

    def recover_interrupted(self) -> int:
        """Tras un cierre inesperado: sesiones RUNNING -> INTERRUPTED y elementos STAGED -> DISCOVERED
        (el staging se limpia y se recopia). Los VERIFIED NO se tocan aquí: pueden tener ya su archivo
        renombrado en el destino; el motor los reconcilia con ``pending_commit_items``.
        Devuelve cuántos elementos STAGED se revirtieron."""
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET status=?, finished_at=? WHERE status=?",
                (SessionStatus.INTERRUPTED.value, _now(), SessionStatus.RUNNING.value),
            )
            cur = self._conn.execute(
                "UPDATE items SET copy_status=?, verify_status=?, sha256=NULL, sha1_source=NULL "
                "WHERE copy_status=?",
                (ItemStatus.DISCOVERED.value, VerifyStatus.NOT_VERIFIED.value, ItemStatus.STAGED.value),
            )
            return cur.rowcount

    def pending_commit_items(self) -> list[sqlite3.Row]:
        """Elementos VERIFIED (copia verificada, sin confirmar). Pueden tener ya el archivo en el destino."""
        return self._conn.execute(
            "SELECT * FROM items WHERE copy_status=?", (ItemStatus.VERIFIED.value,)).fetchall()

    def reset_to_discovered(self, item_id: int) -> None:
        """VERIFIED sin archivo en el destino -> vuelve a pendiente (se recopiará)."""
        self._conn.execute(
            "UPDATE items SET copy_status=?, verify_status=?, sha256=NULL, sha1_source=NULL, rel_path=NULL, "
            "final_name=NULL WHERE id=? AND copy_status=?",
            (ItemStatus.DISCOVERED.value, VerifyStatus.NOT_VERIFIED.value, item_id, ItemStatus.VERIFIED.value))

    def set_fields(self, item_id: int, **fields: Any) -> None:
        """Actualiza campos descriptivos SIN cambiar el estado (p. ej. ruta prevista antes del renombrado)."""
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ManifestError(f"Campos no actualizables: {sorted(bad)}")
        if not fields:
            return
        vals = [v.value if isinstance(v, VerifyStatus) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE items SET {sets} WHERE id=?", (*vals, item_id))

    # --- elementos ---
    def upsert_discovered(self, session_id: int, udid: str, f: DeviceFile) -> tuple[int, bool]:
        """Registra un archivo visto. Devuelve (item_id, creado). Idempotente por (udid, ruta, tamaño, mtime)."""
        row = self.find_by_fingerprint(udid, f.path, f.size, f.mtime_ns)
        if row:
            return int(row["id"]), False
        _, mime = classify(f.ext)
        cur = self._conn.execute(
            "INSERT INTO items (session_id, device_udid, device_path, original_name, ext, mime, size, mtime_ns,"
            " copy_status, verify_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, udid, f.path, f.name, f.ext, mime, f.size, f.mtime_ns,
             ItemStatus.DISCOVERED.value, VerifyStatus.NOT_VERIFIED.value),
        )
        return int(cur.lastrowid), True

    def get_item(self, item_id: int) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ManifestError(f"Elemento inexistente: {item_id}")
        return row

    def find_by_fingerprint(self, udid: str, path: str, size: int, mtime_ns: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM items WHERE device_udid=? AND device_path=? AND size=? AND mtime_ns=?",
            (udid, path, size, mtime_ns),
        ).fetchone()

    def device_states(self, udid: str) -> dict[str, list[tuple[int, int, str, str | None]]]:
        """Solo lectura: ruta del iPhone -> [(tamaño, mtime_ns, estado de copia, nota)] de todo lo ya registrado.
        Permite saber qué está respaldado sin escribir nada."""
        out: dict[str, list[tuple[int, int, str, str | None]]] = {}
        for r in self._conn.execute(
                "SELECT device_path, size, mtime_ns, copy_status, error FROM items WHERE device_udid=?", (udid,)):
            out.setdefault(r["device_path"], []).append((r["size"], r["mtime_ns"], r["copy_status"], r["error"]))
        return out

    def find_committed_by_sha256(self, sha256: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM items WHERE sha256=? AND copy_status=? LIMIT 1",
            (sha256, ItemStatus.COMMITTED.value),
        ).fetchone()

    def transition(self, item_id: int, new_status: ItemStatus, **fields: Any) -> None:
        """Cambia el estado validando la máquina de estados y los invariantes."""
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ManifestError(f"Campos no actualizables: {sorted(bad)}")
        with self.transaction():
            cur = self.get_item(item_id)
            check_transition(ItemStatus(cur["copy_status"]), new_status)
            merged = {k: (fields[k] if k in fields else cur[k]) for k in _UPDATABLE}
            for k, v in list(merged.items()):
                if isinstance(v, VerifyStatus):
                    merged[k] = v.value
            if new_status is ItemStatus.COMMITTED:
                if merged["verify_status"] not in _GOOD_VERIFY:
                    raise ManifestError("No se puede confirmar un elemento sin verificación positiva")
                if not merged["sha256"] or not merged["rel_path"]:
                    raise ManifestError("COMMITTED requiere sha256 y rel_path")
                merged["imported_at"] = merged["imported_at"] or _now()
            sets = ", ".join(f"{k}=?" for k in merged)
            self._conn.execute(
                f"UPDATE items SET copy_status=?, {sets} WHERE id=?",
                (new_status.value, *merged.values(), item_id),
            )

    def counts_by_status(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT copy_status, COUNT(*) n FROM items GROUP BY copy_status").fetchall()
        return {r["copy_status"]: r["n"] for r in rows}
