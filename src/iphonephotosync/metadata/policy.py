"""Política de interpretación de fecha, GPS e identificador Live Photo a partir de etiquetas ExifTool.

Reglas (requerimiento §5):
  * La fecha es la de CAPTURA, jamás la de copia.
  * Se conserva la hora local tal como está escrita; NO se convierten zonas horarias.
  * Si solo hay fecha UTC (vídeos sin Keys:CreationDate) se usa tal cual y se marca ``QUICKTIME_UTC``:
    puede diferir del día local; queda registrado.
  * Sin fecha confiable -> ``UNKNOWN`` (nunca se inventa). La fecha de modificación del archivo solo
    se usa si el usuario lo activa explícitamente y queda marcada como ``FILE_MTIME`` (baja confianza).
  * Coordenadas (0, 0) o fuera de rango se descartan.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from iphonephotosync.domain.filetypes import FileKind
from iphonephotosync.domain.models import DateSource

_DATE_RE = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
MIN_YEAR = 2007   # año del primer iPhone: antes de eso la fecha es basura (p. ej. 1904 o 0000)

EXIF_DATE_PRIORITY = ("DateTimeOriginal", "CreateDate")
PHOTO_GROUPS = ("ExifIFD", "IFD0", "XMP-exif", "XMP-photoshop")


@dataclass(frozen=True)
class MediaMetadata:
    capture_dt: str | None            # 'YYYY-MM-DDTHH:MM:SS' hora local tal como está escrita
    date_source: DateSource
    tz_info: str | None               # '+02:00', 'UTC' o None (no declarada)
    date_note: str | None
    gps_lat: float | None
    gps_lon: float | None
    gps_note: str | None
    content_identifier: str | None
    make: str | None = None
    model: str | None = None

    @property
    def has_date(self) -> bool:
        return self.capture_dt is not None

    @property
    def has_gps(self) -> bool:
        return self.gps_lat is not None


def find(tags: dict, name: str, groups: tuple[str, ...] | None = None):
    """Valor de la etiqueta ``name`` (sin grupo), prefiriendo los grupos dados."""
    if groups:
        for g in groups:
            if f"{g}:{name}" in tags:
                return tags[f"{g}:{name}"]
    for k, v in tags.items():
        if k.split(":", 1)[-1] == name and (groups is None):
            return v
    return None


def parse_exif_datetime(value: object, now: datetime | None = None) -> tuple[str, str | None] | None:
    """'2024:05:01 12:30:15[.ss][±HH:MM]' -> ('2024-05-01T12:30:15', '±HH:MM'|'UTC'|None). None si inválida."""
    if not isinstance(value, str):
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    now = now or datetime.now(timezone.utc)
    if y < MIN_YEAR or y > now.year + 1:
        return None
    try:
        datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    tz = m.group(7)
    if tz == "Z":
        tz = "UTC"
    elif tz and ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}", tz


def _norm_offset(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[+-]\d{2}:\d{2}", value.strip()):
        return value.strip()
    return None


def _pick_date(tags: dict, kind: FileKind, now: datetime | None):
    """Devuelve (capture_dt, source, tz, note) o None."""
    note_bad = None
    if kind is FileKind.VIDEO:
        keys = find(tags, "CreationDate", ("Keys",))
        parsed = parse_exif_datetime(keys, now)
        if parsed:
            return parsed[0], DateSource.QUICKTIME_KEYS, parsed[1], None
        if keys is not None:
            note_bad = "Keys:CreationDate inválida"
        qt = find(tags, "CreateDate", ("QuickTime",))
        parsed = parse_exif_datetime(qt, now)
        if parsed:
            return (parsed[0], DateSource.QUICKTIME_UTC, "UTC",
                    "Solo hay fecha UTC del contenedor; el día local puede diferir")
        if qt is not None:
            note_bad = (note_bad + "; " if note_bad else "") + "QuickTime:CreateDate inválida"
        return ("BAD", note_bad) if note_bad else None
    for name in EXIF_DATE_PRIORITY:
        raw = find(tags, name, PHOTO_GROUPS)
        parsed = parse_exif_datetime(raw, now)
        if parsed:
            tz = parsed[1]
            if tz is None and name == "DateTimeOriginal":
                tz = _norm_offset(find(tags, "OffsetTimeOriginal", PHOTO_GROUPS))
            elif tz is None:
                tz = _norm_offset(find(tags, "OffsetTimeDigitized", PHOTO_GROUPS))
            return parsed[0], DateSource.EXIF, tz, None
        if raw is not None:
            note_bad = f"{name} inválida"
    return ("BAD", note_bad) if note_bad else None


def _valid_coord(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _pick_gps(tags: dict):
    """(lat, lon, note). Prefiere Composite (con signo correcto); GPS:GPSLongitude crudo no lleva signo."""
    lat, lon = tags.get("Composite:GPSLatitude"), tags.get("Composite:GPSLongitude")
    if lat is None or lon is None:
        coords = find(tags, "GPSCoordinates", ("Keys", "QuickTime"))
        if isinstance(coords, str):
            nums = re.findall(r"[+-]?\d+(?:\.\d+)?", coords)
            if len(nums) >= 2:
                lat, lon = nums[0], nums[1]
    if lat is None or lon is None:
        return None, None, None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None, "Coordenadas ilegibles"
    if not _valid_coord(lat, lon):
        return None, None, "Coordenadas inválidas o (0,0): descartadas"
    return lat, lon, None


def interpret(tags: dict, kind: FileKind, *, mtime_ns: int | None = None, use_mtime_fallback: bool = False,
              now: datetime | None = None) -> MediaMetadata:
    picked = _pick_date(tags, kind, now)
    note = None
    if picked and picked[0] != "BAD":
        dt, source, tz, note = picked
    else:
        note = picked[1] if picked else None
        dt, source, tz = None, DateSource.UNKNOWN, None
        if use_mtime_fallback and mtime_ns:
            local = datetime.fromtimestamp(mtime_ns / 1e9)
            dt, source = local.strftime("%Y-%m-%dT%H:%M:%S"), DateSource.FILE_MTIME
            note = (note + "; " if note else "") + "Fecha de modificación del archivo (baja confianza)"
        else:
            note = (note + "; " if note else "") + "Sin fecha de captura confiable"
    lat, lon, gnote = _pick_gps(tags)
    cid = find(tags, "ContentIdentifier")
    return MediaMetadata(
        capture_dt=dt, date_source=source, tz_info=tz, date_note=note,
        gps_lat=lat, gps_lon=lon, gps_note=gnote,
        content_identifier=str(cid).lower() if cid else None,
        make=find(tags, "Make"), model=find(tags, "Model"),
    )
