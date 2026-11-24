"""Pruebas del adaptador AFC con un AFC falso (sin dispositivo): lógica de enumeración,
traducción de errores, restricción a /DCIM y garantía de operaciones de solo lectura."""
import asyncio
import hashlib
import inspect
import posixpath
from datetime import datetime

import pytest
from pymobiledevice3 import exceptions as E
from pymobiledevice3.services.afc import AfcError

from iphonephotosync.device.afc_source import AfcDeviceSource, dt_to_ns, map_exception
from iphonephotosync.domain.models import DeviceInfo
from iphonephotosync.ports.device import (
    FORBIDDEN_DEVICE_METHODS, DeviceDisconnected, DeviceError, DeviceLocked, DeviceNotFound,
    DeviceReadError, TrustRequired, iter_chunks,
)

ALLOWED_AFC_CALLS = {"listdir", "stat", "fopen", "pread", "fclose", "get_file_hash"}
MTIME = datetime(2024, 5, 1, 12, 30, 15, 123456)


class FakeAfc:
    def __init__(self, tree: dict[str, bytes]):
        self.tree = tree
        self.calls: list[str] = []
        self.bad_stat: set[str] = set()
        self.read_error: BaseException | None = None
        self.read_delay = 0.0
        self.hash_error: BaseException | None = None
        self._handles: dict[int, str] = {}

    def __getattr__(self, name):  # cualquier operación fuera de la lista permitida es un fallo de la prueba
        raise AssertionError(f"Operación AFC no permitida: {name}")

    def _dirs(self):
        d = {"/DCIM"}
        for p in self.tree:
            while (p := posixpath.dirname(p)) not in ("/", ""):
                d.add(p)
        return d

    async def listdir(self, path):
        self.calls.append("listdir")
        names = {p[len(path) + 1:].split("/")[0] for p in list(self.tree) + list(self._dirs())
                 if p.startswith(path + "/")}
        return [".", ".."][2:] + sorted(names)

    async def stat(self, path):
        self.calls.append("stat")
        if path in self.bad_stat:
            raise E.AfcException("falla stat", AfcError.READ_ERROR, path)
        if path in self._dirs():
            return {"st_ifmt": "S_IFDIR", "st_size": 64, "st_mtime": MTIME, "st_birthtime": MTIME}
        if path in self.tree:
            return {"st_ifmt": "S_IFREG", "st_size": len(self.tree[path]), "st_mtime": MTIME,
                    "st_birthtime": MTIME}
        raise E.AfcFileNotFoundError(f"No such file on the device: {path}", AfcError.READ_ERROR, path)

    async def fopen(self, path, mode="r"):
        self.calls.append("fopen")
        assert mode == "r"
        h = len(self._handles) + 1
        self._handles[h] = path
        return h

    async def pread(self, handle, size, offset):
        self.calls.append("pread")
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        if self.read_error:
            raise self.read_error
        return self.tree[self._handles[handle]][offset:offset + size]

    async def fclose(self, handle):
        self.calls.append("fclose")

    async def get_file_hash(self, path):
        self.calls.append("get_file_hash")
        if self.hash_error:
            raise self.hash_error
        if path not in self.tree:
            raise E.AfcFileNotFoundError("No such file on the device", AfcError.READ_ERROR, path)
        return hashlib.sha1(self.tree[path]).digest()


TREE = {
    "/DCIM/100APPLE/IMG_0001.HEIC": b"h" * 10_000,
    "/DCIM/100APPLE/IMG_0001.MOV": b"m" * 5_000,
    "/DCIM/101APPLE/IMG_0002.JPG": b"j" * 777,
    "/DCIM/ispRegDump.bin": b"\x00" * 64,
}


@pytest.fixture
def make_source():
    created = []

    def _make(afc=None, **kw):
        afc = afc or FakeAfc(dict(TREE))
        src = AfcDeviceSource.for_testing(afc, DeviceInfo("UDID-X", "iPhoneTest", "99.0"), **kw)
        created.append(src)
        return src, afc

    yield _make
    for s in created:
        s.close()


def test_adapter_exposes_no_write_operations():
    public = {n for n, _ in inspect.getmembers(AfcDeviceSource, callable) if not n.startswith("_")}
    assert not (public & FORBIDDEN_DEVICE_METHODS)


def test_enumerates_dcim_including_top_level_files_sorted(make_source):
    src, afc = make_source()
    files = list(src.list_files())
    assert [f.path for f in files] == sorted(TREE)
    assert all(f.mtime_ns == dt_to_ns(MTIME) for f in files)
    assert files[0].size == 10_000 and src.listing_errors() == []
    assert set(afc.calls) <= ALLOWED_AFC_CALLS


def test_unreadable_entry_is_reported_not_fatal(make_source):
    afc = FakeAfc(dict(TREE))
    afc.bad_stat.add("/DCIM/101APPLE/IMG_0002.JPG")
    src, _ = make_source(afc)
    paths = [f.path for f in src.list_files()]
    assert "/DCIM/101APPLE/IMG_0002.JPG" not in paths and len(paths) == 3
    assert src.listing_errors()[0][0] == "/DCIM/101APPLE/IMG_0002.JPG"


def test_read_full_file_in_chunks(make_source):
    src, afc = make_source()
    path = "/DCIM/100APPLE/IMG_0001.HEIC"
    with src.open_read(path) as h:
        assert h.size == 10_000
        data = b"".join(iter_chunks(h, 3000))
    assert data == TREE[path]
    assert set(afc.calls) <= ALLOWED_AFC_CALLS and afc.calls.count("fclose") == 1


@pytest.mark.parametrize("bad", ["/PhotoData/Photos.sqlite", "/DCIM/../PhotoData/x", "/etc/passwd", "DCIM/x", "/DCIM"])
def test_paths_outside_dcim_never_reach_the_device(make_source, bad):
    src, afc = make_source()
    with pytest.raises(DeviceReadError):
        src.open_read(bad)
    with pytest.raises(DeviceReadError):
        src.source_sha1(bad)
    assert afc.calls == []


def test_connection_loss_during_read_maps_to_disconnected(make_source):
    afc = FakeAfc(dict(TREE))
    src, _ = make_source(afc)
    with src.open_read("/DCIM/100APPLE/IMG_0001.MOV") as h:
        afc.read_error = E.ConnectionTerminatedError()
        with pytest.raises(DeviceDisconnected):
            h.read_at(0, 100)


def test_unresponsive_device_times_out_as_disconnected(make_source):
    afc = FakeAfc(dict(TREE))
    src, _ = make_source(afc, op_timeout=0.3)
    afc.read_delay = 5
    with src.open_read("/DCIM/100APPLE/IMG_0001.MOV") as h:
        with pytest.raises(DeviceDisconnected):
            h.read_at(0, 100)


def test_source_sha1_ok_unavailable_and_missing(make_source):
    afc = FakeAfc(dict(TREE))
    src, _ = make_source(afc)
    p = "/DCIM/100APPLE/IMG_0001.MOV"
    assert src.source_sha1(p) == hashlib.sha1(TREE[p]).digest()
    afc.hash_error = E.AfcException("no soportado", AfcError.OP_NOT_SUPPORTED if hasattr(AfcError, "OP_NOT_SUPPORTED") else AfcError.READ_ERROR, p)
    assert src.source_sha1(p) is None                      # sin hash de origen: se informa, no se inventa
    afc.hash_error = None
    with pytest.raises(DeviceReadError):
        src.source_sha1("/DCIM/100APPLE/NOPE.MOV")


@pytest.mark.parametrize("exc,expected", [
    (E.PasswordRequiredError(), DeviceLocked),
    (E.PasscodeRequiredError("bloqueado", "id", "99.0"), DeviceLocked),
    (E.UserDeniedPairingError(), TrustRequired),
    (E.PairingDialogResponsePendingError(), TrustRequired),
    (E.NotPairedError(), TrustRequired),
    (E.NoDeviceConnectedError(), DeviceNotFound),
    (E.ConnectionTerminatedError(), DeviceDisconnected),
    (ConnectionResetError(), DeviceDisconnected),
    (asyncio.IncompleteReadError(b"", 10), DeviceDisconnected),
    (E.AfcException("x", AfcError.READ_ERROR, "/DCIM/a"), DeviceReadError),
    (E.AfcFileNotFoundError("x", AfcError.READ_ERROR, "/DCIM/a"), DeviceReadError),
    (ValueError("raro"), DeviceError),
])
def test_exception_mapping(exc, expected):
    assert type(map_exception(exc)) is expected


def test_closed_connection_raises_disconnected(make_source):
    src, _ = make_source()
    src.close()
    with pytest.raises(DeviceDisconnected):
        list(src.list_files())
