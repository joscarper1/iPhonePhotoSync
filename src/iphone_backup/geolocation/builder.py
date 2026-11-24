"""Construye la base SQLite local de lugares a partir de volcados de GeoNames (CC-BY 4.0).

Entradas (iterables de líneas de texto, formato GeoNames):
  cities  : cities15000.txt (tabulado)
  admin1  : admin1CodesASCII.txt (code, name, asciiname, geonameid)
  country : countryInfo.txt (comentarios con '#')
  alt_es  : líneas de alternateNamesV2 (solo se aprovechan las de idioma 'es')
Atribución: GeoNames, https://www.geonames.org, licencia CC-BY 4.0.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

# PPLX = sección de un lugar poblado (distrito, barrio); PPLH/PPLW/PPLQ = histórico/destruido/abandonado; PPLR = religioso
EXCLUDED_FEATURE_CODES = frozenset({"PPLX", "PPLH", "PPLW", "PPLQ", "PPLR"})

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE countries (iso TEXT PRIMARY KEY, name_en TEXT NOT NULL, name_es TEXT);
CREATE TABLE admin1 (code TEXT PRIMARY KEY, name_en TEXT NOT NULL, name_es TEXT);
CREATE TABLE cities (id INTEGER PRIMARY KEY, name_en TEXT NOT NULL, name_es TEXT, lat REAL NOT NULL,
                     lon REAL NOT NULL, admin1 TEXT, country TEXT NOT NULL, population INTEGER NOT NULL);
CREATE INDEX idx_cities_name ON cities(country, name_en);
CREATE VIRTUAL TABLE city_idx USING rtree(id, min_lat, max_lat, min_lon, max_lon);
"""


def pick_spanish_names(alt_lines: Iterable[str], wanted_ids: set[int]) -> dict[int, str]:
    """Mejor nombre 'es' por geonameid: preferido > no corto/coloquial/histórico > primero visto."""
    best: dict[int, tuple[int, str]] = {}
    for line in alt_lines:
        p = line.rstrip("\n").split("\t")
        if len(p) < 8 or p[2] != "es":
            continue
        try:
            gid = int(p[1])
        except ValueError:
            continue
        if gid not in wanted_ids:
            continue
        name, preferred, short, colloq, historic = p[3], p[4] == "1", p[5] == "1", p[6] == "1", p[7] == "1"
        if colloq or historic or not name:
            continue
        score = 0 if preferred else (2 if short else 1)
        if gid not in best or score < best[gid][0]:
            best[gid] = (score, name)
    return {g: n for g, (_, n) in best.items()}


def build_db(out: Path, cities: Iterable[str], admin1: Iterable[str], country: Iterable[str],
             alt_es_lines: Iterable[str] | None, source_note: str = "") -> dict[str, int]:
    city_rows, admin_rows, country_rows = [], [], []
    wanted: set[int] = set()
    for line in cities:
        p = line.rstrip("\n").split("\t")
        if len(p) < 15:
            continue
        if p[7] in EXCLUDED_FEATURE_CODES:
            continue    # distritos/barrios, lugares históricos, destruidos o abandonados: no son "ciudad"
        gid = int(p[0])
        city_rows.append((gid, p[1], float(p[4]), float(p[5]), p[10] or None, p[8], int(p[14] or 0)))
        wanted.add(gid)
    admin_gid: dict[str, int] = {}
    for line in admin1:
        p = line.rstrip("\n").split("\t")
        if len(p) < 4:
            continue
        admin_rows.append((p[0], p[1]))
        admin_gid[p[0]] = int(p[3])
        wanted.add(int(p[3]))
    country_gid: dict[str, int] = {}
    for line in country:
        if line.startswith("#") or not line.strip():
            continue
        p = line.rstrip("\n").split("\t")
        if len(p) < 17 or not p[16].strip():
            continue
        country_rows.append((p[0], p[4]))
        country_gid[p[0]] = int(p[16])
        wanted.add(int(p[16]))
    es = pick_spanish_names(alt_es_lines, wanted) if alt_es_lines is not None else {}

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO countries VALUES (?,?,?)",
                        [(c, n, es.get(country_gid[c])) for c, n in country_rows])
        con.executemany("INSERT INTO admin1 VALUES (?,?,?)",
                        [(c, n, es.get(admin_gid[c])) for c, n in admin_rows])
        con.executemany("INSERT INTO cities VALUES (?,?,?,?,?,?,?,?)",
                        [(i, n, es.get(i), la, lo, a, c, pop) for i, n, la, lo, a, c, pop in city_rows])
        con.execute("INSERT INTO city_idx SELECT id, lat, lat, lon, lon FROM cities")
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("attribution", "GeoNames (https://www.geonames.org), licencia CC-BY 4.0"),
            ("source", source_note), ("spanish_names", str(len(es)))])
        con.commit()
    finally:
        con.close()
    out.unlink(missing_ok=True)
    tmp.replace(out)
    return {"cities": len(city_rows), "admin1": len(admin_rows), "countries": len(country_rows),
            "spanish_names": len(es)}
