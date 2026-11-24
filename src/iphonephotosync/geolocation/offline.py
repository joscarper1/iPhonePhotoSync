"""Geocodificador offline: ciudad más cercana con radios de confianza.

Reglas (configurables):
  * ≤ city_radius_km   -> ciudad
  * ≤ region_radius_km -> región (la de la ciudad más cercana)
  * ≤ country_radius_km-> solo país
  * más lejos          -> None (no se inventa un lugar)
Limitación documentada: cerca de una frontera la ciudad más cercana puede estar en el país vecino.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from iphonephotosync.domain.place import Place, PlaceLevel

EARTH_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def default_db_path() -> Path:
    """Base GeoNames incluida como dato del paquete (también dentro de la app empaquetada)."""
    return Path(__file__).resolve().parents[1] / "data" / "geonames.sqlite"


class OfflineGeocoder:
    def __init__(self, db_path: Path | str | None = None, language: str = "es",
                 city_radius_km: float = 40.0, region_radius_km: float = 150.0,
                 country_radius_km: float = 500.0, max_detail: PlaceLevel = PlaceLevel.CITY,
                 parent_radius_km: float = 30.0, parent_pop_factor: float = 3.0):
        path = Path(db_path) if db_path else default_db_path()
        if not path.is_file():
            raise FileNotFoundError(f"Falta la base de lugares: {path}")
        self._con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self._lang = language
        self._radii = (city_radius_km, region_radius_km, country_radius_km)
        self._parent_radius, self._parent_factor = parent_radius_km, parent_pop_factor
        self._max_detail = max_detail

    def close(self) -> None:
        self._con.close()

    def _n(self, es: str | None, en: str | None) -> str | None:
        return (es or en) if self._lang == "es" else en

    def _candidates(self, lat: float, lon: float, radius_km: float):
        dlat = radius_km / 111.0
        cos = max(math.cos(math.radians(lat)), 0.01)
        dlon = min(180.0, radius_km / (111.0 * cos))
        lat_lo, lat_hi = max(-90.0, lat - dlat), min(90.0, lat + dlat)
        ranges = [(lon - dlon, lon + dlon)]
        if lon - dlon < -180:
            ranges = [(-180.0, lon + dlon), (lon - dlon + 360, 180.0)]
        elif lon + dlon > 180:
            ranges = [(lon - dlon, 180.0), (-180.0, lon + dlon - 360)]
        for lo, hi in ranges:
            yield from self._con.execute(
                "SELECT c.id, c.name_en, c.name_es, c.lat, c.lon, c.admin1, c.country, c.population FROM city_idx i "
                "JOIN cities c ON c.id=i.id WHERE i.min_lat>=? AND i.max_lat<=? AND i.min_lon>=? AND i.max_lon<=?",
                (lat_lo, lat_hi, lo, hi))

    def lookup(self, lat: float, lon: float) -> Place | None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        for idx, radius in enumerate(self._radii):
            best, best_d = None, None
            for row in self._candidates(lat, lon, radius):
                d = haversine_km(lat, lon, row[3], row[4])
                if best_d is None or d < best_d or (d == best_d and row[0] < best[0]):
                    best, best_d = row, d
            if best is not None and best_d <= radius:
                if idx == 0:
                    for _ in range(4):      # encadenado: suburbio -> barrio PPL -> ciudad principal
                        nxt, nd = self._dominant_parent(best, lat, lon, best_d)
                        if nxt[0] == best[0]:
                            break
                        best, best_d = nxt, nd
                return self._build(best, best_d, PlaceLevel(idx)).coarsened(self._max_detail)
        return None

    def _dominant_parent(self, best, lat: float, lon: float, best_d: float):
        """Si la ciudad más cercana es un barrio/suburbio de una ciudad mayor (≥ factor veces su
        población y a ≤ parent_radius_km), se usa la mayor: 'Paris 15 Vaugirard' -> 'Paris'.
        Se aplica de forma encadenada desde ``lookup``. Valores por defecto: 30 km y 3× (área metropolitana)."""
        parent = None
        for row in self._candidates(best[3], best[4], self._parent_radius):
            if row[6] != best[6] or row[7] < best[7] * self._parent_factor:
                continue
            if haversine_km(best[3], best[4], row[3], row[4]) > self._parent_radius:
                continue
            if parent is None or row[7] > parent[7] or (row[7] == parent[7] and row[0] < parent[0]):
                parent = row
        if parent is None:
            return best, best_d
        return parent, haversine_km(lat, lon, parent[3], parent[4])

    def _build(self, row, dist: float, level: PlaceLevel) -> Place:
        cid, name_en, name_es, _, _, admin1, cc, _pop = row
        c = self._con.execute("SELECT name_en, name_es FROM countries WHERE iso=?", (cc,)).fetchone()
        country = self._n(c[1], c[0]) if c else cc
        region = None
        if admin1:
            r = self._con.execute("SELECT name_en, name_es FROM admin1 WHERE code=?", (f"{cc}.{admin1}",)).fetchone()
            region = self._n(r[1], r[0]) if r else None
        city = self._n(name_es, name_en)
        amb_city = amb_region = False
        if level is PlaceLevel.CITY:
            same = self._con.execute(
                "SELECT id, admin1 FROM cities WHERE country=? AND id<>? AND COALESCE(name_es, name_en)=?"
                if self._lang == "es" else
                "SELECT id, admin1 FROM cities WHERE country=? AND id<>? AND name_en=?",
                (cc, cid, city)).fetchall()
            amb_city = bool(same)
            amb_region = any(a == admin1 for _, a in same)
        return Place(country_code=cc, country=country or cc, region=region, city=city, city_id=cid,
                     distance_km=round(dist, 1), level=level, ambiguous_city_name=amb_city,
                     ambiguous_in_region=amb_region)
