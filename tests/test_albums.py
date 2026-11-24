"""Álbumes del iPhone como raíz de carpetas: Álbum/Año/Mes/Ubicación (lo que no está en un álbum, sin raíz)."""
import sqlite3

import pytest

from media_fixtures import make_jpeg, make_mov, needs_exiftool
from iphone_backup.app.albums import AlbumIndex, AlbumsUnavailable, load_albums, read_albums
from iphone_backup.app.workflow import UserSettings, Workflow
from iphone_backup.database.manifest import Manifest
from iphone_backup.device.simulated import SimulatedDeviceSource


def make_photos_db(path, assets, albums, memberships, favorites=(), join="Z_34", trashed_assets=(), kind=2):
    """Base de Fotos mínima con el esquema de Apple. assets: {pk: (directorio, nombre)};
    albums: {pk: (título, ZKIND, trashed)}; memberships: [(álbum pk, asset pk)]."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY TEXT, ZFILENAME TEXT, ZFAVORITE INTEGER,"
                " ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER)")
    con.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZKIND INTEGER, ZTITLE TEXT, ZTRASHEDSTATE INTEGER)")
    con.execute(f"CREATE TABLE {join}ASSETS ({join}ALBUMS INTEGER, Z_3ASSETS INTEGER, Z_FOK_3ASSETS INTEGER)")
    for pk, (d, n) in assets.items():
        con.execute("INSERT INTO ZASSET VALUES (?,?,?,?,0,?)", (pk, d, n, 1 if pk in favorites else 0,
                                                                1 if pk in trashed_assets else 0))
    for pk, (title, k, tr) in albums.items():
        con.execute("INSERT INTO ZGENERICALBUM VALUES (?,?,?,?)", (pk, k, title, tr))
    for a, s in memberships:
        con.execute(f"INSERT INTO {join}ASSETS VALUES (?,?,0)", (a, s))
    con.commit()
    con.close()


ASSETS = {1: ("DCIM/100APPLE", "IMG_0001.JPG"), 2: ("DCIM/100APPLE", "IMG_0002.JPG"),
          3: ("DCIM/100APPLE", "IMG_0003.JPG"), 4: ("DCIM/100APPLE", "IMG_0004.MOV")}


def test_reads_user_albums_favorites_and_ignores_the_rest(tmp_path):
    db = tmp_path / "Photos.sqlite"
    make_photos_db(db, ASSETS,
                   albums={10: ("Viaje a Costa Rica", 2, 0), 11: ("Álbum A", 2, 0), 12: ("Recientes", 1506, 0),
                           13: ("Borrado", 2, 1), 14: ("   ", 2, 0)},
                   memberships=[(10, 1), (11, 1), (10, 2), (12, 3), (13, 3), (14, 4)],
                   favorites={2, 3})
    idx = read_albums(db)
    assert idx.primary("/DCIM/100APPLE/IMG_0001.JPG") == "Álbum A"              # varios álbumes: el 1.º alfabético
    assert idx.all_albums("/DCIM/100APPLE/IMG_0001.JPG") == ("Álbum A", "Viaje a Costa Rica")
    assert idx.primary("/DCIM/100APPLE/IMG_0002.JPG") == "Viaje a Costa Rica"
    assert idx.primary("/DCIM/100APPLE/IMG_0003.JPG") is None                   # álbum inteligente / álbum en papelera: no
    assert idx.primary("/DCIM/100APPLE/IMG_0004.MOV") is None                   # álbum sin título: no puede ser carpeta
    assert idx.favorites == {"/DCIM/100APPLE/IMG_0002.JPG", "/DCIM/100APPLE/IMG_0003.JPG"}
    assert idx.user_albums == 3 and idx.schema == "Z_34ASSETS"


def test_join_table_number_changes_between_ios_versions(tmp_path):
    db = tmp_path / "Photos.sqlite"
    make_photos_db(db, ASSETS, {10: ("Mascotas", 2, 0)}, [(10, 1)], join="Z_28")
    assert read_albums(db).primary("/DCIM/100APPLE/IMG_0001.JPG") == "Mascotas"


def test_trashed_photos_are_not_counted_and_unknown_schema_degrades(tmp_path):
    db = tmp_path / "Photos.sqlite"
    make_photos_db(db, ASSETS, {10: ("X", 2, 0)}, [(10, 1), (10, 2)], trashed_assets={2})
    idx = read_albums(db)
    assert idx.primary("/DCIM/100APPLE/IMG_0002.JPG") is None and idx.primary("/DCIM/100APPLE/IMG_0001.JPG") == "X"
    bad = tmp_path / "otra.sqlite"
    sqlite3.connect(bad).execute("CREATE TABLE algo (x)")
    with pytest.raises(AlbumsUnavailable, match="no reconocido"):
        read_albums(bad)
    notdb = tmp_path / "x.sqlite"
    notdb.write_bytes(b"no es sqlite" * 50)
    with pytest.raises(AlbumsUnavailable):
        read_albums(notdb)


def test_json_roundtrip_and_load_removes_the_private_copy(tmp_path):
    root = tmp_path / "iphone"
    (root / "PhotoData").mkdir(parents=True)
    make_photos_db(root / "PhotoData/Photos.sqlite", ASSETS, {10: ("Fam", 2, 0)}, [(10, 1)], favorites={1})
    work = tmp_path / "trabajo"
    idx = load_albums(SimulatedDeviceSource(root), work)
    assert not work.exists()                                                    # la copia (datos personales) se borra
    again = AlbumIndex.from_json(idx.to_json())
    assert again.primary("/DCIM/100APPLE/IMG_0001.JPG") == "Fam" and again.is_favorite("/DCIM/100APPLE/IMG_0001.JPG")
    with pytest.raises(AlbumsUnavailable):
        load_albums(SimulatedDeviceSource(tmp_path / "sin_base"), tmp_path / "t2")


@pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
class TestFlow:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IPHONE_BACKUP_HOME", str(tmp_path / "cfg"))
        root = tmp_path / "iphone"
        d = root / "DCIM/100APPLE"
        make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2026:02:10 12:00:00"})   # en 2 álbumes
        make_jpeg(d / "IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2026:02:11 12:00:00"})   # en 1 álbum, favorito
        make_jpeg(d / "IMG_0003.JPG", {"ExifIFD:DateTimeOriginal": "2025:07:04 12:00:00"})   # sin álbum
        (d / "IMG_0002.AAE").write_bytes(b"<plist/>")                                          # acompañante de la foto 2
        (root / "PhotoData").mkdir()
        assets = {1: ("DCIM/100APPLE", "IMG_0001.JPG"), 2: ("DCIM/100APPLE", "IMG_0002.JPG"),
                  3: ("DCIM/100APPLE", "IMG_0003.JPG")}
        make_photos_db(root / "PhotoData/Photos.sqlite", assets,
                       {10: ("Viaje: San/Salvador", 2, 0), 11: ("Familia", 2, 0)},
                       [(10, 1), (11, 1), (10, 2)], favorites={2})
        (tmp_path / "hdd").mkdir()
        return Workflow(UserSettings(dest=str(tmp_path / "hdd"), use_gps=False, workers=2), simulate=root)

    def lib(self, tmp_path):
        base = tmp_path / "hdd/iPhone_Photos"
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*")
                      if p.is_file() and not p.relative_to(base).parts[0].startswith("_"))

    def test_album_is_the_root_single_copy_and_unalbumed_keep_the_old_layout(self, env, tmp_path):
        env.connect()
        r = env.scan()
        assert r.albums_status == "ok" and r.album_items == 2 and r.favorite_items == 1
        groups = {g.name: g for g in env.pending_groups()}
        assert groups["IMG_0001.JPG"].album == "Familia" and groups["IMG_0002.JPG"].favorite
        s = env.run_backup(lambda *a, **k: None)
        assert s.copied == 4 and s.failed == 0
        files = self.lib(tmp_path)
        assert "Familia/2026/02 - Febrero/IMG_0001.JPG" in files                           # raíz = álbum (1.º alfabético)
        assert "Viaje_ San_Salvador/2026/02 - Febrero/IMG_0002.JPG" in files               # nombre saneado: sin ':' ni '/'
        assert "Viaje_ San_Salvador/2026/02 - Febrero/IMG_0002.AAE" in files               # el .AAE sigue a su foto
        assert "2025/07 - Julio/IMG_0003.JPG" in files                                     # sin álbum: sin raíz, como antes
        assert sum(f.endswith("IMG_0001.JPG") for f in files) == 1                             # UNA sola copia
        with Manifest(tmp_path / "hdd/iPhone_Photos/_Manifest/manifest.sqlite") as mf:
            rows = {r["original_name"]: mf.get_item_meta(r["id"]) for r in
                    mf._conn.execute("SELECT id, original_name FROM items WHERE copy_status='committed'")}
        assert rows["IMG_0001.JPG"] == (False, ["Familia", "Viaje: San/Salvador"])           # todos los álbumes se anotan
        assert rows["IMG_0002.JPG"][0] is True and rows["IMG_0002.AAE"][0] is True            # favorito anotado (y su .AAE)
        assert rows["IMG_0003.JPG"] == (False, [])

    def test_album_layout_can_be_turned_off(self, env, tmp_path):
        env.settings.by_album = False
        env.connect()
        r = env.scan()
        assert r.albums_status == "off" and r.album_items == 0
        env.run_backup(lambda *a, **k: None)
        assert all(not f.startswith(("Familia", "Viaje")) for f in self.lib(tmp_path))

    def test_without_photos_database_it_degrades_to_the_old_layout_and_says_so(self, env, tmp_path):
        (tmp_path / "iphone/PhotoData/Photos.sqlite").unlink()
        env.connect()
        r = env.scan()
        assert r.albums_status == "unavailable" and any("sin álbum" in n.lower() for n in r.compare_notes)
        s = env.run_backup(lambda *a, **k: None)
        assert s.copied == 4 and all(f.startswith("202") for f in self.lib(tmp_path))      # nada se pierde ni se inventa

    def test_quick_scan_reuses_the_saved_albums_instead_of_copying_the_database(self, env, tmp_path):
        env.connect()
        env.scan(mode="full")
        (tmp_path / "iphone/PhotoData/Photos.sqlite").unlink()                             # ya no se puede copiar
        q = env.scan(mode="quick")
        assert q.albums_status == "cached" and q.album_items == 2
        assert any("álbumes" in n.lower() for n in q.compare_notes)
