"""Construye src/iphone_backup/data/geonames.sqlite desde los volcados de GeoNames descargados en una carpeta.

Uso (los datos descargados se tratan como NO confiables: se ejecuta aislado con -I):
    python -I tools\\build_geonames_db.py --src <carpeta_con_descargas> --out src\\iphone_backup\\data\\geonames.sqlite
Archivos esperados en --src: cities15000.zip, countryInfo.txt, admin1CodesASCII.txt, alternateNamesV2.zip (opcional)
"""
import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # código propio y de confianza
from iphone_backup.geolocation.builder import build_db  # noqa: E402


def lines_from_zip(zip_path: Path, member: str):
    with zipfile.ZipFile(zip_path) as z:
        with z.open(member) as f:
            for raw in f:
                yield raw.decode("utf-8", errors="replace")


def lines_from_text(p: Path):
    with open(p, encoding="utf-8", errors="replace") as f:
        yield from f


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    alt = a.src / "alternateNamesV2.zip"
    stats = build_db(
        a.out,
        cities=lines_from_zip(a.src / "cities15000.zip", "cities15000.txt"),
        admin1=lines_from_text(a.src / "admin1CodesASCII.txt"),
        country=lines_from_text(a.src / "countryInfo.txt"),
        alt_es_lines=lines_from_zip(alt, "alternateNamesV2.txt") if alt.is_file() else None,
        source_note="GeoNames dump " + "(cities15000, countryInfo, admin1CodesASCII, alternateNamesV2[es])",
    )
    print(stats)
