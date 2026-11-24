import pytest

from iphonephotosync.domain.filetypes import FileKind, classify
from iphonephotosync.domain.models import (
    DeviceFile, InvalidTransition, ItemStatus, can_transition, check_transition,
)


def test_happy_path_transitions():
    chain = [ItemStatus.DISCOVERED, ItemStatus.STAGED, ItemStatus.VERIFIED, ItemStatus.COMMITTED]
    for a, b in zip(chain, chain[1:]):
        assert can_transition(a, b)


@pytest.mark.parametrize("old,new", [
    (ItemStatus.DISCOVERED, ItemStatus.COMMITTED),   # no se salta staging ni verificación
    (ItemStatus.DISCOVERED, ItemStatus.VERIFIED),
    (ItemStatus.STAGED, ItemStatus.COMMITTED),
    (ItemStatus.COMMITTED, ItemStatus.DISCOVERED),   # lo ya respaldado no se revierte
    (ItemStatus.COMMITTED, ItemStatus.FAILED),
    (ItemStatus.SKIPPED_DUP, ItemStatus.STAGED),
])
def test_forbidden_transitions(old, new):
    assert not can_transition(old, new)
    with pytest.raises(InvalidTransition):
        check_transition(old, new)


def test_failed_can_retry():
    assert can_transition(ItemStatus.FAILED, ItemStatus.DISCOVERED)


def test_device_file_properties():
    f = DeviceFile("/DCIM/100APPLE/IMG_0001.HEIC", 10, 5)
    assert (f.name, f.ext, f.folder) == ("IMG_0001.HEIC", ".heic", "/DCIM/100APPLE")
    assert f.fingerprint == "10:5"


@pytest.mark.parametrize("ext,kind", [
    (".heic", FileKind.PHOTO), (".JPEG", FileKind.PHOTO), (".png", FileKind.PHOTO),
    (".webp", FileKind.PHOTO), (".mov", FileKind.VIDEO), (".mp4", FileKind.VIDEO),
    (".aae", FileKind.SIDECAR), (".bin", FileKind.IGNORED), ("", FileKind.IGNORED),
])
def test_classify(ext, kind):
    assert classify(ext)[0] is kind
