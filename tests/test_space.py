import os
from collections import namedtuple

import pytest

from iphonephotosync.transfer import space
from iphonephotosync.transfer.space import (
    GIB, MIN_RESERVE, check_space, default_reserve, filesystem_name, has_room_for, oversized_for_fs, required_bytes,
)

Usage = namedtuple("Usage", "total used free")


def fake_free(monkeypatch, free):
    monkeypatch.setattr(space.shutil, "disk_usage", lambda p: Usage(10 * GIB * 100, 0, free))


def test_required_excludes_already_backed_up_and_unplanned():
    sizes = {"/a": 100, "/b": 200, "/c": 300, "/ignored.bin": 999}
    assert required_bytes(sizes, ["/a", "/b", "/c"]) == 600
    assert required_bytes(sizes, ["/a", "/b", "/c"], already_backed_up=["/b"]) == 400


def test_reserve_is_at_least_1gib_or_1_percent():
    assert default_reserve(10 * 1024 ** 2) == MIN_RESERVE
    assert default_reserve(500 * GIB) == int(500 * GIB * 0.01)


def test_enough_space(monkeypatch, tmp_path):
    fake_free(monkeypatch, 100 * GIB)
    r = check_space(tmp_path, 74 * GIB)
    assert r.ok and r.shortfall == 0 and "suficiente" in r.message()


def test_insufficient_space_reports_shortfall(monkeypatch, tmp_path):
    fake_free(monkeypatch, 74 * GIB)
    r = check_space(tmp_path, 74 * GIB)                    # justo lo requerido, sin reserva: NO alcanza
    assert not r.ok and r.shortfall == r.reserve and "insuficiente" in r.message()


def test_exact_fit_with_reserve_is_ok(monkeypatch, tmp_path):
    required = 50 * GIB
    fake_free(monkeypatch, required + default_reserve(required))
    assert check_space(tmp_path, required).ok


def test_destination_not_created_yet_uses_existing_parent(tmp_path):
    r = check_space(tmp_path / "no" / "existe" / "aun", 1)
    assert r.checked_path == str(tmp_path) and r.free > 0
    assert not (tmp_path / "no").exists()                  # comprobar no crea carpetas


def test_per_file_check(monkeypatch, tmp_path):
    fake_free(monkeypatch, 3 * GIB)
    assert has_room_for(tmp_path, 1 * GIB)                 # 1 GiB + 1 GiB de reserva <= 3 GiB
    assert not has_room_for(tmp_path, 2 * GIB + 1)


def test_fat32_rejects_files_of_4gib_or_more():
    sizes = {"/ok": 4 * GIB - 1, "/grande.mov": 4 * GIB, "/otro.mp4": 5 * GIB}
    assert oversized_for_fs("FAT32", sizes) == ["/grande.mov", "/otro.mp4"]
    assert oversized_for_fs("fat32", sizes) == ["/grande.mov", "/otro.mp4"]
    assert oversized_for_fs("NTFS", sizes) == [] and oversized_for_fs("EXFAT", sizes) == []
    assert oversized_for_fs(None, sizes) == []


@pytest.mark.skipif(os.name != "nt", reason="Solo Windows")
def test_filesystem_name_detected_or_unknown(tmp_path):
    """Windows/Linux/macOS: el nombre normalizado de un sistema de archivos conocido, o None si no se sabe (nunca falla)."""
    assert filesystem_name(tmp_path) in {None, "NTFS", "EXFAT", "FAT32", "REFS", "FAT", "APFS", "HFS+", "EXT4", "BTRFS",
                                         "XFS", "OVERLAY", "TMPFS", "FUSEBLK", "ZFS", "9P", "FUSE.OSXFS", "FUSE", "NFS"} \
        or isinstance(filesystem_name(tmp_path), str)
