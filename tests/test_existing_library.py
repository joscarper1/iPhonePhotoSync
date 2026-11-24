"""No duplicar lo que el usuario ya tiene en su biblioteca existente (solo lectura sobre ella)."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from media_fixtures import needs_exiftool
from test_engine import Env, NoHashSource, sha
from iphone_backup.cli.__main__ import main
from iphone_backup.database.manifest import Manifest
from iphone_backup.library.external_index import ExternalIndex, analyze_existing
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.transfer.destination import Destination
from iphone_backup.transfer.engine import SessionOutcome

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
IMG1 = "DCIM/100APPLE/IMG_0001.JPG"
IMG2 = "DCIM/100APPLE/IMG_0002.JPG"
PNG3 = "DCIM/100APPLE/IMG_0003.PNG"


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


@pytest.fixture
def env(tmp_path, et):
    e = Env(tmp_path, et)
    base = tmp_path / "Fotos y Videos"
    e.dest = Destination(base)
    # biblioteca existente del usuario
    (base / "Fotos del 2024").mkdir(parents=True)
    (base / "Fotos del 2024/foto_renombrada.jpg").write_bytes((e.iphone / IMG1).read_bytes())     # mismo contenido, OTRO nombre
    (base / "Álbum Familia").mkdir()
    same_size = bytearray((e.iphone / IMG2).read_bytes())
    same_size[-3] ^= 0xFF                                                                         # mismo tamaño, contenido distinto
    (base / "Álbum Familia/parecida.jpg").write_bytes(bytes(same_size))
    (base / "Papelera").mkdir()
    (base / "Papelera/borrada.png").write_bytes((e.iphone / PNG3).read_bytes())                   # solo en la papelera
    return e


def existing_snapshot(env):
    out = {}
    for p in sorted(env.dest.base.rglob("*")):
        if p.is_file() and env.dest.library not in p.parents:
            st = p.stat()
            out[str(p.relative_to(env.dest.base))] = (st.st_size, st.st_mtime_ns, sha(p))
    return out


def build_index(env, mf, sizes=None):
    idx = ExternalIndex(env.dest.base, mf, exclude_paths=[env.dest.library])
    plan = env.plan()
    sizes = sizes or {f.size for f in plan.scan.files}
    idx.build(sizes)
    return idx, plan


def test_index_hashes_only_size_candidates_and_skips_trash_and_own_library(env):
    env.dest.prepare()
    (env.dest.library / "2024").mkdir()
    (env.dest.library / "2024/propio.jpg").write_bytes((env.iphone / IMG1).read_bytes())          # nuestra propia carpeta
    (env.dest.base / "Fotos del 2024/solo_para_ruido.txt").write_bytes(b"x" * 7)                  # tamaño que no coincide
    with Manifest(env.dest.manifest_path) as mf:
        idx, _ = build_index(env, mf)
        assert idx.stats.candidates == 2 and idx.stats.hashed == 2                                # no 5, no 4: solo tamaños que coinciden
        rels = {r for v in idx.by_sha1.values() for r in v}
        assert rels == {"Fotos del 2024/foto_renombrada.jpg", "Álbum Familia/parecida.jpg"}       # sin Papelera ni iPhone_Photos
        assert idx.stats.excluded_dirs == 2


def test_hash_cache_avoids_rereading(env):
    with Manifest(env.dest.manifest_path) as mf:
        idx, plan = build_index(env, mf)
        assert idx.stats.hashed == 2
        idx2 = ExternalIndex(env.dest.base, mf, exclude_paths=[env.dest.library])
        idx2.build({f.size for f in plan.scan.files})
        assert idx2.stats.hashed == 0 and idx2.stats.cache_hits == 2


def test_cache_is_invalidated_when_the_file_changes(env):
    with Manifest(env.dest.manifest_path) as mf:
        idx, plan = build_index(env, mf)
        p = env.dest.base / "Fotos del 2024/foto_renombrada.jpg"
        data = p.read_bytes()
        p.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))                                       # mismo tamaño, otro contenido
        idx2 = ExternalIndex(env.dest.base, mf, exclude_paths=[env.dest.library])
        idx2.build({f.size for f in plan.scan.files});
        assert idx2.find_by_sha256(sha(env.iphone / IMG1)) is None                                # ya no es el mismo contenido


def test_analysis_matches_by_content_not_name_or_size_and_transfers_nothing(env):
    with Manifest(env.dest.manifest_path) as mf:
        idx, plan = build_index(env, mf)
        before = env.src.bytes_read
        res = analyze_existing(env.src, plan.scan.files, idx)
        assert env.src.bytes_read == before                                                       # el hash lo calcula el iPhone: 0 bytes por USB
        assert res.matches == {"/" + IMG1: "Fotos del 2024/foto_renombrada.jpg"}                  # IMG_0002 (mismo tamaño) NO coincide
        assert res.checked == 3                                                                   # los 3 JPEG sintéticos miden lo mismo
        assert res.bytes_already_present == (env.iphone / IMG1).stat().st_size


def run_with_existing(env, src=None):
    mf = Manifest(env.dest.manifest_path)
    idx = ExternalIndex(env.dest.base, mf, exclude_paths=[env.dest.library])
    src = src or env.src
    plan = env.plan(src)
    idx.build({f.size for f in plan.scan.files})
    ex = analyze_existing(src, plan.scan.files, idx)
    mf.close()
    return env.run(plan=plan, src=src, existing=ex, external_index=idx), ex


def test_engine_does_not_copy_what_already_exists_and_never_touches_it(env):
    before = existing_snapshot(env)
    iphone_before = env.snapshot()
    res, ex = run_with_existing(env)
    assert res.outcome is SessionOutcome.COMPLETED
    assert res.skipped_existing == 1 and res.copied == 4 and res.failed == 0
    files = env.library_files()
    assert len(files) == 4 and not any("IMG_0001" in f for f in files)                            # no se duplicó
    assert any("IMG_0002" in f for f in files)                                                    # mismo tamaño pero distinto: se copia
    assert any(f.endswith("IMG_0003.PNG") for f in files)                                         # solo estaba en la Papelera: se copia
    assert existing_snapshot(env) == before                                                       # biblioteca existente intacta
    assert env.snapshot() == iphone_before                                                        # iPhone intacto
    with Manifest(env.dest.manifest_path) as mf:
        row = mf._conn.execute("SELECT * FROM items WHERE original_name='IMG_0001.JPG'").fetchone()
    assert row["copy_status"] == "skipped_dup" and row["rel_path"] == "@ext/Fotos del 2024/foto_renombrada.jpg"


def test_rerun_keeps_counting_existing_and_copies_nothing(env):
    run_with_existing(env)
    res, _ = run_with_existing(env)
    assert res.copied == 0 and res.skipped_existing == 1 and res.skipped_incremental == 4


def test_without_device_hash_the_sha256_check_after_copy_still_prevents_duplicates(env):
    res, ex = run_with_existing(env, src=NoHashSource(env.iphone))
    assert ex.unavailable_hash == 3 and ex.matches == {}                                          # no hay forma de saberlo antes
    assert res.skipped_existing == 1 and res.copied == 4                                          # se detecta tras copiar a staging
    assert not any("IMG_0001" in f for f in env.library_files()) and env.leftovers() == []


def test_without_existing_check_everything_is_copied(env):
    res = env.run()
    assert res.copied == 5 and res.skipped_existing == 0


def test_schema_v1_manifest_is_migrated(tmp_path):
    p = tmp_path / "old.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version','1')")
    con.execute("INSERT INTO meta VALUES ('backup_id','abc')")
    con.commit(); con.close()
    with Manifest(p) as mf:
        assert mf.get_meta("schema_version") == "2" and mf.backup_id == "abc"
        mf.external_store("a/b.jpg", 1, 2, "x" * 40, "y" * 64)
        assert mf.external_lookup("a/b.jpg", 1, 2) == ("x" * 40, "y" * 64)
        assert mf.external_lookup("a/b.jpg", 1, 3) is None


def test_cli_reports_existing_and_skips_them(env, capsys, tmp_path):
    report = tmp_path / "r.json"
    code = main(["backup", "--simulate", str(env.iphone), "--dest", str(env.dest.base), "--yes", "--no-gps",
                 "--json", str(report)])
    out = capsys.readouterr().out
    assert code == 0 and "Ya existen en tu biblioteca: 1 archivos" in out
    r = json.loads(report.read_text(encoding="utf-8"))
    assert r["skipped_existing"] == 1 and r["copied"] == 4


def test_cli_sample_prefers_items_that_will_actually_be_copied(env, capsys, tmp_path):
    code = main(["backup", "--simulate", str(env.iphone), "--dest", str(env.dest.base), "--no-gps", "--sample", "2"])
    out = capsys.readouterr().out
    assert code == 0 and "No se copió nada" in out
