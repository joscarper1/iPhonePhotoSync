"""Flujo de la aplicación (sin Qt) sobre un iPhone simulado con ExifTool real."""
import pytest

from media_fixtures import make_jpeg, make_mov, make_png, needs_exiftool
from iphonephotosync.app.workflow import ScanInvalid, UserSettings, Workflow
from iphonephotosync.device.simulated import SimulatedDeviceSource
from iphonephotosync.ui.progress_model import ProgressModel, fmt_eta

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")
GPS = {"GPS:GPSLatitude": "41.3851", "GPS:GPSLatitudeRef": "N", "GPS:GPSLongitude": "2.1734", "GPS:GPSLongitudeRef": "E"}


@pytest.fixture
def phone(tmp_path):
    d = tmp_path / "iphone/DCIM/100APPLE"
    make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00", **GPS})
    make_jpeg(d / "IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2023:12:31 23:59:59"})
    make_png(d / "IMG_0003.PNG")
    make_mov(d / "IMG_0004.MOV", {"Keys:CreationDate": "2025:01:02 03:04:05+01:00"})
    return tmp_path / "iphone"


@pytest.fixture
def wf(phone, tmp_path, monkeypatch):
    monkeypatch.setenv("IPHONEPHOTOSYNC_HOME", str(tmp_path / "cfg"))
    s = UserSettings(dest=str(tmp_path / "hdd"), use_gps=False, workers=2)
    (tmp_path / "hdd").mkdir()
    return Workflow(s, simulate=phone)


def test_old_settings_folder_is_still_read_after_the_project_rename(tmp_path, monkeypatch):
    from iphonephotosync.app.workflow import settings_path
    monkeypatch.delenv("IPHONEPHOTOSYNC_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert settings_path() == tmp_path / ".iphonephotosync" / "settings.json"              # sin configuración previa
    (tmp_path / ".iphone_backup").mkdir()
    (tmp_path / ".iphone_backup/settings.json").write_text('{"dest": "E:/x"}', encoding="utf-8")
    assert settings_path() == tmp_path / ".iphone_backup" / "settings.json"                # se respeta la guardada
    (tmp_path / ".iphonephotosync").mkdir()
    (tmp_path / ".iphonephotosync/settings.json").write_text("{}", encoding="utf-8")
    assert settings_path() == tmp_path / ".iphonephotosync" / "settings.json"              # la nueva tiene prioridad


def test_destination_inside_the_library_is_moved_to_its_parent(tmp_path):
    from iphonephotosync.app.workflow import normalize_dest
    lib = tmp_path / "Fotos y Videos/iPhone_Photos"
    (lib / "_Manifest").mkdir(parents=True)
    (lib / "_Manifest/backup_id.json").write_text("{}", encoding="utf-8")
    fixed, note = normalize_dest(str(lib))
    assert fixed == str(lib.parent) and "carpeta madre" in note                 # evita una biblioteca anidada vacía
    assert normalize_dest(str(lib.parent)) == (str(lib.parent), "")             # el destino correcto no se toca
    assert normalize_dest("") == ("", "")


def test_connect_reports_device_and_scan_report_matches_the_phone(wf):
    st = wf.connect()
    assert st.connected and st.state == "ok" and st.message
    r = wf.scan()
    assert (r.photos, r.videos, r.items) == (3, 1, 4)
    assert r.with_gps == 1 and r.without_gps == 3 and r.unknown_date == 1       # el PNG no tiene fecha
    assert r.total_bytes > 0 and r.free_bytes and r.space_ok and r.preview_lines and len(r.sample) == 4
    # el cruce con lo respaldado va DENTRO del mismo escaneo: destino vacío => todo es nuevo
    assert r.compared and r.pending_files == 4 and r.pending_bytes == r.total_bytes and r.backed_up == 0
    assert r.reasons == [("Nuevos: todavía no están en tu respaldo", 4, r.total_bytes)]


def test_scan_reports_every_phase_in_order_each_with_its_own_progress(wf, phone, tmp_path):
    (tmp_path / "hdd/Viejas").mkdir()
    (tmp_path / "hdd/Viejas/a.jpg").write_bytes((phone / "DCIM/100APPLE/IMG_0002.JPG").read_bytes())
    seen = []
    wf.connect()
    wf.scan(phase=lambda key, done, total, detail, fin: seen.append((key, done, total, fin)))
    keys = [k for k, _ in Workflow.PHASES]
    finished = [k for k, _, _, fin in seen if fin]
    assert finished == keys                                                     # las 4 fases terminan, en orden
    assert [k for k, *_ in seen] == sorted([k for k, *_ in seen], key=keys.index)   # nunca vuelve a una fase previa
    dev = [x for x in seen if x[0] == "device" and not x[3]]
    assert dev and dev[-1][2] == 4 and any(x[1] == 4 for x in dev)             # avance real 4 de 4 al leer el iPhone
    # sin nada pendiente, las fases 3 y 4 se marcan como innecesarias, no se quedan "colgadas"
    wf.run_backup(lambda *a, **k: None)
    seen.clear()
    wf.connect()
    wf.scan(phase=lambda key, done, total, detail, fin: seen.append((key, done, total, fin)))
    assert [k for k, _, _, fin in seen if fin] == keys


def _count_walks(monkeypatch):
    from iphonephotosync.library.external_index import ExternalIndex
    calls = []
    real = ExternalIndex.walk
    monkeypatch.setattr(ExternalIndex, "walk", lambda self: (calls.append(1), real(self))[1])
    return calls


def test_full_scan_saves_a_per_device_snapshot_and_quick_scan_reuses_it(wf, phone, tmp_path, monkeypatch):
    from datetime import timedelta
    walks = _count_walks(monkeypatch)
    wf.connect()
    assert wf.snapshot_info() is None and wf.is_stale(None)                     # sin instantánea: el primero es completo
    r = wf.scan(mode="full")
    assert r.snapshot_saved and len(walks) == 1
    snap = wf.snapshot_info()
    assert snap and snap.device_files == 4 and snap.udid and snap.age_hours() < 0.1
    assert not wf.is_stale(snap) and not wf.is_stale(snap, now=snap.saved_at + timedelta(hours=23))
    assert wf.is_stale(snap, now=snap.saved_at + timedelta(hours=25))
    # lo agregado a la biblioteca DESPUÉS de la instantánea no se ve en el modo rápido (lo avisa), sí en el completo
    (tmp_path / "hdd/Nueva").mkdir()
    (tmp_path / "hdd/Nueva/b.jpg").write_bytes((phone / "DCIM/100APPLE/IMG_0001.JPG").read_bytes())
    q = wf.scan(mode="quick")
    assert len(walks) == 1 and not q.snapshot_saved                             # rápido: no recorre el disco
    assert q.backed_up_library == 0 and any("índice" in n for n in q.compare_notes)
    f = wf.scan(mode="full")
    assert len(walks) == 2 and f.backed_up_library == 1 and f.pending_files == 3
    # respaldar reutiliza el listado del escaneo: no vuelve a recorrer el disco
    wf.run_backup(lambda *a, **k: None)
    assert len(walks) == 2


def test_snapshot_is_per_device_and_per_destination_and_survives_corruption(wf, tmp_path):
    from iphonephotosync.app.snapshots import SnapshotStore
    from iphonephotosync.library.external_index import ExtFile
    store = SnapshotStore(tmp_path / "m")
    store.save("UDID-A", "iPhone15", str(tmp_path / "hdd"), 10, [ExtFile("a/x.jpg", 5, 7)])
    store.save("UDID-B", "iPhone12", str(tmp_path / "hdd"), 20, [])
    assert store.info("UDID-A").device_files == 10 and store.info("UDID-B").device_files == 20
    assert store.load_listing("UDID-A") == [ExtFile("a/x.jpg", 5, 7)]
    assert store.info("UDID-A", dest=str(tmp_path / "otro")) is None            # otro destino: no vale
    assert store.info("UDID-C") is None
    (tmp_path / "m/scans/UDID-A.json").write_text("{roto", encoding="utf-8")
    assert store.info("UDID-A") is None                                         # dañada: se ignora, no revienta


def test_quick_without_a_snapshot_falls_back_to_a_real_walk(wf, monkeypatch):
    walks = _count_walks(monkeypatch)
    wf.connect()
    r = wf.scan(mode="quick")
    assert len(walks) == 1 and r.snapshot_saved


def test_selection_copies_only_the_chosen_groups_and_leaves_the_rest_pending(wf, tmp_path):
    wf.connect()
    r = wf.scan()
    groups = wf.pending_groups()
    assert len(groups) == 4 and sum(len(g.members) for g in groups) == 4 and r.pending_files == 4
    assert all(g.reason.startswith("Nuevos") for g in groups)
    chosen = {g.leader for g in groups[:2]}
    wf.set_selection(chosen)
    n, size = wf.selection_totals()
    assert n == 2 and size == sum(g.size for g in groups[:2])
    wf.set_selection(set())
    assert wf.can_start() == (False, "No marcaste nada para respaldar.")
    wf.set_selection(chosen)
    s = wf.run_backup(lambda *a, **k: None)
    assert s.copied == 2 and s.failed == 0
    # lo no elegido NO se copió ni se marcó como hecho: reaparece como pendiente en el siguiente escaneo
    wf.connect()
    r2 = wf.scan()
    assert r2.pending_files == 2 and {g.leader for g in wf.pending_groups()} == {g.leader for g in groups[2:]}
    assert wf.selected is None                                                  # un escaneo nuevo parte de "todo"


def test_live_photo_is_one_item_with_its_companions(tmp_path, monkeypatch):
    monkeypatch.setenv("IPHONEPHOTOSYNC_HOME", str(tmp_path / "cfg"))
    d = tmp_path / "iphone/DCIM/100APPLE"
    cid = "ABCDEF01-2345-6789-ABCD-EF0123456789"
    make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00", "Apple:ContentIdentifier": cid})
    make_mov(d / "IMG_0001.MOV", {"Keys:CreationDate": "2024:03:10 12:00:01-06:00", "Keys:ContentIdentifier": cid})
    (d / "IMG_0001.AAE").write_bytes(b"<plist/>")
    make_jpeg(d / "IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2025:01:01 12:00:00"})
    (tmp_path / "hdd").mkdir()
    wf = Workflow(UserSettings(dest=str(tmp_path / "hdd"), use_gps=False), simulate=tmp_path / "iphone")
    wf.connect()
    wf.scan()
    # los JPEG sintéticos no pueden llevar ContentIdentifier (solo HEIC real): se confirma el par a mano
    wf.plan.confirmed_pairs.add(("/DCIM/100APPLE/IMG_0001.JPG", "/DCIM/100APPLE/IMG_0001.MOV"))
    gs = {g.name: g for g in wf.pending_groups()}
    assert set(gs) == {"IMG_0001.JPG", "IMG_0002.JPG"}                          # el Live Photo es UN elemento
    assert gs["IMG_0001.JPG"].kind == "live" and len(gs["IMG_0001.JPG"].members) == 3
    assert wf.pending_groups()[0].name == "IMG_0002.JPG"                        # más reciente primero
    wf.set_selection({gs["IMG_0001.JPG"].leader})
    s = wf.run_backup(lambda *a, **k: None)
    assert s.copied == 3                                                        # foto + vídeo + .AAE juntos


def test_scan_crosses_with_manifest_and_existing_library(wf, phone, tmp_path):
    hdd = tmp_path / "hdd"
    (hdd / "Mis fotos viejas/2023").mkdir(parents=True)
    (hdd / "Mis fotos viejas/2023/vieja.jpg").write_bytes((phone / "DCIM/100APPLE/IMG_0002.JPG").read_bytes())
    wf.connect()
    r = wf.scan()
    assert r.backed_up_library == 1 and r.pending_files == 3                    # una ya existía en su biblioteca
    s = wf.run_backup(lambda *a, **k: None)
    assert s.skipped_existing == 1 and s.copied == 3 and s.failed == 0
    wf.connect()
    r2 = wf.scan()
    assert r2.pending_files == 0 and r2.backed_up == 4 and r2.backed_up_manifest == 3 and r2.backed_up_library == 1
    ok, why = wf.can_start()
    assert not ok and "ya está respaldado" in why
    # una foto nueva en el iPhone vuelve a aparecer como lo único pendiente, con su motivo
    make_jpeg(phone / "DCIM/100APPLE/IMG_0099.JPG", {"ExifIFD:DateTimeOriginal": "2026:01:01 10:00:00"})
    wf.connect()
    r3 = wf.scan()
    assert r3.pending_files == 1 and r3.reasons[0][0].startswith("Nuevos") and wf.can_start()[0]


def test_start_is_blocked_until_a_valid_scan_and_destination(wf, tmp_path):
    assert wf.can_start()[0] is False                                           # sin conectar
    wf.connect()
    assert wf.can_start() == (False, "Escanea el iPhone antes de iniciar el respaldo.")
    with pytest.raises(ScanInvalid):
        wf.run_backup(lambda *a, **k: None)
    wf.scan()
    assert wf.can_start() == (True, "")
    wf.settings.dest = ""
    assert wf.can_start()[0] is False


def test_scan_writes_nothing_to_the_library_and_full_backup_roundtrip(wf, tmp_path):
    wf.connect()
    wf.scan()
    media = [p for p in (tmp_path / "hdd").rglob("*") if p.suffix.lower() in (".jpg", ".mov", ".png")]
    assert media == []                                                          # escanear NO copia nada al destino
    events = []
    s = wf.run_backup(lambda kind, **k: events.append(kind))
    assert s.outcome == "completed" and s.copied == 4 and s.failed == 0 and s.verified == 4
    assert "start" in events and events.count("file_done") == 4
    assert s.unknown_date == 1
    hist = wf.history()
    assert len(hist) == 1 and hist[0]["status"] == "completed" and hist[0]["copied"] == 4
    # segunda vez: nada nuevo
    wf.connect()
    r2 = wf.scan()
    assert r2.pending_files == 0 and r2.backed_up == 4 and wf.can_start()[0] is False
    with pytest.raises(ScanInvalid):                                            # nada que copiar: no se ofrece iniciar
        wf.run_backup(lambda *a, **k: None)


def test_type_filter_excludes_videos(wf):
    wf.settings.include_videos = False
    wf.connect()
    r = wf.scan()
    assert r.videos == 0 and r.photos == 3 and all(not n.endswith(".MOV") for n, _, _ in r.sample)


def test_changing_settings_semantics_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("IPHONEPHOTOSYNC_HOME", str(tmp_path / "cfg"))
    assert UserSettings.load() == UserSettings()                                # valores seguros por defecto
    s = UserSettings(dest="D:/x", use_gps=False, max_detail="COUNTRY", include_videos=False)
    s.save()
    assert UserSettings.load() == s
    (tmp_path / "cfg/settings.json").write_text("{no es json", encoding="utf-8")
    assert UserSettings.load() == UserSettings()                                # archivo dañado: no revienta


def test_device_not_found_and_lost_during_scan_give_friendly_messages(tmp_path):
    from iphonephotosync.ports.device import DeviceNotFound
    def boom():
        raise DeviceNotFound("x")
    st = Workflow(UserSettings(), source_factory=boom).connect()
    assert not st.connected and st.state == "not_found" and "cable" in st.message


def test_progress_model_counts_in_flight_bytes_and_errors():
    m = ProgressModel()
    m.event("start", total=2, bytes=1000)
    m.event("file_start", item=1, path="a.MOV", size=600)
    m.event("bytes", item=1, path="a.MOV", done=300, total=600)
    assert m.percent == pytest.approx(30.0) and m.current == "a.MOV"
    m.event("file_done", item=1, path="a.MOV", size=600)
    m.event("file_error", item=2, path="b.JPG", message="boom")
    assert m.files == 1 and m.percent == pytest.approx(60.0) and m.errors == ["b.JPG: boom"]
    assert fmt_eta(None) == "calculando…" and fmt_eta(7200) == "2 h 00 min"
