"""Motor de transferencia: copia, verificación, incrementalidad y tolerancia a fallos (iPhone simulado,
medios sintéticos y ExifTool real; nunca datos del usuario)."""
import errno
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from media_fixtures import make_jpeg, make_mov, make_png, needs_exiftool
from iphone_backup.app.planning import build_plan
from iphone_backup.database.manifest import Manifest
from iphone_backup.device.simulated import FaultPlan, SimulatedDeviceSource
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.metadata.extractor import exiftool_extractor
from iphone_backup.organizer.planner import OrganizerSettings, plan_folders
from iphone_backup.transfer import space
from iphone_backup.transfer.destination import Destination, DestinationMismatch, find_backup_by_id
from iphone_backup.transfer.engine import BackupEngine, EngineConfig, EngineControl, SessionOutcome
from iphone_backup.transfer.fsops import RealFs

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
CFG = EngineConfig(reserve_bytes=0, workers=1)       # determinista: los contadores de fallos inyectados son exactos
LIB_RESERVED = {"_Manifest", "_Logs", "_Quarantine", "_Staging"}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Env:
    def __init__(self, tmp_path: Path, et):
        self.tmp, self.et = tmp_path, et
        self.iphone = tmp_path / "iphone"
        self.dest = Destination(tmp_path / "hdd")
        d = self.iphone / "DCIM"
        make_jpeg(d / "100APPLE/IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00"})
        make_jpeg(d / "100APPLE/IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2023:12:31 23:59:59"})
        make_png(d / "100APPLE/IMG_0003.PNG")
        make_mov(d / "100APPLE/IMG_0004.MOV", {"Keys:CreationDate": "2025:01:02 03:04:05+01:00"},
                 mdat_before_moov=3_000_000)
        make_jpeg(d / "101APPLE/IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2023:12:15 08:00:00"})  # mismo nombre
        self.src = SimulatedDeviceSource(self.iphone)

    def snapshot(self):
        return {str(p.relative_to(self.iphone)): (p.stat().st_size, p.stat().st_mtime_ns, sha(p))
                for p in sorted(self.iphone.rglob("*")) if p.is_file()}

    def plan(self, src=None):
        return build_plan(src or self.src, self.et, None, OrganizerSettings())

    def engine(self, plan=None, src=None, fs=None, control=None, on_event=None, manifest=None, config=CFG, **extra):
        src = src or self.src
        mf = manifest or Manifest(self.dest.manifest_path)
        self._mf = mf
        return BackupEngine(src, mf, self.dest, plan or self.plan(src), extractor=exiftool_extractor(self.et),
                            config=config, fs=fs, control=control, on_event=on_event, **extra)

    def run(self, **kw):
        eng = self.engine(**kw)
        try:
            return eng.run()
        finally:
            self._mf.close()

    def library_files(self):
        lib = self.dest.library
        out = []
        for p in sorted(lib.rglob("*")):
            rel = p.relative_to(lib)
            if p.is_file() and rel.parts[0] not in LIB_RESERVED:
                out.append(rel.as_posix())
        return out

    def leftovers(self):
        return [p.name for d in (self.dest.staging_dir,) for p in d.glob("*")] if self.dest.staging_dir.exists() else []

    def statuses(self):
        with Manifest(self.dest.manifest_path) as mf:
            return mf.counts_by_status()


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


@pytest.fixture
def env(tmp_path, et):
    return Env(tmp_path, et)


def test_happy_path_copies_verifies_and_organizes(env):
    before = env.snapshot()
    res = env.run()
    assert res.outcome is SessionOutcome.COMPLETED and res.copied == 5 and res.failed == 0
    assert res.verified_full == 5 and res.verified_dest_only == 0
    files = env.library_files()
    assert "2024/03 - Marzo/Sin ubicación/IMG_0001.JPG" in files
    assert "2025/01 - Enero/Sin ubicación/IMG_0004.MOV" in files
    assert "Fecha desconocida/Sin ubicación/IMG_0003.PNG" in files
    assert "2023/12 - Diciembre/Sin ubicación/IMG_0002.JPG" in files          # el de 100APPLE (primero) conserva el nombre
    suffixed = [f for f in files if "IMG_0002__" in f]
    assert len(suffixed) == 1 and suffixed[0].startswith("2023/12 - Diciembre/Sin ubicación/")   # el otro, con sufijo
    for rel in ("2024/03 - Marzo/Sin ubicación/IMG_0001.JPG", "2025/01 - Enero/Sin ubicación/IMG_0004.MOV"):
        orig = next(env.iphone.rglob(Path(rel).name))
        assert sha(env.dest.library / rel) == sha(orig)                          # bytes idénticos, sin recomprimir
        assert (env.dest.library / rel).stat().st_mtime_ns // 10**9 == orig.stat().st_mtime_ns // 10**9
    assert env.leftovers() == []                                                  # sin .part en staging
    assert env.statuses() == {"committed": 5}
    assert env.snapshot() == before                                               # el iPhone no cambió
    assert len(files) == 5                                                        # nada se perdió ni se duplicó


def test_everything_written_stays_inside_destination(env):
    before = {p for p in env.tmp.rglob("*")}
    env.run()
    new = {p for p in env.tmp.rglob("*")} - before
    assert new and all(env.dest.base in p.parents or p == env.dest.base for p in new)


def test_manifest_records_metadata_and_hashes(env):
    env.run()
    with Manifest(env.dest.manifest_path) as mf:
        row = mf._conn.execute("SELECT * FROM items WHERE original_name='IMG_0001.JPG'").fetchone()
    assert row["copy_status"] == "committed" and row["verify_status"] == "verified_full"
    assert row["sha256"] == sha(env.iphone / "DCIM/100APPLE/IMG_0001.JPG") and len(row["sha1_source"]) == 40
    assert row["capture_dt"] == "2024-03-10T12:00:00" and row["date_source"] == "exif"
    assert row["rel_path"] == "2024/03 - Marzo/Sin ubicación/IMG_0001.JPG" and row["imported_at"]


def test_second_run_does_not_read_or_copy_anything(env):
    env.run()
    plan = env.plan()
    before_bytes = env.src.bytes_read
    res = env.run(plan=plan)
    assert res.copied == 0 and res.skipped_incremental == 5 and res.bytes_copied == 0
    assert env.src.bytes_read == before_bytes                                     # ni un byte leído del iPhone
    assert len(env.library_files()) == 5


def test_new_file_is_imported_incrementally(env):
    env.run()
    make_jpeg(env.iphone / "DCIM/101APPLE/IMG_0099.JPG", {"ExifIFD:DateTimeOriginal": "2026:02:02 10:00:00"})
    res = env.run()
    assert res.copied == 1 and res.skipped_incremental == 5
    assert "2026/02 - Febrero/Sin ubicación/IMG_0099.JPG" in env.library_files()


def test_modified_content_same_name_keeps_both_versions(env):
    env.run()
    p = env.iphone / "DCIM/100APPLE/IMG_0001.JPG"
    make_jpeg(p, {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00", "IFD0:Make": "Cambiado"})
    os.utime(p, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    res = env.run()
    assert res.copied == 1
    versions = [f for f in env.library_files() if "IMG_0001" in f]
    assert len(versions) == 2                                                      # ninguna se sobrescribe


def test_disconnect_mid_transfer_is_safe_and_resumable(env):
    plan = env.plan()
    env.src.faults.disconnect_after_bytes = env.src.bytes_read + 500_000          # a mitad del MOV de 3 MB
    res = env.run(plan=plan)
    assert res.outcome is SessionOutcome.DEVICE_LOST and res.copied == 3
    assert not any(f.endswith(".MOV") for f in env.library_files())               # sin archivo parcial con apariencia final
    assert env.leftovers() == []
    assert env.statuses()["committed"] == 3 and "failed" not in env.statuses()
    env.src.reconnect()
    res2 = env.run()
    assert res2.outcome is SessionOutcome.COMPLETED and res2.copied == 2 and res2.skipped_incremental == 3
    assert len(env.library_files()) == 5 and not [f for f in env.library_files() if "__" in f.split("/")[-1] and "IMG_0002__" not in f]


def test_unreadable_file_fails_alone_and_is_retried_next_time(env):
    bad = "/DCIM/100APPLE/IMG_0002.JPG"
    env.src.faults.unreadable_paths.add(bad)
    res = env.run()
    assert res.copied == 4 and res.failed == 1 and res.errors[0][0] == bad
    assert env.statuses() == {"committed": 4, "failed": 1}
    env.src.faults.unreadable_paths.clear()
    res2 = env.run()
    assert res2.copied == 1 and res2.failed == 0 and env.statuses() == {"committed": 5}


def test_transient_transport_corruption_is_detected_and_retried(env):
    plan = env.plan()
    env.src.faults.corrupt_paths.add("/DCIM/100APPLE/IMG_0001.JPG")
    env.src.faults.corrupt_times = 1
    res = env.run(plan=plan)
    assert res.copied == 5 and res.quarantined == 0
    assert sha(env.dest.library / "2024/03 - Marzo/Sin ubicación/IMG_0001.JPG") == sha(env.iphone / "DCIM/100APPLE/IMG_0001.JPG")


def test_persistent_transport_corruption_goes_to_quarantine_not_library(env):
    plan = env.plan()
    env.src.faults.corrupt_paths.add("/DCIM/100APPLE/IMG_0001.JPG")
    env.src.faults.corrupt_times = 99
    res = env.run(plan=plan)
    assert res.quarantined == 1 and res.copied == 4
    assert not any("IMG_0001" in f for f in env.library_files())                  # la copia dudosa NO llega al destino final
    assert len(list(env.dest.quarantine_dir.glob("*.quarantine"))) == 1           # se conserva para revisión
    assert env.statuses() == {"committed": 4, "quarantined": 1}
    env.src.faults.corrupt_times = 0
    res2 = env.run()
    assert res2.copied == 0 and res2.pending_quarantine == 1                      # requiere decisión del usuario


class _Flip:
    def __init__(self, f):
        self._f, self._first = f, True

    def write(self, data):
        if self._first and data:
            self._first = False
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return self._f.write(data)

    def __getattr__(self, n):
        return getattr(self._f, n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


class FlipFs(RealFs):
    """Corrompe un byte al escribir en disco (disco defectuoso)."""

    def __init__(self, times):
        self.times = times

    def open_write(self, path):
        f = super().open_write(path)
        if self.times > 0 and path.name.endswith(".part"):
            self.times -= 1
            return _Flip(f)
        return f


class _Full:
    def __init__(self, f, limit):
        self._f, self._left = f, limit

    def write(self, data):
        if len(data) > self._left:
            raise OSError(errno.ENOSPC, "No space left on device")
        self._left -= len(data)
        return self._f.write(data)

    def __getattr__(self, n):
        return getattr(self._f, n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()


class FullFs(RealFs):
    def __init__(self, limit):
        self.left = limit

    def open_write(self, path):
        return _Full(super().open_write(path), self.left)


def test_disk_write_corruption_is_caught_by_verification_and_retried(env):
    res = env.run(fs=FlipFs(times=1))
    assert res.copied == 5 and res.failed == 0
    for rel in env.library_files():
        assert (env.dest.library / rel).is_file()
    assert sha(env.dest.library / "2024/03 - Marzo/Sin ubicación/IMG_0001.JPG") == sha(env.iphone / "DCIM/100APPLE/IMG_0001.JPG")


def test_persistent_disk_corruption_never_commits(env):
    res = env.run(fs=FlipFs(times=999))
    assert res.copied == 0 and res.failed == 5
    assert env.library_files() == [] and env.leftovers() == []
    assert env.statuses() == {"failed": 5}


def test_disk_full_stops_session_cleanly(env):
    res = env.run(fs=FullFs(limit=5_000))
    assert res.outcome is SessionOutcome.DESTINATION_ERROR and "espacio" in res.message
    assert env.leftovers() == []
    assert not any(f.endswith(".MOV") for f in env.library_files())
    assert env.statuses().get("failed") is None
    res2 = env.run()                                                               # con espacio, se completa
    assert res2.outcome is SessionOutcome.COMPLETED and len(env.library_files()) == 5


def test_precheck_blocks_when_space_is_insufficient(env, monkeypatch):
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(space.shutil, "disk_usage", lambda p: U(10**12, 0, 1_000_000))
    plan = env.plan()
    before = env.src.bytes_read
    res = env.run(plan=plan)
    assert res.outcome is SessionOutcome.NO_SPACE and res.copied == 0 and "insuficiente" in res.message
    assert env.src.bytes_read == before and env.library_files() == []


def test_existing_file_is_never_overwritten(env):
    target = env.dest.library / "2024/03 - Marzo/Sin ubicación"
    target.mkdir(parents=True)
    (target / "IMG_0001.JPG").write_bytes(b"contenido previo del usuario")
    res = env.run()
    assert res.copied == 5
    assert (target / "IMG_0001.JPG").read_bytes() == b"contenido previo del usuario"
    assert len([f for f in env.library_files() if "IMG_0001" in f]) == 2


def test_identical_content_is_not_copied_twice(env):
    shutil.copy2(env.iphone / "DCIM/100APPLE/IMG_0001.JPG", env.iphone / "DCIM/101APPLE/IMG_0777.JPG")
    res = env.run()
    assert res.copied == 5 and res.skipped_duplicate == 1
    assert len([f for f in env.library_files() if f.endswith(("IMG_0001.JPG", "IMG_0777.JPG"))]) == 1
    assert env.statuses() == {"committed": 5, "skipped_dup": 1}


def test_live_pair_keeps_folder_and_suffix_together(env):
    make_jpeg(env.iphone / "DCIM/100APPLE/IMG_0100.JPG", {"ExifIFD:DateTimeOriginal": "2022:06:01 10:00:00"})
    make_mov(env.iphone / "DCIM/100APPLE/IMG_0100.MOV", {"Keys:CreationDate": "2022:06:01 10:00:01-06:00"})
    plan = env.plan()
    still, motion = "/DCIM/100APPLE/IMG_0100.JPG", "/DCIM/100APPLE/IMG_0100.MOV"
    pid = "abcdef0123456789abcdef0123456789"
    plan.confirmed_pairs = {(still, motion)}
    plan.pair_ids = {still: pid, motion: pid}
    plan.plans = plan_folders(plan.scan, plan.metas, OrganizerSettings(), None, plan.confirmed_pairs)
    clash = env.dest.library / "2022/06 - Junio/Sin ubicación"
    clash.mkdir(parents=True)
    (clash / "IMG_0100.JPG").write_bytes(b"ya existe")                              # fuerza la colisión del grupo
    res = env.run(plan=plan)
    assert res.copied == 7                                                         # 5 base + foto y MOV del Live Photo
    names = sorted(f.split("/")[-1] for f in env.library_files() if "IMG_0100" in f)
    assert names == ["IMG_0100.JPG", "IMG_0100__abcdef01.JPG", "IMG_0100__abcdef01.MOV"]   # foto y MOV, mismo sufijo
    with Manifest(env.dest.manifest_path) as mf:
        rows = mf._conn.execute("SELECT pair_id, pair_role FROM items WHERE original_name LIKE 'IMG_0100%'").fetchall()
    assert {(r["pair_id"], r["pair_role"]) for r in rows} == {(pid, "still"), (pid, "motion")}


def test_cancel_keeps_what_was_copied_and_leaves_no_partials(env):
    control = EngineControl()
    done = []

    def on_event(kind, **k):
        if kind == "file_done":
            done.append(k["path"])
            if len(done) == 2:
                control.cancel()

    res = env.run(control=control, on_event=on_event)
    assert res.outcome is SessionOutcome.CANCELLED and 2 <= res.copied < 5
    assert env.leftovers() == [] and len(env.library_files()) == res.copied
    res2 = env.run()
    assert res2.outcome is SessionOutcome.COMPLETED and len(env.library_files()) == 5


def test_crash_between_rename_and_commit_is_reconciled_without_duplicates(env, monkeypatch):
    eng = env.engine()
    real = eng.manifest.transition
    state = {"crashed": False}

    def flaky(item_id, new_status, **fields):
        if new_status.value == "committed" and not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("caída simulada tras renombrar")
        return real(item_id, new_status, **fields)

    monkeypatch.setattr(eng.manifest, "transition", flaky)
    with pytest.raises(RuntimeError):
        eng.run()
    env._mf.close()
    assert len(env.library_files()) >= 1                                           # el archivo ya estaba renombrado
    res = env.run()
    assert res.outcome is SessionOutcome.COMPLETED
    assert len(env.library_files()) == 5
    assert not [f for f in env.library_files() if "IMG_0001__" in f or "IMG_0004__" in f]   # sin copias duplicadas
    assert env.statuses() == {"committed": 5}


class NoHashSource(SimulatedDeviceSource):
    def source_sha1(self, path):
        return None


def test_without_device_hash_files_are_marked_dest_only(env):
    src = NoHashSource(env.iphone)
    res = env.run(src=src)
    assert res.copied == 5 and res.verified_full == 0 and res.verified_dest_only == 5
    with Manifest(env.dest.manifest_path) as mf:
        assert {r["verify_status"] for r in mf._conn.execute("SELECT verify_status FROM items")} == {"verified_dest_only"}


def test_destination_belonging_to_another_backup_is_not_written(env):
    env.dest.prepare()
    env.dest.marker_path.write_text('{"backup_id": "otro-respaldo"}', encoding="utf-8")
    res = env.run()
    assert res.outcome is SessionOutcome.BLOCKED and res.copied == 0 and env.library_files() == []


def test_backup_is_found_by_id_after_drive_letter_change(env):
    env.run()
    with Manifest(env.dest.manifest_path) as mf:
        bid = mf.backup_id
    moved = env.tmp / "otra_letra"
    moved.mkdir()
    shutil.move(str(env.dest.base), str(moved / "hdd"))
    assert find_backup_by_id(bid, [moved]) == moved / "hdd"
    assert find_backup_by_id("no-existe", [moved]) is None
    env.dest = Destination(moved / "hdd")
    res = env.run()
    assert res.copied == 0 and res.skipped_incremental == 5                        # reconoce lo ya respaldado


def test_pause_resumes_and_completes(env):
    import threading
    control = EngineControl()
    control.pause()
    t = threading.Timer(0.4, control.resume)
    t.start()
    res = env.run(control=control)
    t.join()
    assert res.outcome is SessionOutcome.COMPLETED and res.copied == 5
