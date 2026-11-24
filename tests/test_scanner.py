import json

from conftest import make_file
from iphonephotosync.cli.__main__ import main
from iphonephotosync.device.simulated import SimulatedDeviceSource
from iphonephotosync.domain.filetypes import FileKind
from iphonephotosync.scanner import scan


def test_scan_counts_and_candidates(fake_iphone):
    r = scan(SimulatedDeviceSource(fake_iphone))
    s = r.summary()
    assert s["entries"] == 6
    assert (s["photos"], s["videos"], s["sidecars_aae"], s["ignored"]) == (3, 1, 1, 1)
    assert s["ignored_by_ext"] == {".bin": 1}                     # lo ignorado se cuenta, no desaparece
    assert [(p.still.name, p.motion.name) for p in r.live_candidates] == [("IMG_0001.HEIC", "IMG_0001.MOV")]
    assert s["aae_orphan"] == 1 and s["aae_attached"] == 0       # IMG_0003.AAE no tiene foto con ese nombre
    assert r.kinds["/DCIM/ispRegDump.bin"] is FileKind.IGNORED
    assert s["total_bytes"] == sum(f.size for f in r.files)


def test_same_basename_in_different_folders_is_not_a_pair(tmp_path):
    root = tmp_path / "p"
    make_file(root / "DCIM/100APPLE/IMG_1.HEIC", b"a")
    make_file(root / "DCIM/101APPLE/IMG_1.MOV", b"b")
    assert scan(SimulatedDeviceSource(root)).live_candidates == []


def test_pairing_is_case_insensitive_and_aae_attaches(tmp_path):
    root = tmp_path / "p"
    make_file(root / "DCIM/100APPLE/IMG_7.heic", b"a")
    make_file(root / "DCIM/100APPLE/IMG_7.MOV", b"b")
    make_file(root / "DCIM/100APPLE/IMG_7.AAE", b"c")
    r = scan(SimulatedDeviceSource(root))
    assert len(r.live_candidates) == 1
    assert r.sidecars_by_still == {"/DCIM/100APPLE/IMG_7.heic": "/DCIM/100APPLE/IMG_7.AAE"}


def test_ambiguous_group_is_flagged_not_silently_paired(tmp_path):
    root = tmp_path / "p"
    for n in ("IMG_9.HEIC", "IMG_9.JPG", "IMG_9.MOV"):
        make_file(root / "DCIM/100APPLE" / n, b"x")
    r = scan(SimulatedDeviceSource(root))
    assert r.live_candidates == [] and len(r.ambiguous_pairs) == 1


def test_scan_summary_has_no_filenames(fake_iphone):
    text = json.dumps(scan(SimulatedDeviceSource(fake_iphone)).summary())
    assert "IMG_" not in text and "DCIM" not in text


def test_cli_scan_simulated(fake_iphone, tmp_path, capsys):
    out = tmp_path / "s.json"
    assert main(["scan", "--simulate", str(fake_iphone), "--json", str(out)]) == 0
    assert "Elementos: 6" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["photos"] == 3
