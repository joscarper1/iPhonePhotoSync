import pytest

from media_fixtures import needs_exiftool
from test_engine import Env
from iphonephotosync.cli.__main__ import main
from iphonephotosync.database.manifest import Manifest
from iphonephotosync.metadata.exiftool_service import ExifToolService
from iphonephotosync.transfer.verify import verify_library

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


@pytest.fixture
def env(tmp_path, et):
    e = Env(tmp_path, et)
    e.run()
    return e


def test_clean_backup_verifies_against_disk_and_device(env):
    with Manifest(env.dest.manifest_path) as mf:
        rep = verify_library(env.dest, mf, env.src)
    assert rep.clean and rep.ok == rep.checked == 5 and rep.device_checked == 5 and not rep.device_changed


def test_detects_missing_resized_and_corrupted_files(env):
    files = [env.dest.library / f for f in env.library_files()]
    files[0].unlink()                                                    # falta
    files[1].write_bytes(files[1].read_bytes() + b"x")                   # tamaño distinto
    data = bytearray(files[2].read_bytes()); data[10] ^= 0xFF
    files[2].write_bytes(bytes(data))                                    # mismo tamaño, contenido alterado (bit rot)
    with Manifest(env.dest.manifest_path) as mf:
        rep = verify_library(env.dest, mf)
    assert not rep.clean and len(rep.missing) == 1 and len(rep.size_mismatch) == 1 and len(rep.hash_mismatch) == 1
    assert rep.ok == 2


def test_device_change_is_reported_as_information(env):
    p = env.iphone / "DCIM/100APPLE/IMG_0003.PNG"
    p.write_bytes(p.read_bytes() + b"cambio")
    with Manifest(env.dest.manifest_path) as mf:
        rep = verify_library(env.dest, mf, env.src)
    assert rep.clean and len(rep.device_changed) == 1                    # la copia sigue íntegra; el iPhone cambió


def test_cli_verify_exit_codes(env, capsys):
    assert main(["verify", "--dest", str(env.dest.base), "--simulate", str(env.iphone)]) == 0
    assert "Verificados: 5/5" in capsys.readouterr().out
    next(f for f in (env.dest.library).rglob("*.JPG") if "_" not in f.parent.name[:1]).unlink()
    assert main(["verify", "--dest", str(env.dest.base)]) == 1
