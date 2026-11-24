import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from iphone_backup.app.status import fit_time_model, read_status
from iphone_backup.cli.__main__ import main
from iphone_backup.cli.progress import LiveBar, bar, fmt_bytes, fmt_eta, render_status
from iphone_backup.database.manifest import Manifest
from iphone_backup.domain.models import DeviceFile, ItemStatus, VerifyStatus

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_bar_and_formats():
    assert bar(0, 10) == "[----------]   0.0%" and bar(100, 10) == "[##########] 100.0%" and bar(50, 10).count("#") == 5
    assert bar(150, 4).count("#") == 4 and bar(-5, 4).count("#") == 0           # acotado
    assert fmt_bytes(2_500_000_000) == "2.50 GB" and fmt_bytes(5_000_000) == "5 MB"
    assert fmt_eta(None) == "calculando…" and fmt_eta(10) == "<1 min" and fmt_eta(3600 * 2 + 600) == "2 h 10 min"


def test_time_model_recovers_fixed_cost_and_throughput():
    a, b = 0.8, 30e6
    samples = [(a + s / b, s) for s in (50_000, 2_000_000, 5_000_000, 40_000_000, 300_000_000, 900_000_000,
                                        1_500_000, 8_000_000, 120_000_000, 3_000_000)]
    fa, fb = fit_time_model(samples)
    assert fa == pytest.approx(a, rel=0.02) and fb == pytest.approx(b, rel=0.02)
    assert fit_time_model(samples[:3]) is None                                   # pocos datos: no inventa


def make_manifest(tmp_path, committed, pending, gap=1.0, size=2_000_000, session_running=True):
    """committed/pending: nº de archivos; las confirmaciones terminan 5 s antes de NOW, una cada ``gap`` s."""
    mf = Manifest(tmp_path / "m.sqlite")
    sid = mf.start_session("U")
    if not session_running:
        from iphone_backup.domain.models import SessionStatus
        mf.finish_session(sid, SessionStatus.COMPLETED)
    for i in range(committed + pending):
        iid, _ = mf.upsert_discovered(sid, "U", DeviceFile(f"/DCIM/100APPLE/IMG_{i:04d}.JPG", size, i))
        if i < committed:
            mf.transition(iid, ItemStatus.STAGED, sha256=hashlib.sha256(bytes([i % 256])).hexdigest())
            mf.transition(iid, ItemStatus.VERIFIED, verify_status=VerifyStatus.VERIFIED_FULL)
            when = (NOW - timedelta(seconds=5 + (committed - i) * gap)).isoformat(timespec="seconds")
            mf.set_fields(iid, rel_path=f"2024/x{i}.jpg", imported_at=when)
            mf.transition(iid, ItemStatus.COMMITTED)
    return mf


def test_status_counts_progress_and_eta(tmp_path):
    mf = make_manifest(tmp_path, committed=60, pending=40, gap=2.0)
    mf.close()
    st = read_status(tmp_path / "m.sqlite", now=NOW)
    assert (st.done_files, st.pending_files, st.total_files) == (60, 40, 100)
    assert st.percent_bytes == pytest.approx(60.0) and st.session_status == "running"
    assert st.seconds_since_last == pytest.approx(7, abs=1) and not st.stalled
    assert st.files_per_s == pytest.approx(0.5, rel=0.1)
    assert st.eta_seconds == pytest.approx(40 * 2.0, rel=0.25)                    # 40 pendientes a ~2 s/archivo
    text = render_status(st)
    assert "60.0%" in text and "60 de 100" in text and "Tiempo restante estimado" in text


def test_stalled_session_is_flagged(tmp_path):
    mf = make_manifest(tmp_path, committed=20, pending=10, gap=200.0)            # último archivo hace ~205 s (> 3 min)
    mf.close()
    st = read_status(tmp_path / "m.sqlite", now=NOW)
    st.session_started_ago = 900.0                                               # la sesión lleva 15 min en marcha
    assert st.stalled and "sin actividad" in render_status(st)


def test_big_file_in_progress_or_fresh_session_is_not_a_stall(tmp_path):
    """Falsa alarma real: tras reiniciar, el último commit es de la sesión anterior; un MP4 de 2 GB en curso
    tampoco es un atasco si su .part sigue creciendo."""
    (tmp_path / "iPhone_Photos/_Manifest").mkdir(parents=True)
    staging = tmp_path / "iPhone_Photos/_Staging"
    staging.mkdir()
    mf = Manifest(tmp_path / "iPhone_Photos/_Manifest/manifest.sqlite")
    sid = mf.start_session("U")
    for i in range(15):
        iid, _ = mf.upsert_discovered(sid, "U", DeviceFile(f"/DCIM/100APPLE/IMG_{i:04d}.MP4", 1_000_000, i))
        if i < 10:
            mf.transition(iid, ItemStatus.STAGED, sha256=str(i) * 64)
            mf.transition(iid, ItemStatus.VERIFIED, verify_status=VerifyStatus.VERIFIED_FULL)
            mf.set_fields(iid, rel_path=f"x{i}", imported_at=(NOW - timedelta(seconds=2000)).isoformat(timespec="seconds"))
            mf.transition(iid, ItemStatus.COMMITTED)
    mf.close()
    (staging / "s1_i11.part").write_bytes(b"x" * 100)
    import os
    mp = tmp_path / "iPhone_Photos/_Manifest/manifest.sqlite"
    now = datetime.now(timezone.utc) + timedelta(seconds=600)                   # la sesión lleva 10 min; último commit hace >30 min
    st = read_status(mp, now=now)
    assert st.parts_in_progress == 1 and st.part_bytes == 100
    assert not st.stalled                                                        # consulta única: no acusa sin pruebas
    assert "Copiando ahora: 1 archivos" in render_status(st) and "(+100 MB en curso)" not in render_status(st)
    # el .part SIGUE creciendo (aunque su fecha no cambie, como en exFAT): no hay atasco
    (staging / "s1_i11.part").write_bytes(b"x" * 5000)
    st2 = read_status(mp, now=now + timedelta(seconds=200))
    assert st2.seconds_since_staging_activity == 0.0 and not st2.stalled
    # ...y si deja de crecer más de 3 min, SÍ se avisa
    st3 = read_status(mp, now=now + timedelta(seconds=200 + 240))
    assert st3.seconds_since_staging_activity == pytest.approx(240) and st3.stalled
    assert "sin actividad" in render_status(st3)


def test_finished_session_is_not_stalled_and_has_no_eta(tmp_path):
    mf = make_manifest(tmp_path, committed=20, pending=0, gap=60.0, session_running=False)
    mf.close()
    st = read_status(tmp_path / "m.sqlite", now=NOW)
    assert not st.stalled and st.eta_seconds is None and st.percent_bytes == 100.0


def test_status_is_read_only_and_works_while_the_copy_holds_a_write_transaction(tmp_path):
    mf = make_manifest(tmp_path, committed=12, pending=5)
    before = (tmp_path / "m.sqlite").read_bytes()
    mf._conn.execute("BEGIN IMMEDIATE")                                          # el proceso de copia está escribiendo
    try:
        st = read_status(tmp_path / "m.sqlite", now=NOW)
        assert st.done_files == 12 and st.pending_files == 5
    finally:
        mf._conn.execute("ROLLBACK")
    mf.close()
    assert (tmp_path / "m.sqlite").read_bytes() == before                        # no escribió nada


def test_live_bar_tracks_bytes_and_prints_plain_lines_when_not_a_tty():
    out = io.StringIO()
    lb = LiveBar(total_files=2, total_bytes=1000, out=out, plain_interval=0.0)
    lb.event("file_start", path="IMG_1.MOV", size=600)
    lb.event("bytes", path="IMG_1.MOV", done=300, total=600)
    assert "30.0%" in lb.line()                                                  # bytes del archivo en curso cuentan
    lb.event("file_done", path="IMG_1.MOV", size=600)
    lb.event("file_start", path="IMG_2.JPG", size=400)
    lb.event("file_done", path="IMG_2.JPG", size=400)
    lb.event("finished")
    text = out.getvalue()
    assert "\r" not in text and text.count("\n") >= 3                            # sin retornos de carro: legible en un archivo
    assert "2/2" in text.splitlines()[-1] and "100.0%" in text.splitlines()[-1]


def test_cli_status_command(tmp_path, capsys):
    mf = make_manifest(tmp_path / "x", committed=3, pending=2) if False else None
    dest = tmp_path / "hdd"
    (dest / "iPhone_Photos/_Manifest").mkdir(parents=True)
    m = Manifest(dest / "iPhone_Photos/_Manifest/manifest.sqlite")
    sid = m.start_session("U")
    for i in range(3):
        m.upsert_discovered(sid, "U", DeviceFile(f"/DCIM/100APPLE/IMG_{i}.JPG", 10, i))
    m.close()
    assert main(["status", "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "0 de 3" in out and "running" in out
    assert main(["status", "--dest", str(tmp_path / "nada")]) == 2
