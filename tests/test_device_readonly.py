"""Garantías de solo lectura y comportamiento del simulador."""
import hashlib
import inspect
from pathlib import Path

import pytest

from iphone_backup.device.simulated import FaultPlan, SimulatedDeviceSource
from iphone_backup.ports.device import (
    FORBIDDEN_DEVICE_METHODS, DeviceDisconnected, DeviceLocked, DeviceReadError, DeviceSource,
    ReadHandle, iter_chunks,
)


def public_methods(cls):
    return {n for n, _ in inspect.getmembers(cls, callable) if not n.startswith("_")}


def test_port_has_no_write_operations():
    assert not (public_methods(DeviceSource) & FORBIDDEN_DEVICE_METHODS)
    assert not (public_methods(ReadHandle) & FORBIDDEN_DEVICE_METHODS)


def test_simulated_adapter_has_no_write_operations():
    assert not (public_methods(SimulatedDeviceSource) & FORBIDDEN_DEVICE_METHODS)


def snapshot(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_full_read_does_not_modify_device(fake_iphone):
    before = snapshot(fake_iphone)
    dev = SimulatedDeviceSource(fake_iphone)
    for f in dev.list_files():
        with dev.open_read(f.path) as h:
            b"".join(iter_chunks(h, 1024))
        dev.source_sha1(f.path)
    assert snapshot(fake_iphone) == before


def test_list_files_is_deterministic_and_complete(fake_iphone):
    dev = SimulatedDeviceSource(fake_iphone)
    paths = [f.path for f in dev.list_files()]
    assert paths == sorted(paths)
    assert "/DCIM/100APPLE/IMG_0002.JPG" in paths and "/DCIM/101APPLE/IMG_0002.JPG" in paths
    assert "/DCIM/ispRegDump.bin" in paths and len(paths) == 6


def test_read_and_sha1_match(fake_iphone):
    dev = SimulatedDeviceSource(fake_iphone)
    path = "/DCIM/100APPLE/IMG_0001.HEIC"
    with dev.open_read(path) as h:
        data = b"".join(iter_chunks(h, 777))
    assert data == (fake_iphone / "DCIM/100APPLE/IMG_0001.HEIC").read_bytes()
    assert dev.source_sha1(path) == hashlib.sha1(data).digest()


@pytest.mark.parametrize("bad", ["/DCIM/../secret", "/etc/passwd", "/DCIM/100APPLE/NOPE.JPG"])
def test_rejects_bad_paths(fake_iphone, bad):
    with pytest.raises(DeviceReadError):
        SimulatedDeviceSource(fake_iphone).open_read(bad)


def test_disconnect_mid_read_and_reconnect(fake_iphone):
    dev = SimulatedDeviceSource(fake_iphone, faults=FaultPlan(disconnect_after_bytes=3000))
    path = "/DCIM/100APPLE/IMG_0001.MOV"
    with pytest.raises(DeviceDisconnected):
        with dev.open_read(path) as h:
            b"".join(iter_chunks(h, 1000))
    with pytest.raises(DeviceDisconnected):   # sigue caído hasta reconectar
        dev.info()
    dev.reconnect()
    with dev.open_read(path) as h:
        assert len(b"".join(iter_chunks(h, 1000))) == h.size


def test_locked_device(fake_iphone):
    dev = SimulatedDeviceSource(fake_iphone, faults=FaultPlan(locked=True))
    with pytest.raises(DeviceLocked):
        list(dev.list_files())
    dev.unlock()
    assert len(list(dev.list_files())) == 6


def test_unreadable_file_only_affects_that_file(fake_iphone):
    bad = "/DCIM/100APPLE/IMG_0002.JPG"
    dev = SimulatedDeviceSource(fake_iphone, faults=FaultPlan(unreadable_paths={bad}))
    with pytest.raises(DeviceReadError):
        dev.open_read(bad)
    with dev.open_read("/DCIM/101APPLE/IMG_0002.JPG") as h:
        assert h.size > 0
