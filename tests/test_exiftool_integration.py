"""Integración con ExifTool real sobre medios sintéticos. Se omite si ExifTool no está instalado."""
import hashlib
import struct

import pytest

from media_fixtures import make_jpeg, make_mov, make_png, needs_exiftool
from iphone_backup.device.simulated import SimulatedDeviceSource
from iphone_backup.domain.filetypes import FileKind
from iphone_backup.domain.models import DateSource
from iphone_backup.metadata.device_probe import make_tmp_dir, probe_tags
from iphone_backup.metadata.exiftool_service import ExifToolError, ExifToolService
from iphone_backup.metadata.policy import interpret
from iphone_backup.metadata.quicktime_probe import extract_ftyp_moov

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
CID = "ABCDEF01-2345-6789-ABCD-EF0123456789"
KEYS = {"Keys:CreationDate": "2024:05:01 23:30:00-06:00", "Keys:ContentIdentifier": CID,
        "Keys:GPSCoordinates": "+41.3851-002.1734+012.000/"}


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


def test_jpeg_exif_date_offset_gps(tmp_path, et):
    p = make_jpeg(tmp_path / "a.jpg", {"ExifIFD:DateTimeOriginal": "2021:07:15 08:09:10",
                                       "ExifIFD:OffsetTimeOriginal": "+02:00",
                                       "GPS:GPSLatitude": "41.3851", "GPS:GPSLatitudeRef": "N",
                                       "GPS:GPSLongitude": "2.1734", "GPS:GPSLongitudeRef": "W"})
    m = interpret(et.read(p), FileKind.PHOTO)
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2021-07-15T08:09:10", DateSource.EXIF, "+02:00")
    assert (m.gps_lat, m.gps_lon) == (41.3851, -2.1734)          # el signo viene de la referencia W


def test_png_without_metadata_is_unknown(tmp_path, et):
    m = interpret(et.read(make_png(tmp_path / "s.png")), FileKind.PHOTO)
    assert m.date_source is DateSource.UNKNOWN and not m.has_gps


def test_mov_keys_metadata(tmp_path, et):
    m = interpret(et.read(make_mov(tmp_path / "v.mov", KEYS)), FileKind.VIDEO)
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2024-05-01T23:30:00", DateSource.QUICKTIME_KEYS, "-06:00")
    assert (m.gps_lat, m.gps_lon) == (41.3851, -2.1734) and m.content_identifier == CID.lower()


def test_files_are_not_modified_by_reading(tmp_path, et):
    p = make_mov(tmp_path / "v.mov", KEYS)
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    et.read(p)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before


def test_moov_at_end_is_found_without_reading_whole_file(tmp_path, et):
    p = make_mov(tmp_path / "big.mov", KEYS, mdat_before_moov=3_000_000)
    data = p.read_bytes()
    reads = []

    def read_at(off, n):
        reads.append(n)
        return data[off:off + n]

    blob = extract_ftyp_moov(read_at, len(data))
    assert blob is not None and sum(reads) < 50_000               # saltó los 3 MB del mdat
    q = tmp_path / "x.mov"
    q.write_bytes(blob)
    m = interpret(et.read(q), FileKind.VIDEO)
    assert m.date_source is DateSource.QUICKTIME_KEYS and m.content_identifier == CID.lower()


@pytest.mark.parametrize("data", [b"", b"\x00" * 7, struct.pack(">I4s", 100, b"free") + b"x" * 10,
                                  struct.pack(">I4s", 8, b"free") * 3])
def test_extract_moov_handles_garbage(data):
    assert extract_ftyp_moov(lambda o, n: data[o:o + n], len(data)) is None


def test_probe_tags_through_device_port(tmp_path, et):
    root = tmp_path / "iphone"
    make_jpeg(root / "DCIM/100APPLE/IMG_1.JPG", {"ExifIFD:DateTimeOriginal": "2020:01:02 03:04:05"})
    make_mov(root / "DCIM/100APPLE/IMG_2.MOV", KEYS, mdat_before_moov=500_000)
    dev = SimulatedDeviceSource(root)
    with make_tmp_dir() as td:
        from pathlib import Path
        files = {f.name: f for f in dev.list_files()}
        photo = interpret(probe_tags(dev, files["IMG_1.JPG"], et, Path(td)), FileKind.PHOTO)
        vid = interpret(probe_tags(dev, files["IMG_2.MOV"], et, Path(td)), FileKind.VIDEO)
        assert list(Path(td).iterdir()) == []                      # no deja temporales
    assert photo.capture_dt == "2020-01-02T03:04:05" and vid.content_identifier == CID.lower()


def test_service_reports_missing_exe(tmp_path):
    with pytest.raises(ExifToolError):
        ExifToolService(tmp_path / "nope.exe").start()


def test_service_handles_many_files_with_one_process(tmp_path, et):
    paths = [make_jpeg(tmp_path / f"{i}.jpg", {"ExifIFD:DateTimeOriginal": f"2020:01:{i + 1:02d} 00:00:00"})
             for i in range(15)]
    days = [interpret(et.read(p), FileKind.PHOTO).capture_dt[:10] for p in paths]
    assert days == [f"2020-01-{i + 1:02d}" for i in range(15)]
