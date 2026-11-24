from datetime import datetime, timezone

import pytest

from iphone_backup.domain.filetypes import FileKind
from iphone_backup.domain.models import DateSource
from iphone_backup.metadata.live import confirm_pair
from iphone_backup.metadata.policy import interpret, parse_exif_datetime

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
CID = "ABCDEF01-2345-6789-ABCD-EF0123456789"


def photo(tags, **kw):
    return interpret(tags, FileKind.PHOTO, now=NOW, **kw)


def video(tags, **kw):
    return interpret(tags, FileKind.VIDEO, now=NOW, **kw)


def test_photo_exif_with_offset_and_gps():
    m = photo({"ExifIFD:DateTimeOriginal": "2024:05:01 10:20:30", "ExifIFD:OffsetTimeOriginal": "-06:00",
               "Composite:GPSLatitude": 41.3851, "Composite:GPSLongitude": -2.1734, "IFD0:Model": "iPhone 16 Pro Max"})
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2024-05-01T10:20:30", DateSource.EXIF, "-06:00")
    assert (m.gps_lat, m.gps_lon) == (41.3851, -2.1734) and m.has_gps and m.model == "iPhone 16 Pro Max"


def test_photo_falls_back_to_exif_createdate():
    m = photo({"ExifIFD:CreateDate": "2023:12:31 23:59:59", "ExifIFD:OffsetTimeDigitized": "+01:00"})
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2023-12-31T23:59:59", DateSource.EXIF, "+01:00")


def test_photo_without_metadata_is_unknown_and_not_invented():
    m = photo({})
    assert m.capture_dt is None and m.date_source is DateSource.UNKNOWN and not m.has_gps
    assert "Sin fecha" in m.date_note


def test_mtime_fallback_is_opt_in_and_labelled():
    mtime = int(datetime(2022, 3, 4, 5, 6, 7).timestamp() * 1e9)
    assert photo({}, mtime_ns=mtime).date_source is DateSource.UNKNOWN          # por defecto NO se usa
    m = photo({}, mtime_ns=mtime, use_mtime_fallback=True)
    assert m.date_source is DateSource.FILE_MTIME and m.capture_dt == "2022-03-04T05:06:07"
    assert "baja confianza" in m.date_note


@pytest.mark.parametrize("bad", ["0000:00:00 00:00:00", "1904:01:01 00:00:00", "2099:01:01 00:00:00",
                                 "2024:13:45 10:00:00", "basura", None, 12345])
def test_invalid_dates_rejected(bad):
    assert parse_exif_datetime(bad, NOW) is None


def test_invalid_exif_date_gives_unknown_with_reason():
    m = photo({"ExifIFD:DateTimeOriginal": "0000:00:00 00:00:00"})
    assert m.date_source is DateSource.UNKNOWN and "inválida" in m.date_note


def test_video_keys_creationdate_keeps_local_day_without_conversion():
    # 23:30 en UTC-6 es 05:30 UTC del día siguiente: NO se convierte.
    m = video({"Keys:CreationDate": "2024:05:01 23:30:00-06:00", "QuickTime:CreateDate": "2024:05:02 05:30:00"})
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2024-05-01T23:30:00", DateSource.QUICKTIME_KEYS, "-06:00")


def test_video_utc_only_is_flagged():
    m = video({"QuickTime:CreateDate": "2024:05:02 05:30:00"})
    assert (m.capture_dt, m.date_source, m.tz_info) == ("2024-05-02T05:30:00", DateSource.QUICKTIME_UTC, "UTC")
    assert "UTC" in m.date_note


def test_video_zero_createdate_is_unknown():
    assert video({"QuickTime:CreateDate": "0000:00:00 00:00:00"}).date_source is DateSource.UNKNOWN


def test_photo_ignores_video_tags_and_vice_versa():
    assert photo({"QuickTime:CreateDate": "2024:05:02 05:30:00"}).date_source is DateSource.UNKNOWN
    assert video({"ExifIFD:DateTimeOriginal": "2024:05:02 05:30:00"}).date_source is DateSource.UNKNOWN


def test_z_suffix_and_compact_offset():
    assert parse_exif_datetime("2024:05:01 10:00:00Z", NOW) == ("2024-05-01T10:00:00", "UTC")
    assert parse_exif_datetime("2024:05:01 10:00:00+0530", NOW) == ("2024-05-01T10:00:00", "+05:30")


def test_gps_from_keys_string_when_no_composite():
    m = video({"Keys:GPSCoordinates": "41.3851 -2.1734 12.5"})
    assert (m.gps_lat, m.gps_lon) == (41.3851, -2.1734)
    m = video({"Keys:GPSCoordinates": "+41.3851-002.1734+012.500/"})          # forma ISO 6709
    assert (m.gps_lat, m.gps_lon) == (41.3851, -2.1734)


def test_raw_unsigned_gps_group_is_never_used():
    # GPS:GPSLongitude con -n viene SIN signo: usarlo ubicaría mal el archivo
    m = photo({"GPS:GPSLatitude": 41.38, "GPS:GPSLongitude": 2.17})
    assert not m.has_gps


@pytest.mark.parametrize("lat,lon", [(0, 0), (91, 10), (10, 181), ("x", 1)])
def test_bad_coordinates_discarded(lat, lon):
    m = photo({"Composite:GPSLatitude": lat, "Composite:GPSLongitude": lon})
    assert not m.has_gps and m.gps_note


def test_content_identifier_normalized_from_either_group():
    assert photo({"Apple:ContentIdentifier": CID}).content_identifier == CID.lower()
    assert video({"Keys:ContentIdentifier": CID}).content_identifier == CID.lower()


def test_live_pair_confirmation():
    assert confirm_pair(CID.lower(), CID).confirmed and confirm_pair(CID, CID).pair_id == CID.lower()
    for a, b in [(CID, "other"), (CID, None), (None, CID), (None, None)]:
        v = confirm_pair(a, b)
        assert not v.confirmed and v.pair_id is None and v.reason
