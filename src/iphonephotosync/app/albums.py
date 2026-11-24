"""Álbumes del usuario y favoritos, leídos de la base de Fotos del iPhone (``/PhotoData/Photos.sqlite``).

SOLO LECTURA y solo lo necesario: se copia la base a una carpeta local temporal, se consultan únicamente los
álbumes de usuario (``ZGENERICALBUM.ZKIND = 2``), su pertenencia y la marca de favorito, y la copia se borra.
La base contiene además datos muy personales (personas, lugares) que NO se leen ni se guardan.

El esquema es interno de Apple y cambia entre versiones de iOS (p. ej. la tabla de unión es ``Z_34ASSETS`` aquí,
``Z_28ASSETS`` en otras): se descubre por patrón. Si no se reconoce, se lanza ``AlbumsUnavailable`` y el flujo
sigue SIN álbumes (nunca se inventa una pertenencia).
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

USER_ALBUM_KIND = 2


class AlbumsUnavailable(Exception):
    """No se pudo obtener la información de álbumes (no existe la base o el esquema no se reconoce)."""


@dataclass
class AlbumIndex:
    albums: dict[str, tuple[str, ...]] = field(default_factory=dict)   # ruta en el iPhone -> títulos (orden alfabético)
    favorites: set[str] = field(default_factory=set)
    schema: str = ""                                                   # tabla de unión usada (informativo)
    user_albums: int = 0

    def primary(self, path: str) -> str | None:
        """Álbum donde se guarda la única copia: el primero por orden alfabético (sin distinguir mayúsculas)."""
        a = self.albums.get(path)
        return a[0] if a else None

    def all_albums(self, path: str) -> tuple[str, ...]:
        return self.albums.get(path, ())

    def is_favorite(self, path: str) -> bool:
        return path in self.favorites

    def to_json(self) -> str:
        return json.dumps({"albums": {p: list(a) for p, a in self.albums.items()}, "favorites": sorted(self.favorites),
                           "schema": self.schema, "user_albums": self.user_albums}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "AlbumIndex":
        d = json.loads(text)
        return cls({p: tuple(a) for p, a in d["albums"].items()}, set(d["favorites"]), d.get("schema", ""),
                   int(d.get("user_albums", 0)))


def _sort_key(title: str) -> tuple[str, str]:
    """Orden alfabético como lo espera una persona: sin distinguir mayúsculas ni acentos ('Álbum' antes que 'Viaje')."""
    import unicodedata
    folded = "".join(c for c in unicodedata.normalize("NFD", title) if not unicodedata.combining(c))
    return folded.casefold(), title


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def read_albums(db_path: Path) -> AlbumIndex:
    """Lee álbumes de usuario y favoritos de una COPIA local de Photos.sqlite."""
    try:
        con = sqlite3.connect(str(db_path))        # copia propia: puede aplicar el -wal sin riesgo
    except sqlite3.Error as e:
        raise AlbumsUnavailable(f"No se pudo abrir la base de Fotos: {e}") from e
    try:
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:
            raise AlbumsUnavailable(f"La base de Fotos no es legible: {e}") from e
        for need in ("ZASSET", "ZGENERICALBUM"):
            if need not in tables:
                raise AlbumsUnavailable(f"Esquema de Fotos no reconocido: falta {need}")
        acols, gcols = _cols(con, "ZASSET"), _cols(con, "ZGENERICALBUM")
        for c in ("Z_PK", "ZFILENAME", "ZDIRECTORY"):
            if c not in acols:
                raise AlbumsUnavailable(f"Esquema de Fotos no reconocido: ZASSET sin {c}")
        for c in ("Z_PK", "ZKIND", "ZTITLE"):
            if c not in gcols:
                raise AlbumsUnavailable(f"Esquema de Fotos no reconocido: ZGENERICALBUM sin {c}")
        asset_ok = "AND COALESCE(a.ZTRASHEDSTATE,0)=0" if "ZTRASHEDSTATE" in acols else ""
        album_ok = "AND COALESCE(g.ZTRASHEDSTATE,0)=0" if "ZTRASHEDSTATE" in gcols else ""

        def path_of(directory, filename) -> str | None:
            if not directory or not filename:
                return None
            p = "/" + str(directory).strip("/") + "/" + str(filename)
            return p if p.startswith("/DCIM/") else None

        joins = []
        for t in sorted(tables):
            if re.fullmatch(r"Z_\d+ASSETS", t):
                cols = _cols(con, t)
                alb = next((c for c in cols if re.fullmatch(r"Z_\d+ALBUMS", c)), None)
                ast = next((c for c in cols if re.fullmatch(r"Z_\d+ASSETS", c)), None)
                if alb and ast:
                    joins.append((t, alb, ast))
        if not joins:
            raise AlbumsUnavailable("Esquema de Fotos no reconocido: no hay tabla de unión álbum-foto (Z_<n>ASSETS)")

        membership: dict[str, dict[int, str]] = {}
        used = []
        for t, alb, ast in joins:
            rows = con.execute(
                f'SELECT a.ZDIRECTORY, a.ZFILENAME, g.Z_PK, g.ZTITLE FROM "{t}" m '
                f'JOIN ZASSET a ON a.Z_PK = m."{ast}" JOIN ZGENERICALBUM g ON g.Z_PK = m."{alb}" '
                f"WHERE g.ZKIND = {USER_ALBUM_KIND} {asset_ok} {album_ok}").fetchall()
            if rows:
                used.append(t)
            for directory, filename, gid, title in rows:
                p, title = path_of(directory, filename), (title or "").strip()
                if p and title:                                   # un álbum sin título no puede ser carpeta
                    membership.setdefault(p, {})[gid] = title
        albums = {p: tuple(sorted(set(d.values()), key=_sort_key)) for p, d in membership.items()}
        fav_rows = con.execute(f"SELECT a.ZDIRECTORY, a.ZFILENAME FROM ZASSET a WHERE a.ZFAVORITE = 1 {asset_ok}") \
            if "ZFAVORITE" in acols else []
        favorites = {p for p in (path_of(d, f) for d, f in fav_rows) if p}
        n_albums = con.execute(
            f"SELECT COUNT(*) FROM ZGENERICALBUM g WHERE g.ZKIND = {USER_ALBUM_KIND} {album_ok}").fetchone()[0]
        return AlbumIndex(albums, favorites, ",".join(used) or ",".join(t for t, _, _ in joins), n_albums)
    except sqlite3.Error as e:
        raise AlbumsUnavailable(f"No se pudo leer la base de Fotos: {e}") from e
    finally:
        con.close()


def load_albums(source, work_dir: Path | None = None) -> AlbumIndex:
    """Copia la base de Fotos del iPhone a una carpeta temporal local, lee álbumes y favoritos y borra la copia."""
    copier = getattr(source, "copy_photos_database", None)
    if copier is None:
        raise AlbumsUnavailable("Este acceso al iPhone no permite leer los álbumes.")
    tmp = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="photosdb_"))
    try:
        db = copier(tmp)
        if db is None:
            raise AlbumsUnavailable("El iPhone no tiene base de Fotos accesible (/PhotoData/Photos.sqlite).")
        return read_albums(Path(db))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)      # la copia contiene datos personales: no se conserva
