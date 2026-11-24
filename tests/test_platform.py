"""Portabilidad: lo específico de cada sistema operativo está aislado y se comporta igual de forma segura."""
import os
import sys

import pytest

from iphonephotosync.metadata.exiftool_service import default_exiftool_path
from iphonephotosync.transfer import platform_fs
from iphonephotosync.transfer.fsops import RealFs


def test_filesystem_names_are_normalized():
    n = platform_fs._normalize
    assert (n("vfat"), n("msdos"), n("exfat"), n("ntfs3"), n("apfs"), n("ext4")) == \
        ("FAT32", "FAT32", "EXFAT", "NTFS", "APFS", "EXT4")
    assert n("algo_raro") == "ALGO_RARO"


def test_linux_mountinfo_parsing_picks_the_longest_mount_point(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_fs, "IS_LINUX", True)
    monkeypatch.setattr(platform_fs, "IS_MAC", False)
    monkeypatch.setattr(platform_fs, "_mounts_linux", lambda: [("/", "ext4"), ("/media/yo/DISCO", "exfat")])
    monkeypatch.setattr(os.path, "realpath", lambda p: str(p).replace("\\", "/"))
    assert platform_fs.posix_filesystem_name("/media/yo/DISCO/Fotos") == "EXFAT"     # el disco externo, no "/"
    assert platform_fs.posix_filesystem_name("/home/yo") == "EXT4"


def test_macos_mount_output_parsing(monkeypatch):
    class R:
        stdout = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
                  "/dev/disk5s1 on /Volumes/Mi Disco (exfat, local, nodev, nosuid, noowners)\n")
    monkeypatch.setattr(platform_fs.subprocess, "run", lambda *a, **k: R())
    assert ("/Volumes/Mi Disco", "exfat") in platform_fs._mounts_mac()
    assert ("/", "apfs") in platform_fs._mounts_mac()


def test_device_hint_mentions_the_right_thing_per_os(monkeypatch):
    monkeypatch.setattr(platform_fs, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_fs, "IS_MAC", False)
    assert "usbmuxd" in platform_fs.device_not_found_hint()
    monkeypatch.setattr(platform_fs, "IS_MAC", True)
    assert "Confiar" in platform_fs.device_not_found_hint() and "usbmuxd" not in platform_fs.device_not_found_hint()
    monkeypatch.setattr(platform_fs, "IS_WINDOWS", True)
    assert "Apple Devices" in platform_fs.device_not_found_hint()


def test_exiftool_path_prefers_env_then_bundle_then_path(monkeypatch, tmp_path):
    fake = tmp_path / "mi_exiftool"
    monkeypatch.setenv("IPHONEPHOTOSYNC_EXIFTOOL", str(fake))
    assert default_exiftool_path() == fake
    monkeypatch.delenv("IPHONEPHOTOSYNC_EXIFTOOL")
    name = "exiftool.exe" if os.name == "nt" else "exiftool"
    bundle = tmp_path / "bundle/tools/exiftool"
    bundle.mkdir(parents=True)
    (bundle / name).write_bytes(b"x")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)       # app empaquetada
    assert default_exiftool_path() == bundle / name


def test_rename_never_overwrites_on_any_os(tmp_path):
    fs = RealFs()
    a, b = tmp_path / "a.part", tmp_path / "b.jpg"
    a.write_bytes(b"nuevo")
    b.write_bytes(b"existente")
    with pytest.raises(FileExistsError):
        fs.rename_noreplace(a, b)
    assert b.read_bytes() == b"existente" and a.exists()                          # ni se sobrescribió ni se perdió
    c = tmp_path / "c.jpg"
    fs.rename_noreplace(a, c)
    assert c.read_bytes() == b"nuevo" and not a.exists()


@pytest.mark.skipif(os.name == "nt", reason="renameat2/renamex_np solo existen en Linux/macOS")
def test_posix_atomic_noreplace_helper_reports_exists_or_unsupported(tmp_path):
    from iphonephotosync.transfer.fsops import _posix_rename_noreplace
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    try:
        done = _posix_rename_noreplace(str(a), str(b))
    except FileExistsError:
        done = None                                                              # soportado y detectó que existe
    assert done in (None, False) and b.read_bytes() == b"2"
