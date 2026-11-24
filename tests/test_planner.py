import pytest

from conftest import make_file
from iphone_backup.device.simulated import SimulatedDeviceSource
from iphone_backup.domain.models import DateSource
from iphone_backup.domain.place import Place, PlaceLevel
from iphone_backup.metadata.policy import MediaMetadata
from iphone_backup.organizer.planner import (
    OrganizerSettings, PlaceStatus, build_preview, group_leaders, plan_folders, planned_name_collisions,
)
from iphone_backup.scanner import scan


def meta(dt=None, lat=None, lon=None, cid=None):
    return MediaMetadata(dt, DateSource.EXIF if dt else DateSource.UNKNOWN, None, None, lat, lon, None, cid)


class FakeGeo:
    def __init__(self):
        self.calls = []

    def lookup(self, lat, lon):
        self.calls.append((lat, lon))
        if lat > 41:
            return Place("ES", "España", "Cataluña", "Barcelona", 1, 2.0, PlaceLevel.CITY)
        if lat > 40:
            return Place("ES", "España", "Madrid", "Madrid", 2, 2.0, PlaceLevel.CITY)
        return None


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "iphone"
    d1, d2 = root / "DCIM/100APPLE", root / "DCIM/101APPLE"
    for n, c in [("IMG_0001.HEIC", b"a"), ("IMG_0001.MOV", b"b"), ("IMG_0001.AAE", b"c"), ("IMG_0002.JPG", b"d"),
                 ("IMG_0003.PNG", b"e"), ("IMG_0004.MOV", b"f"), ("IMG_0009.AAE", b"g"), ("IMG_0005.HEIC", b"h"),
                 ("IMG_0006.JPG", b"i")]:
        make_file(d1 / n, c)
    make_file(d2 / "IMG_0002.JPG", b"otro")
    make_file(root / "DCIM/ispRegDump.bin", b"\0")
    return root


def p(path):
    return "/DCIM/" + path


@pytest.fixture
def setup(tree):
    s = scan(SimulatedDeviceSource(tree))
    metas = {
        p("100APPLE/IMG_0001.HEIC"): meta("2026-11-05T10:00:00", 41.39, 2.16, "cid1"),
        p("100APPLE/IMG_0001.MOV"): meta("2026-11-05T10:00:01", 41.39, 2.16, "cid1"),
        p("100APPLE/IMG_0002.JPG"): meta("2026-11-20T09:00:00", 40.41, -3.70),
        p("101APPLE/IMG_0002.JPG"): meta("2026-11-20T09:30:00", 40.41, -3.70),
        p("100APPLE/IMG_0003.PNG"): meta(),                                       # sin fecha ni GPS
        p("100APPLE/IMG_0004.MOV"): meta("2024-01-02T03:04:05"),                  # sin GPS
        p("100APPLE/IMG_0005.HEIC"): meta("2025-07-01T00:00:00", 10.0, 10.0),     # GPS sin lugar cercano
        p("100APPLE/IMG_0006.JPG"): meta(None, 41.39, 2.16),                      # GPS pero sin fecha
    }
    pairs = {(p("100APPLE/IMG_0001.HEIC"), p("100APPLE/IMG_0001.MOV"))}
    return s, metas, pairs


def test_live_pair_and_aae_follow_the_still(setup):
    s, metas, pairs = setup
    lead = group_leaders(s, pairs)
    still = p("100APPLE/IMG_0001.HEIC")
    assert lead[p("100APPLE/IMG_0001.MOV")] == still and lead[p("100APPLE/IMG_0001.AAE")] == still
    assert lead[p("100APPLE/IMG_0009.AAE")] is None                    # .AAE huérfano
    assert p("ispRegDump.bin") not in lead                              # lo ignorado no se planifica


def test_unconfirmed_pair_does_not_follow(setup):
    s, _, _ = setup
    lead = group_leaders(s, set())
    assert lead[p("100APPLE/IMG_0001.MOV")] == p("100APPLE/IMG_0001.MOV")


def test_full_plan_default_template(setup):
    s, metas, pairs = setup
    plans = plan_folders(s, metas, OrganizerSettings(), FakeGeo(), pairs)
    d = {k: v.rel_dir for k, v in plans.items()}
    assert d[p("100APPLE/IMG_0001.HEIC")] == ("2026", "11 - Noviembre", "Barcelona, España")
    assert d[p("100APPLE/IMG_0001.MOV")] == d[p("100APPLE/IMG_0001.AAE")] == d[p("100APPLE/IMG_0001.HEIC")]
    assert d[p("100APPLE/IMG_0002.JPG")] == ("2026", "11 - Noviembre", "Madrid, España")
    assert d[p("100APPLE/IMG_0004.MOV")] == ("2024", "01 - Enero", "Sin ubicación")
    assert d[p("100APPLE/IMG_0005.HEIC")] == ("2025", "07 - Julio", "Ubicación no identificada")
    assert d[p("100APPLE/IMG_0003.PNG")] == ("Fecha desconocida", "Sin ubicación")
    assert d[p("100APPLE/IMG_0006.JPG")] == ("Fecha desconocida", "Barcelona, España")
    assert d[p("100APPLE/IMG_0009.AAE")] == ("Fecha desconocida", "Sin ubicación")
    assert plans[p("100APPLE/IMG_0005.HEIC")].place_status is PlaceStatus.UNIDENTIFIED
    assert plans[p("100APPLE/IMG_0003.PNG")].date_unknown


@pytest.mark.parametrize("template,expected", [
    ("Año/Mes", ("2026", "11 - Noviembre")),
    ("Año/Ubicación/Mes", ("2026", "Barcelona, España", "11 - Noviembre")),
])
def test_other_templates(setup, template, expected):
    s, metas, pairs = setup
    plans = plan_folders(s, metas, OrganizerSettings(template=template), FakeGeo(), pairs)
    assert plans[p("100APPLE/IMG_0001.HEIC")].rel_dir == expected


def test_gps_disabled_never_calls_geocoder(setup):
    s, metas, pairs = setup
    geo = FakeGeo()
    plans = plan_folders(s, metas, OrganizerSettings(use_gps=False), geo, pairs)
    assert geo.calls == []
    assert plans[p("100APPLE/IMG_0001.HEIC")].rel_dir == ("2026", "11 - Noviembre")
    assert plans[p("100APPLE/IMG_0003.PNG")].rel_dir == ("Fecha desconocida",)


def test_geocoder_called_once_per_distinct_coordinate(setup):
    s, metas, pairs = setup
    geo = FakeGeo()
    plan_folders(s, metas, OrganizerSettings(), geo, pairs)
    assert len(geo.calls) == len(set(geo.calls))


def test_unknown_template_rejected(setup):
    s, metas, _ = setup
    with pytest.raises(ValueError):
        plan_folders(s, metas, OrganizerSettings(template="Mes/Año"), None, set())


def test_without_geocoder_gps_items_are_unidentified_not_invented(setup):
    s, metas, pairs = setup
    plans = plan_folders(s, metas, OrganizerSettings(), None, pairs)
    assert plans[p("100APPLE/IMG_0001.HEIC")].rel_dir[-1] == "Ubicación no identificada"


def test_name_collision_is_reported_in_preview(setup):
    s, metas, pairs = setup
    plans = plan_folders(s, metas, OrganizerSettings(), FakeGeo(), pairs)
    coll = planned_name_collisions(s, plans)
    assert coll == [(p("100APPLE/IMG_0002.JPG"), p("101APPLE/IMG_0002.JPG"))]


def test_preview_tree_matches_plan_and_is_read_only(setup, tree):
    s, metas, pairs = setup
    plans = plan_folders(s, metas, OrganizerSettings(), FakeGeo(), pairs)
    before = sorted(x.name for x in tree.rglob("*"))
    pv = build_preview(s, plans)
    assert sorted(x.name for x in tree.rglob("*")) == before
    assert pv.total == len(plans) == 10 and pv.unknown_date == 3 and pv.name_collisions == 1
    assert pv.lines[0] == "2024 (1 elementos)"
    assert "2026 (5 elementos)" in pv.lines and "Fecha desconocida (3 elementos)" in pv.lines
    assert sum(1 for ln in pv.lines if ln and ln[0].isdigit()) == 3          # 2024, 2025, 2026
    assert "    ├── Barcelona, España (3 elementos)" in pv.lines
    assert "└── Sin ubicación (2 elementos)" in "\n".join(pv.lines)
    assert sum(pv_count for pv_count in [pv.no_location, pv.unidentified_location]) == 4
