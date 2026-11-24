import sqlite3

import pytest

from iphonephotosync.domain.place import PlaceLevel
from iphonephotosync.geolocation.builder import build_db, pick_spanish_names
from iphonephotosync.geolocation.offline import OfflineGeocoder, haversine_km


def city(gid, name, lat, lon, cc, admin1, pop=100000, fcode="PPL"):
    f = [""] * 19
    f[0], f[1], f[2], f[4], f[5], f[7], f[8], f[10], f[14] = (
        str(gid), name, name, str(lat), str(lon), fcode, cc, admin1, str(pop))
    return "\t".join(f) + "\n"


def country(iso, name, gid):
    f = [""] * 17
    f[0], f[4], f[16] = iso, name, str(gid)
    return "\t".join(f) + "\n"


def alt(aid, gid, lang, name, preferred=0, short=0, colloq=0, historic=0):
    return f"{aid}\t{gid}\t{lang}\t{name}\t{preferred}\t{short}\t{colloq}\t{historic}\n"


CITIES = [
    city(1, "Barcelona", 41.3888, 2.159, "ES", "56"),
    city(2, "Madrid", 40.4165, -3.7026, "ES", "29"),
    city(3, "Girona", 41.9831, 2.8249, "ES", "56"),
    city(4, "Paris", 48.8534, 2.3488, "FR", "11"),
    city(5, "Santa Ana", 13.9942, -89.5597, "SV", "10"),
    city(6, "Santa Ana", 13.5, -89.9, "SV", "11"),      # mismo nombre y país, otra región
    city(7, "Santa Ana", 13.6, -89.2, "SV", "10"),      # mismo nombre, país y región
    city(8, "Suva", -18.1416, 178.4415, "FJ", "08"),
    city(9, "Taveuni Town", -16.85, -179.95, "FJ", "03"),
]
ADMIN1 = ["ES.56\tCatalonia\tCatalonia\t3336901\n", "ES.29\tMadrid\tMadrid\t3117732\n", "FR.11\tIle-de-France\tIle-de-France\t3012874\n",
          "SV.10\tSanta Ana\tSanta Ana\t3584\n", "SV.11\tSonsonate\tSonsonate\t3585\n",
          "FJ.08\tCentral\tCentral\t1\n", "FJ.03\tNorthern\tNorthern\t2\n"]
COUNTRIES = ["# comentario\n", country("ES", "Spain", 2510769), country("FR", "France", 3017382),
             country("SV", "El Salvador", 3585968), country("FJ", "Fiji", 2205218)]
ALT = [alt(1, 1, "es", "Barcelona", 1), alt(2, 4, "es", "París", 1), alt(3, 4, "es", "Paris-colq", 0, 0, 1),
       alt(4, 4, "en", "Paris", 1), alt(5, 2510769, "es", "España", 1), alt(6, 3017382, "es", "Francia", 1),
       alt(7, 3336901, "es", "Cataluña", 1), alt(8, 3012874, "es", "Isla de Francia", 1), alt(9, 4, "es", "Lutecia", 0, 0, 0, 1)]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("geo") / "geonames.sqlite"
    stats = build_db(out, CITIES, ADMIN1, COUNTRIES, ALT, "test")
    assert stats["cities"] == 9 and stats["spanish_names"] >= 5
    return out


def geo(db, **kw):
    return OfflineGeocoder(db, **kw)


def test_city_within_radius_spanish_names(db):
    p = geo(db).lookup(41.40, 2.17)
    assert (p.level, p.folder_name()) == (PlaceLevel.CITY, "Barcelona, España")
    assert geo(db).lookup(48.86, 2.35).folder_name() == "París, Francia"      # ignora coloquial e histórico


def test_english_names_when_configured(db):
    assert geo(db, language="en").lookup(48.86, 2.35).folder_name() == "Paris, France"


def test_falls_back_to_region_then_country_then_none(db):
    g = geo(db)
    near_region = g.lookup(41.3888 + 0.9, 2.159)              # ~100 km al norte de Barcelona; Girona queda más cerca
    assert near_region is not None and near_region.level in (PlaceLevel.CITY, PlaceLevel.REGION)
    p = g.lookup(40.4165, -3.7026 + 1.6)                      # ~135 km de Madrid
    assert (p.level, p.folder_name()) == (PlaceLevel.REGION, "Madrid, España")
    p = g.lookup(40.4165, -3.7026 + 4.0)                      # ~340 km
    assert (p.level, p.folder_name()) == (PlaceLevel.COUNTRY, "España")
    assert g.lookup(0.0, -30.0) is None                       # océano: no se inventa un lugar


def test_max_detail_coarsens(db):
    assert geo(db, max_detail=PlaceLevel.REGION).lookup(41.40, 2.17).folder_name() == "Cataluña, España"
    assert geo(db, max_detail=PlaceLevel.COUNTRY).lookup(41.40, 2.17).folder_name() == "España"


def test_same_city_name_disambiguation_is_order_independent(db):
    g = geo(db)
    a = g.lookup(13.5, -89.9).folder_name()           # Santa Ana en SV.11 (única en su región)
    b = g.lookup(13.9942, -89.5597).folder_name()     # Santa Ana en SV.10 (hay otra en la misma región)
    assert a == "Santa Ana (Sonsonate), El Salvador"
    assert b.startswith("Santa Ana (Santa Ana) [5]") and b.endswith(", El Salvador")
    assert a != b


def test_antimeridian_lookup(db):
    p = geo(db).lookup(-16.85, 179.99)                # ~ 10 km de Taveuni, al otro lado de ±180
    assert p is not None and p.city == "Taveuni Town"


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_invalid_coordinates_return_none(db, lat, lon):
    assert geo(db).lookup(lat, lon) is None


def test_lookup_is_deterministic(db):
    g = geo(db)
    assert [g.lookup(41.4, 2.2) for _ in range(3)] == [g.lookup(41.4, 2.2)] * 3


def test_database_is_opened_read_only(db):
    g = geo(db)
    with pytest.raises(sqlite3.OperationalError):
        g._con.execute("DELETE FROM cities")


def test_missing_database_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        OfflineGeocoder(tmp_path / "no.sqlite")


def test_city_districts_and_historical_places_are_not_cities(tmp_path):
    """Regresión: 'Paris 04 Hôtel-de-Ville' (PPLX) ganaba a 'París' como ciudad más cercana."""
    lines = [city(4, "Paris", 48.8534, 2.3488, "FR", "11", fcode="PPLC"),
             city(21, "Paris 04 Hotel-de-Ville", 48.8549, 2.3566, "FR", "11", fcode="PPLX"),
             city(22, "Antigua", 48.86, 2.36, "FR", "11", fcode="PPLH")]
    out = tmp_path / "g.sqlite"
    stats = build_db(out, lines, ADMIN1, COUNTRIES, ALT, "test")
    assert stats["cities"] == 1
    assert OfflineGeocoder(out).lookup(48.8549, 2.3566).city == "París"


def test_suburb_of_much_larger_city_resolves_to_the_larger_city(tmp_path):
    """Regresión con datos reales: 'Paris 15 Vaugirard' (PPL, 230 mil) debe ser 'París' (2,1 M)."""
    lines = [city(4, "Paris", 48.8534, 2.3488, "FR", "11", 2_138_551, "PPLC"),
             city(31, "Paris 15 Vaugirard", 48.8400, 2.3000, "FR", "11", 229_713),          # ~4 km
             city(32, "Ville Lejana", 48.5000, 2.3488, "FR", "11", 10_000),                 # ~39 km: otra ciudad
             city(33, "Pueblo Vecino", 48.9500, 2.3488, "FR", "11", 9_000),                 # ~10 km de París, 9 mil
             city(34, "Gemela", 48.8700, 2.4000, "FR", "11", 2_000_000)]                    # tamaño similar a París
    out = tmp_path / "g.sqlite"
    build_db(out, lines, ADMIN1, COUNTRIES, ALT, "test")
    g = OfflineGeocoder(out)
    assert g.lookup(48.8400, 2.3000).city == "París"                  # barrio -> ciudad principal
    assert g.lookup(48.5001, 2.3488).city == "Ville Lejana"           # lejos (> 15 km) de una ciudad mayor: se queda
    assert g.lookup(48.8700, 2.4000).city != "París"                  # pop. similar (< 4×): no se fusiona
    assert OfflineGeocoder(out, parent_pop_factor=1e9).lookup(48.8400, 2.3000).city == "Paris 15 Vaugirard"


def test_merge_is_chained_through_ppl_districts(tmp_path):
    """Regresión real: un suburbio se unía a 'Paris 20 Ménilmontant' (PPL, 185 mil) en vez de a París."""
    lines = [city(4, "Paris", 48.8534, 2.3488, "FR", "11", 2_138_551, "PPLC"),
             city(41, "Distrito PPL", 48.8534, 2.3944, "FR", "11", 200_000),        # ~3,3 km de París
             city(42, "Suburbio", 48.8534, 2.5088, "FR", "11", 30_000)]             # ~11,7 km de París, ~8,4 km del distrito
    out = tmp_path / "g.sqlite"
    build_db(out, lines, ADMIN1, COUNTRIES, ALT, "test")
    g = OfflineGeocoder(out, parent_radius_km=10.0)      # con 10 km París no es alcanzable directamente
    assert g.lookup(48.8534, 2.5088).city == "París"     # suburbio -> distrito -> París


def test_default_merge_parameters_cover_metropolitan_area(tmp_path):
    lines = [city(4, "Paris", 48.8534, 2.3488, "FR", "11", 2_138_551, "PPLC"),
             city(51, "Satélite", 48.8534, 2.7, "FR", "11", 20_000),               # ~25,7 km: dentro de 30 km
             city(52, "Lejana", 48.8534, 2.9, "FR", "11", 20_000)]                 # ~40 km: no se une
    out = tmp_path / "g.sqlite"
    build_db(out, lines, ADMIN1, COUNTRIES, ALT, "test")
    g = OfflineGeocoder(out)
    assert g.lookup(48.8534, 2.7).city == "París" and g.lookup(48.8534, 2.9).city == "Lejana"


def test_spanish_name_selection_rules():
    lines = [alt(1, 10, "es", "Corto", 0, 1), alt(2, 10, "es", "Normal"), alt(3, 10, "es", "Preferido", 1),
             alt(4, 11, "es", "Solo coloquial", 0, 0, 1), alt(5, 12, "fr", "Francés", 1), alt(6, 13, "es", "Otro")]
    out = pick_spanish_names(lines, {10, 11, 12})
    assert out == {10: "Preferido"}                   # 11 solo coloquial, 12 no es 'es', 13 no solicitado


def test_haversine_known_distance():
    assert haversine_km(41.3888, 2.159, 40.4165, -3.7026) == pytest.approx(505, abs=5)   # Barcelona–Madrid
