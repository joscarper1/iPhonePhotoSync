import csv
from pathlib import Path

import pytest

from media_fixtures import make_jpeg, make_mov, needs_exiftool
from test_engine import Env, NoHashSource
from iphonephotosync.cli.__main__ import main
from iphonephotosync.database.manifest import Manifest
from iphonephotosync.library.external_index import ExternalIndex, analyze_existing
from iphonephotosync.metadata.exiftool_service import ExifToolService
from iphonephotosync.transfer.destination import Destination
from iphonephotosync.transfer.safety_report import REASONS, SafetyReport, build_safety_report, write_report

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


@pytest.fixture
def env(tmp_path, et):
    return Env(tmp_path, et)


def report(env, src=None, **kw):
    with Manifest(env.dest.manifest_path) as mf:
        return build_safety_report(src or env.src, env.dest, mf, **kw)


def codes(rep):
    return {e.name if not e.name.startswith("IMG_0002") else e.path: e.reason for e in rep.exceptions}


def test_everything_backed_up_is_safe_and_balances(env):
    env.run()
    before = env.snapshot()
    rep = report(env)
    assert rep.total_media == 5 and rep.safe == 5 and rep.exceptions == [] and rep.consistent
    assert rep.newest_covered
    assert env.snapshot() == before                                           # el iPhone no se tocó


def test_new_and_changed_files_are_not_covered(env):
    env.run()
    make_jpeg(env.iphone / "DCIM/101APPLE/IMG_0900.JPG", {"ExifIFD:DateTimeOriginal": "2026:11:23 10:00:00"})
    p = env.iphone / "DCIM/100APPLE/IMG_0001.JPG"
    p.write_bytes(p.read_bytes() + b"x")                                      # cambió después del respaldo
    rep = report(env)
    assert rep.total_media == 6 and rep.consistent
    assert codes(rep) == {"IMG_0900.JPG": "CHANGED_OR_NEW", "IMG_0001.JPG": "CHANGED_OR_NEW"}


def test_edited_photo_is_an_exception_because_the_unedited_original_is_not_saved(env):
    (env.iphone / "DCIM/100APPLE/IMG_0001.AAE").write_bytes(b"<plist/>")
    env.run()
    rep = report(env)
    assert rep.total_media == 5 and codes(rep) == {"IMG_0001.JPG": "EDITED"} and rep.consistent


def test_failed_copy_is_reported(env):
    env.src.faults.unreadable_paths.add("/DCIM/100APPLE/IMG_0003.PNG")
    env.run()
    rep = report(env, src=env.src)
    assert codes(rep) == {"IMG_0003.PNG": "FAILED"} and rep.safe == 4


def test_not_yet_copied_files_are_exceptions(env):
    rep_empty_backup = None
    env.dest.prepare()
    with Manifest(env.dest.manifest_path):
        pass
    rep_empty_backup = report(env)
    assert rep_empty_backup.safe == 0 and len(rep_empty_backup.exceptions) == 5
    assert set(codes(rep_empty_backup).values()) == {"CHANGED_OR_NEW"}        # sin registro: nada está respaldado


def test_disk_copy_missing_or_corrupted_is_detected(env):
    env.run()
    files = [env.dest.library / f for f in env.library_files()]
    files[0].unlink()
    data = bytearray(files[1].read_bytes()); data[8] ^= 0xFF
    files[1].write_bytes(bytes(data))                                         # mismo tamaño: solo la relectura lo detecta
    rep = report(env)
    assert sorted(codes(rep).values()) == ["DEST_CORRUPT", "DEST_MISSING"] and rep.safe == 3
    rep2 = report(env, recheck_disk=False)                                    # sin releer: la corrupción pasaría
    assert sorted(codes(rep2).values()) == ["DEST_MISSING"]


def test_live_pair_is_excluded_together_when_one_half_is_not_safe(env):
    make_jpeg(env.iphone / "DCIM/100APPLE/IMG_0100.JPG", {"ExifIFD:DateTimeOriginal": "2022:06:01 10:00:00"})
    make_mov(env.iphone / "DCIM/100APPLE/IMG_0100.MOV", {"Keys:CreationDate": "2022:06:01 10:00:01-06:00"})
    env.src.faults.unreadable_paths.add("/DCIM/100APPLE/IMG_0100.MOV")
    env.run()
    rep = report(env)
    assert codes(rep) == {"IMG_0100.MOV": "FAILED", "IMG_0100.JPG": "PAIR_INCOMPLETE"}


def test_without_device_hash_verification_is_partial(env):
    src = NoHashSource(env.iphone)
    env.run(src=src)
    rep = report(env, src=src)
    assert rep.safe == 0 and set(codes(rep).values()) == {"PARTIAL_VERIFY"}


def test_item_found_in_users_existing_library_is_safe_only_while_that_file_exists(env):
    base = env.tmp / "Fotos y Videos"
    env.dest = Destination(base)
    (base / "Fotos del 2024").mkdir(parents=True)
    existing = base / "Fotos del 2024/otra_copia.jpg"
    existing.write_bytes((env.iphone / "DCIM/100APPLE/IMG_0001.JPG").read_bytes())
    mf = Manifest(env.dest.manifest_path)
    idx = ExternalIndex(base, mf, exclude_paths=[env.dest.library])
    plan = env.plan()
    idx.build({f.size for f in plan.scan.files})
    ex = analyze_existing(env.src, plan.scan.files, idx)
    mf.close()
    env.run(plan=plan, existing=ex, external_index=idx)
    assert report(env).safe == 5                                              # equivalente presente: a salvo
    st = existing.stat()
    existing.write_bytes(existing.read_bytes()[:-1] + b"Z")                   # el usuario modificó su copia
    import os
    os.utime(existing, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert codes(report(env)) == {"IMG_0001.JPG": "EXISTING_GONE"}
    existing.unlink()
    assert codes(report(env)) == {"IMG_0001.JPG": "EXISTING_GONE"}


def test_report_files_are_written_with_instructions(env):
    (env.iphone / "DCIM/100APPLE/IMG_0001.AAE").write_bytes(b"<plist/>")
    env.run()
    rep = report(env)
    csv_path, txt_path = write_report(rep, env.dest.logs_dir)
    rows = list(csv.reader(open(csv_path, encoding="utf-8-sig")))
    assert rows[0] == ["archivo", "tipo", "fecha_captura", "tamano_bytes", "motivo"]
    assert rows[1][0] == "IMG_0001.JPG" and rows[1][4] == REASONS["EDITED"] and rows[1][2].startswith("2024-03-10")
    txt = txt_path.read_text(encoding="utf-8")
    assert "no borra nada" in txt and "Cuadre" in txt and "OK" in txt and "SEGUNDA copia" in txt
    assert "después" in txt                                                    # advierte que lo posterior no está cubierto


def test_inconsistent_report_is_flagged_and_not_presented_as_safe(tmp_path):
    rep = SafetyReport("2026-01-01T00:00:00+00:00", "x", total_media=10, safe=3)   # 3 + 0 != 10
    assert not rep.consistent
    _, txt = write_report(rep, tmp_path)
    assert "NO CUADRA: NO uses este informe" in txt.read_text(encoding="utf-8")


def test_cli_deletion_report_is_read_only(env, capsys):
    env.run()
    before = env.snapshot()
    assert main(["deletion-report", "--dest", str(env.dest.base), "--simulate", str(env.iphone)]) == 0
    out = capsys.readouterr().out
    assert "A salvo: 5" in out and "Cuadre: OK" in out
    assert env.snapshot() == before and list(env.dest.logs_dir.glob("no_borrar_*.csv"))
