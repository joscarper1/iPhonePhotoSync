"""El pipeline concurrente (4 trabajadores, lectura‖escritura) conserva TODAS las garantías del motor."""
import threading
import time

import pytest

from media_fixtures import make_jpeg, make_mov, needs_exiftool
from test_engine import Env, FlipFs, FullFs, sha
from iphone_backup.database.manifest import Manifest
from iphone_backup.metadata.exiftool_service import ExifToolService
from iphone_backup.transfer.engine import EngineConfig, EngineControl, SessionOutcome

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
P4 = EngineConfig(reserve_bytes=0, workers=4)


@pytest.fixture(scope="module")
def et():
    with ExifToolService() as svc:
        yield svc


@pytest.fixture
def env(tmp_path, et):
    e = Env(tmp_path, et)
    d = e.iphone / "DCIM/102APPLE"
    for i in range(12):                                          # más archivos que trabajadores, todos DISTINTOS
        make_jpeg(d / f"IMG_{i:04d}.JPG", {"ExifIFD:DateTimeOriginal": f"2025:0{1 + i % 6}:{10 + i:02d} 12:00:00"})
    return e                                                     # 17 archivos en total


def lib_hashes(env):
    return {f: sha(env.dest.library / f) for f in env.library_files()}


def test_parallel_copy_is_complete_verified_and_identical_to_sequential(env, tmp_path, et):
    res = env.run(config=P4)
    assert res.outcome is SessionOutcome.COMPLETED and res.copied == 17 and res.failed == 0
    assert res.verified_full == 17 and env.leftovers() == [] and env.statuses() == {"committed": 17}
    parallel = lib_hashes(env)
    seq = Env(tmp_path / "seq", et)                              # mismo contenido, secuencial
    d = seq.iphone / "DCIM/102APPLE"
    for i in range(12):
        make_jpeg(d / f"IMG_{i:04d}.JPG", {"ExifIFD:DateTimeOriginal": f"2025:0{1 + i % 6}:{10 + i:02d} 12:00:00"})
    seq.run(config=EngineConfig(reserve_bytes=0, workers=1))
    assert sorted(parallel) == sorted(lib_hashes(seq))           # mismas rutas y nombres (orden de confirmación estable)


def test_parallel_identical_content_is_deduplicated_not_lost(env):
    """Regresión de mi propio fixture: 12 archivos con SOLO 6 contenidos distintos -> 6 copiados y 6 duplicados."""
    d = env.iphone / "DCIM/103APPLE"
    for i in range(12):
        make_jpeg(d / f"IMG_{i:04d}.JPG", {"ExifIFD:DateTimeOriginal": f"2024:0{1 + i % 6}:10 12:00:00"})
    res = env.run(config=P4)
    assert res.failed == 0 and res.copied + res.skipped_duplicate == 17 + 12     # nada se pierde en silencio
    # 12 archivos con 6 contenidos distintos; uno de ellos (2024-03-10) es idéntico a IMG_0001.JPG del fixture base
    assert res.copied == 22 and res.skipped_duplicate == 7
    assert env.statuses() == {"committed": 22, "skipped_dup": 7}


def test_parallel_name_collisions_are_deterministic(env):
    res = env.run(config=P4)
    names = [f for f in env.library_files() if "IMG_0002" in f]
    assert res.copied == 17 and len(names) == 3
    same_folder = [n for n in names if n.startswith("2023/12")]          # 100APPLE y 101APPLE chocan en 2023/12
    assert len(same_folder) == 2 and sum("__" in n for n in same_folder) == 1
    assert len({n.split("/")[-1] for n in same_folder}) == 2             # ninguno se sobrescribió
    assert next(n for n in names if n.startswith("2025/03")) == "2025/03 - Marzo/Sin ubicación/IMG_0002.JPG"


def test_parallel_second_run_reads_nothing(env):
    env.run(config=P4)
    plan = env.plan()
    before = env.src.bytes_read
    res = env.run(plan=plan, config=P4)
    assert res.copied == 0 and res.skipped_incremental == 17 and env.src.bytes_read == before


def test_parallel_disconnect_leaves_no_partials_and_resumes(env):
    plan = env.plan()
    env.src.faults.disconnect_after_bytes = env.src.bytes_read + 500_000
    res = env.run(plan=plan, config=P4)
    assert res.outcome is SessionOutcome.DEVICE_LOST
    assert not any(f.endswith(".MOV") for f in env.library_files())
    assert env.leftovers() == [] and "failed" not in env.statuses()
    committed = env.statuses().get("committed", 0)
    assert committed == len(env.library_files())                 # nada renombrado queda sin confirmar
    env.src.reconnect()
    res2 = env.run(config=P4)
    assert res2.outcome is SessionOutcome.COMPLETED and res2.copied == 17 - committed
    assert len(env.library_files()) == 17 and env.statuses() == {"committed": 17}


def test_parallel_cancel_midway_keeps_consistent_state(env):
    control, done = EngineControl(), []

    def on_event(kind, **k):
        if kind == "file_done":
            done.append(1)
            if len(done) == 3:
                control.cancel()

    res = env.run(config=P4, control=control, on_event=on_event)
    assert res.outcome is SessionOutcome.CANCELLED and 3 <= res.copied < 17
    assert env.leftovers() == []
    assert env.statuses().get("committed", 0) == len(env.library_files()) == res.copied   # lo renombrado quedó confirmado
    assert env.run(config=P4).outcome is SessionOutcome.COMPLETED and len(env.library_files()) == 17


def test_parallel_disk_full_and_corruption_are_still_handled(env):
    res = env.run(config=P4, fs=FullFs(limit=5_000))
    assert res.outcome is SessionOutcome.DESTINATION_ERROR and env.leftovers() == []
    assert env.run(config=P4).outcome is SessionOutcome.COMPLETED
    assert len(env.library_files()) == 17


def test_parallel_persistent_disk_corruption_never_commits(env):
    res = env.run(config=P4, fs=FlipFs(times=999))
    assert res.copied == 0 and res.failed == 17 and env.library_files() == [] and env.leftovers() == []


def test_one_manifest_transaction_per_group_not_five_per_file(env, monkeypatch):
    """El coste medido de cada commit en el HDD (104 ms) obliga a agrupar: nº de COMMIT <= ~2 por grupo."""
    commits = []
    eng = env.engine(config=P4)
    real = eng.manifest._conn
    class Spy:
        def __init__(self, c): self._c = c
        def execute(self, sql, *a):
            if sql.strip().upper() == "COMMIT":
                commits.append(1)
            return self._c.execute(sql, *a)
        def __getattr__(self, n): return getattr(self._c, n)
    eng.manifest._conn = Spy(real)
    res = eng.run()
    env._mf.close()
    assert res.copied == 17
    assert len(commits) <= 17 * 2 + 6, len(commits)              # antes: ~5 por archivo (~85 + inicio)
    assert env.statuses() == {"committed": 17}


def test_reader_writer_overlap_hides_slow_disk_writes(env):
    """La escritura lenta del destino no debe sumarse a la lectura del iPhone: el escritor trabaja mientras se lee."""
    from iphone_backup.transfer.fsops import RealFs

    class SlowWrite(RealFs):
        def open_write(self, path):
            f = super().open_write(path)
            class W:
                def write(s, data): time.sleep(0.02); return f.write(data)
                def __getattr__(s, n): return getattr(f, n)
                def __enter__(s): return s
                def __exit__(s, *a): f.close()
            return W()

    plan = env.plan()
    t = time.perf_counter()
    res = env.run(plan=plan, config=EngineConfig(reserve_bytes=0, workers=1, chunk_size=64 * 1024), fs=SlowWrite())
    assert res.copied == 17 and env.statuses() == {"committed": 17}
    # sanidad funcional del pipeline con bloques pequeños y escritor lento (no se cuelga ni pierde datos)
    for rel in env.library_files():
        assert (env.dest.library / rel).stat().st_size > 0
    assert time.perf_counter() - t < 60
