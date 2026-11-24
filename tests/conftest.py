"""Fixtures. Solo datos sintéticos generados aquí: jamás se usan fotos reales del usuario."""
import os
from pathlib import Path

import pytest


def make_file(path: Path, data: bytes, mtime: int = 1_700_000_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def fake_iphone(tmp_path: Path) -> Path:
    root = tmp_path / "iphone"
    make_file(root / "DCIM/100APPLE/IMG_0001.HEIC", b"heic-bytes-" * 1000, 1_700_000_001)
    make_file(root / "DCIM/100APPLE/IMG_0001.MOV", b"mov-bytes-" * 5000, 1_700_000_001)
    make_file(root / "DCIM/100APPLE/IMG_0002.JPG", b"jpg-bytes-" * 800, 1_700_000_002)
    make_file(root / "DCIM/101APPLE/IMG_0002.JPG", b"otro-contenido-" * 700, 1_700_000_003)  # mismo nombre, otra carpeta
    make_file(root / "DCIM/101APPLE/IMG_0003.AAE", b"<plist/>", 1_700_000_004)
    make_file(root / "DCIM/ispRegDump.bin", b"\x00" * 64, 1_700_000_005)
    return root
