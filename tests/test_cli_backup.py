import json

import pytest

from media_fixtures import make_jpeg, make_mov, make_png, needs_exiftool
from iphonephotosync.app.planning import PlanResult, subset_plan
from iphonephotosync.cli.__main__ import main
from iphonephotosync.organizer.planner import FolderPlan, PlaceStatus

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")


@pytest.fixture
def iphone(tmp_path):
    d = tmp_path / "iphone/DCIM/100APPLE"
    make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00"})
    make_png(d / "IMG_0002.PNG")
    make_mov(d / "IMG_0003.MOV", {"Keys:CreationDate": "2025:01:02 03:04:05+01:00"})
    return tmp_path / "iphone"


def test_backup_without_yes_copies_nothing(iphone, tmp_path, capsys):
    dest = tmp_path / "hdd"
    assert main(["backup", "--simulate", str(iphone), "--dest", str(dest), "--no-gps"]) == 0
    out = capsys.readouterr().out
    assert "No se copió nada" in out and "--yes" in out
    lib = dest / "iPhone_Photos"
    assert not any(p.is_file() and p.suffix.upper() in (".JPG", ".PNG", ".MOV") for p in lib.rglob("*"))


def test_backup_with_yes_then_incremental(iphone, tmp_path, capsys):
    dest, report = tmp_path / "hdd", tmp_path / "r.json"
    assert main(["backup", "--simulate", str(iphone), "--dest", str(dest), "--yes", "--no-gps", "--json", str(report)]) == 0
    r = json.loads(report.read_text(encoding="utf-8"))
    assert r["copied"] == 3 and r["failed"] == 0 and r["verified_full"] == 3
    assert (dest / "iPhone_Photos/2024/03 - Marzo/IMG_0001.JPG").is_file()
    assert (dest / "iPhone_Photos/Fecha desconocida/IMG_0002.PNG").is_file()
    assert (dest / "iPhone_Photos/_Logs/usuario.log").is_file()
    assert main(["backup", "--simulate", str(iphone), "--dest", str(dest), "--yes", "--no-gps", "--json", str(report)]) == 0
    r2 = json.loads(report.read_text(encoding="utf-8"))
    assert r2["copied"] == 0 and r2["skipped_incremental"] == 3


def test_insufficient_space_exit_code(iphone, tmp_path, monkeypatch, capsys):
    from collections import namedtuple
    from iphonephotosync.transfer import space
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(space.shutil, "disk_usage", lambda p: U(10**12, 0, 1000))
    assert main(["backup", "--simulate", str(iphone), "--dest", str(tmp_path / "hdd"), "--yes", "--no-gps"]) == 3
    assert "insuficiente" in capsys.readouterr().out


def _plan(paths, unknown=(), live=()):
    plans = {p: FolderPlan(p, ("x",), p, 2024, 1, PlaceStatus.NO_GPS, None, p in unknown) for p in paths}
    return PlanResult(scan=None, metas={}, confirmed_pairs=set(), pair_ids={p: "id" for p in live}, plans=plans)


def test_subset_is_deterministic_and_representative():
    paths = [f"/DCIM/100APPLE/IMG_{i:04d}.JPG" for i in range(100)] + [
        "/DCIM/100APPLE/IMG_9000.HEIC", "/DCIM/100APPLE/IMG_9001.MOV", "/DCIM/100APPLE/IMG_9002.PNG",
        "/DCIM/100APPLE/IMG_9003.HEIC"]
    plan = _plan(paths, unknown={"/DCIM/100APPLE/IMG_9002.PNG"}, live={"/DCIM/100APPLE/IMG_9003.HEIC"})
    a, b = subset_plan(plan, 10), subset_plan(plan, 10)
    assert sorted(a.plans) == sorted(b.plans) and len(a.plans) >= 10
    got = " ".join(a.plans)
    assert "IMG_9001.MOV" in got and "IMG_9002.PNG" in got and "IMG_9003.HEIC" in got   # cada categoría presente
    assert subset_plan(plan, 500) is plan
