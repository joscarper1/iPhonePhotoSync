"""Caso de uso completo sobre un iPhone simulado con medios sintéticos y ExifTool real."""
import json

import pytest

from media_fixtures import make_jpeg, make_mov, make_png, needs_exiftool
from iphonephotosync.app.planning import build_plan
from iphonephotosync.cli.__main__ import main
from iphonephotosync.device.simulated import SimulatedDeviceSource
from iphonephotosync.domain.place import Place, PlaceLevel
from iphonephotosync.metadata.exiftool_service import ExifToolService
from iphonephotosync.organizer.planner import OrganizerSettings

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
CID = "ABCDEF01-2345-6789-ABCD-EF0123456789"
GPS = {"GPS:GPSLatitude": "41.3851", "GPS:GPSLatitudeRef": "N", "GPS:GPSLongitude": "2.1734", "GPS:GPSLongitudeRef": "E"}


class Geo:
    def lookup(self, lat, lon):
        return Place("ES", "España", "Cataluña", "Barcelona", 1, 1.0, PlaceLevel.CITY)


@pytest.fixture
def iphone(tmp_path):
    d = tmp_path / "iphone/DCIM/100APPLE"
    make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00", **GPS})
    make_mov(d / "IMG_0001.MOV", {"Keys:CreationDate": "2024:03:10 12:00:01-06:00", "Keys:ContentIdentifier": CID})
    make_jpeg(d / "IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2023:12:31 23:59:59"})
    make_png(d / "IMG_0003.PNG")
    make_mov(d / "IMG_0004.MOV", {"Keys:CreationDate": "2025:01:02 03:04:05+01:00",
                                  "Keys:GPSCoordinates": "+41.3851+002.1734+000.000/"}, mdat_before_moov=200_000)
    return tmp_path / "iphone"


def test_build_plan_end_to_end(iphone):
    with ExifToolService() as et:
        plan = build_plan(SimulatedDeviceSource(iphone), et, Geo(), OrganizerSettings())
    d = {p.rsplit("/", 1)[1]: v.rel_dir for p, v in plan.plans.items()}
    assert d["IMG_0001.JPG"] == ("2024", "03 - Marzo", "Barcelona, España")
    assert d["IMG_0002.JPG"] == ("2023", "12 - Diciembre", "Sin ubicación")
    assert d["IMG_0003.PNG"] == ("Fecha desconocida", "Sin ubicación")
    assert d["IMG_0004.MOV"] == ("2025", "01 - Enero", "Barcelona, España")
    assert plan.probe_errors == {}
    assert plan.preview.unknown_date == 1


def test_one_sided_content_identifier_is_not_a_confirmed_pair(iphone):
    with ExifToolService() as et:
        plan = build_plan(SimulatedDeviceSource(iphone), et, Geo(), OrganizerSettings())
    assert plan.confirmed_pairs == set()
    assert list(plan.unconfirmed) == ["Solo uno de los archivos declara ContentIdentifier"]
    # sin confirmar, el MOV decide su propia carpeta (no se fuerza ni se descarta)
    assert plan.plans["/DCIM/100APPLE/IMG_0001.MOV"].leader == "/DCIM/100APPLE/IMG_0001.MOV"


def test_tag_cache_avoids_reopening_unchanged_files_and_same_plan(iphone):
    cache: dict = {}
    with ExifToolService() as et:
        src = SimulatedDeviceSource(iphone)
        first = build_plan(src, et, Geo(), OrganizerSettings(), tag_cache=cache)
        assert len(cache) == 5 and json.dumps(cache)                 # serializable: se guarda en JSON
        before = src.bytes_read
        second = build_plan(src, et, Geo(), OrganizerSettings(), tag_cache=json.loads(json.dumps(cache)))
    assert src.bytes_read == before                                  # 2.ª vez: cero lecturas del iPhone
    assert {p: v.rel_dir for p, v in first.plans.items()} == {p: v.rel_dir for p, v in second.plans.items()}


def test_device_loss_during_metadata_aborts_instead_of_building_a_wrong_plan(iphone):
    from iphonephotosync.ports.device import DeviceDisconnected
    src = SimulatedDeviceSource(iphone)
    src.faults.disconnect_after_bytes = 100
    with ExifToolService() as et, pytest.raises(DeviceDisconnected):
        build_plan(src, et, Geo(), OrganizerSettings())


def test_unreadable_metadata_is_reported_but_file_is_still_planned_for_backup(iphone):
    (iphone / "DCIM/100APPLE/IMG_0004.MOV").write_bytes(b"\x00" * 50)         # moov inexistente
    with ExifToolService() as et:
        plan = build_plan(SimulatedDeviceSource(iphone), et, Geo(), OrganizerSettings())
    assert "/DCIM/100APPLE/IMG_0004.MOV" in plan.probe_errors                 # el error se informa
    # ...pero el archivo NO se descarta: se respalda igualmente, sin inventar fecha ni lugar
    assert plan.plans["/DCIM/100APPLE/IMG_0004.MOV"].rel_dir == ("Fecha desconocida", "Sin ubicación")
    assert plan.plans["/DCIM/100APPLE/IMG_0004.MOV"].date_unknown


def test_cli_preview_simulated(iphone, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("iphonephotosync.geolocation.offline.default_db_path", lambda: tmp_path / "ausente.sqlite")
    out = tmp_path / "p.json"
    assert main(["preview", "--simulate", str(iphone), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Fecha desconocida" in text and "Total: 5" in text
    assert json.loads(out.read_text(encoding="utf-8"))["total"] == 5
