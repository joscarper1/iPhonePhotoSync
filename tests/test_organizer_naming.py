import os

import pytest

from iphone_backup.organizer.collisions import NameResolutionError, resolve_group_names
from iphone_backup.organizer.naming import (
    UnsafePath, month_folder, safe_join, sanitize_component, sanitize_filename, to_extended,
)


@pytest.mark.parametrize("raw,expected", [
    ("Barcelona, España", "Barcelona, España"),             # acentos y coma se conservan
    ("São Paulo, Brasil", "São Paulo, Brasil"),
    ('A<B>C:D"E/F\\G|H?I*J', "A_B_C_D_E_F_G_H_I_J"),          # caracteres inválidos en Windows
    ("Lugar. ", "Lugar"),                                      # punto/espacio final no permitidos
    ("  muchos   espacios  ", "muchos espacios"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("COM1", "_COM1"),
    ("", "_"), ("...", "_"), ("   ", "_"),
    ("a\x00b\x1fc", "a_b_c"),
])
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_component_normalizes_unicode_and_truncates():
    decomposed = "München"                              # u + diéresis combinante
    assert sanitize_component(decomposed) == "München"
    assert len(sanitize_component("x" * 500)) == 100


def test_sanitize_filename_keeps_extension_when_truncating():
    out = sanitize_filename("n" * 300 + ".HEIC")
    assert out.endswith(".HEIC") and len(out) <= 100


def test_month_folder():
    assert month_folder(1) == "01 - Enero" and month_folder(11) == "11 - Noviembre" and month_folder(12) == "12 - Diciembre"


def test_safe_join_inside_root(tmp_path):
    p = safe_join(tmp_path, ("2024", "05 - Mayo"), "IMG_1.HEIC")
    assert p == tmp_path / "2024" / "05 - Mayo" / "IMG_1.HEIC"


@pytest.mark.parametrize("bad", [("..",), ("a", ".."), ("a/b",), ("a\\b",), ("C:",), ("",), (".",)])
def test_safe_join_rejects_traversal(tmp_path, bad):
    with pytest.raises(UnsafePath):
        safe_join(tmp_path, bad, "x.jpg")


def test_safe_join_rejects_bad_filename(tmp_path):
    with pytest.raises(UnsafePath):
        safe_join(tmp_path, ("ok",), "..\\evil.jpg")


@pytest.mark.skipif(os.name != "nt", reason="Prefijo de ruta larga solo en Windows")
def test_extended_prefix_only_for_long_paths(tmp_path):
    assert not str(to_extended(tmp_path / "corta.jpg")).startswith("\\\\?\\")
    long = tmp_path / ("d" * 90) / ("e" * 90) / ("f" * 90) / "x.jpg"
    assert str(to_extended(long)).startswith("\\\\?\\")


# --- colisiones ---
def taken_in(existing):
    ex = {e.casefold() for e in existing}
    return lambda n: n.casefold() in ex


def test_free_names_are_kept():
    assert resolve_group_names(["IMG_1.HEIC", "IMG_1.MOV"], "abcdef0123", taken_in([])) == ["IMG_1.HEIC", "IMG_1.MOV"]


def test_collision_suffixes_whole_group_together():
    out = resolve_group_names(["IMG_1.HEIC", "IMG_1.MOV", "IMG_1.AAE"], "ABCDEF0123456789", taken_in(["IMG_1.HEIC"]))
    assert out == ["IMG_1__abcdef01.HEIC", "IMG_1__abcdef01.MOV", "IMG_1__abcdef01.AAE"]


def test_collision_is_case_insensitive_like_ntfs():
    assert resolve_group_names(["img_1.jpg"], "1234567890", taken_in(["IMG_1.JPG"])) == ["img_1__12345678.jpg"]


def test_collision_is_deterministic_and_depends_only_on_key():
    a = resolve_group_names(["X.JPG"], "deadbeefcafe", taken_in(["X.JPG"]))
    b = resolve_group_names(["X.JPG"], "deadbeefcafe", taken_in(["X.JPG", "otro.jpg"]))
    assert a == b == ["X__deadbeef.JPG"]


def test_second_level_collision_gets_counter():
    out = resolve_group_names(["X.JPG"], "deadbeefcafe", taken_in(["X.JPG", "X__deadbeef.JPG"]))
    assert out == ["X__deadbeef_2.JPG"]


def test_resolution_never_returns_a_taken_name():
    existing = ["A.JPG", "A__00112233.JPG"] + [f"A__00112233_{i}.JPG" for i in range(2, 50)]
    out = resolve_group_names(["A.JPG"], "00112233ff", taken_in(existing))
    assert not taken_in(existing)(out[0])


def test_exhaustion_raises():
    with pytest.raises(NameResolutionError):
        resolve_group_names(["A.JPG"], "00112233", lambda n: True, max_attempts=5)


def test_duplicate_names_inside_group_rejected():
    with pytest.raises(NameResolutionError):
        resolve_group_names(["A.JPG", "a.jpg"], "00112233", taken_in([]))
