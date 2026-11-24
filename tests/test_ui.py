"""La ventana real (PySide6, plataforma 'offscreen') recorre Principal -> Escaneo -> Respaldo -> Resumen."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from media_fixtures import make_jpeg, make_mov, needs_exiftool
from iphone_backup.app.workflow import UserSettings, Workflow
from iphone_backup.ui.main_window import PAGE_BACKUP, PAGE_HOME, PAGE_SCAN, PAGE_SUMMARY, MainWindow

pytestmark = pytest.mark.skipif(not needs_exiftool, reason="ExifTool no instalado")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def wait_for(qapp, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def win(qapp, tmp_path):
    d = tmp_path / "iphone/DCIM/100APPLE"
    make_jpeg(d / "IMG_0001.JPG", {"ExifIFD:DateTimeOriginal": "2024:03:10 12:00:00"})
    make_jpeg(d / "IMG_0002.JPG", {"ExifIFD:DateTimeOriginal": "2023:12:31 23:59:59"})
    make_mov(d / "IMG_0004.MOV", {"Keys:CreationDate": "2025:01:02 03:04:05+01:00"})
    (tmp_path / "hdd").mkdir()
    s = UserSettings(dest=str(tmp_path / "hdd"), use_gps=False, workers=2)
    w = MainWindow(Workflow(s, simulate=tmp_path / "iphone"), s, simulated=True, save_settings=False)
    w.show()
    yield w
    w.wf.disconnect()
    w.close()


def test_full_user_journey(win, qapp, tmp_path):
    assert "SIMULACIÓN" in win.windowTitle()                                   # lo simulado se rotula siempre
    assert wait_for(qapp, lambda: win._device_ok)
    assert "Conectado" in win.lbl_device.text() and "Espacio libre" in win.lbl_free.text()
    assert not win.btn_start.isEnabled()                                       # no se puede iniciar sin escanear
    assert win.btn_scan.isEnabled()
    win.btn_scan.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN)
    assert win.btn_start2.isEnabled() and "aún no tienes respaldados" in win.lbl_headline.text()
    assert "<b>3</b>" in win.lbl_headline.text() and win.reasons.rowCount() == 1
    assert "Respaldar 3 archivos" in win.btn_start2.text()
    assert not hasattr(win, "tree") and not hasattr(win, "sample")             # ya no se muestran estructura ni muestras
    assert [p for p in (tmp_path / "hdd").rglob("*") if p.suffix.lower() in (".jpg", ".mov")] == []
    win.btn_start2.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SUMMARY, 120)
    assert "completado" in win.lbl_outcome.text()
    assert win._model.files == 3
    texts = [win.sum_grid.itemAt(i).widget().text() for i in range(win.sum_grid.count())]
    assert "<b>3</b>" in texts
    win._home_again()
    assert win.stack.currentIndex() == PAGE_HOME
    # segundo escaneo: todo respaldado, no se ofrece copiar nada
    assert wait_for(qapp, lambda: win._task is None and win.btn_scan.isEnabled())   # terminó de reconectar
    win.btn_scan.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN and "Todo está respaldado" in win.lbl_headline.text())
    assert not win.btn_start2.isEnabled() and win.btn_start2.text() == "Nada que respaldar"


def test_cancel_keeps_state_recoverable(win, qapp, tmp_path):
    assert wait_for(qapp, lambda: win._device_ok)
    win.btn_scan.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN)
    win.btn_start2.click()
    win.cancel_backup()                                                         # cancelación inmediata y segura
    assert wait_for(qapp, lambda: win.stack.currentIndex() in (PAGE_SUMMARY, PAGE_HOME), 120)
    assert not list((tmp_path / "hdd/iPhone_Photos/_Staging").glob("*.part")) if (tmp_path / "hdd/iPhone_Photos/_Staging").exists() else True


def test_scan_mode_is_chosen_by_snapshot_age_and_stale_asks_the_user(win, qapp, monkeypatch):
    from datetime import timedelta
    asked = []
    modes = []
    real = win.wf.scan
    monkeypatch.setattr(win.wf, "scan", lambda **k: (modes.append(k["mode"]), real(**k))[1])
    assert wait_for(qapp, lambda: win._device_ok and win.btn_scan.isEnabled())
    assert "primero será completo" in win.lbl_snapshot.text()
    assert not win.btn_full.isVisible() and win.btn_scan.isEnabled()          # sin escaneo previo: un solo botón
    win.btn_scan.click()                                                        # sin instantánea: completo, sin preguntar
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN)
    assert modes == ["full"]
    win._home_again()
    assert wait_for(qapp, lambda: win._task is None and win.btn_scan.isEnabled())
    assert "Último escaneo completo" in win.lbl_snapshot.text()
    assert win.btn_full.isVisible()                                             # reciente: aparece la opción de forzar
    win.btn_scan.click()                                                        # reciente: rápido, sin preguntar
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN and len(modes) == 2)
    assert modes == ["full", "quick"]
    win._home_again()
    assert wait_for(qapp, lambda: win._task is None and win.btn_scan.isEnabled())
    win.settings.full_scan_after_hours = 0                                      # "pasaron las 24 h"
    win._refresh_snapshot_label()
    assert not win.btn_full.isVisible() and "te preguntaré" in win.lbl_snapshot.text()   # vencido: solo el botón principal
    monkeypatch.setattr(win, "_ask_full", lambda snap: (asked.append(snap), None)[1])   # el usuario cancela
    win.btn_scan.click()
    qapp.processEvents()
    assert len(asked) == 1 and modes == ["full", "quick"] and win.stack.currentIndex() == PAGE_HOME
    monkeypatch.setattr(win, "_ask_full", lambda snap: (asked.append(snap), "full")[1])  # el usuario elige completo
    win.btn_scan.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN and len(modes) == 3)
    assert modes == ["full", "quick", "full"] and len(asked) == 2


def test_gallery_shows_thumbnails_and_only_checked_items_are_backed_up(win, qapp, tmp_path):
    # miniatura nativa para una de las fotos del iPhone simulado
    from PIL import Image
    nat = tmp_path / "iphone/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.JPG"
    nat.mkdir(parents=True)
    Image.new("RGB", (480, 360), (0, 120, 0)).save(nat / "5005.JPG")
    assert wait_for(qapp, lambda: win._device_ok and win.btn_scan.isEnabled())
    win.btn_scan.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SCAN)
    g = win.gallery
    assert win.gallery_card.isVisible() and g.view.count() == 3
    assert "Marcados 3 de 3" in g.lbl.text() and "Respaldar 3 archivos" in win.btn_start2.text()
    # las miniaturas se cargan solas (carga diferida de lo visible): ninguna se queda en "cargando…"
    assert wait_for(qapp, lambda: len(g._pm_cache) == 3, 60)
    # desmarcar un elemento actualiza el botón y la selección que recibirá el motor
    g.view.item(0).setCheckState(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Unchecked)
    assert "Marcados 2 de 3" in g.lbl.text() and "Respaldar 2 archivos" in win.btn_start2.text()
    assert win.wf.selected is not None and len(win.wf.selected) == 2
    g.set_all(False)
    assert not win.btn_start2.isEnabled() and win.btn_start2.text() == "Nada marcado"
    g.set_all(True)
    assert win.wf.selected is None                                              # todo marcado = todo pendiente
    g.view.item(1).setCheckState(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Unchecked)
    win.btn_start2.click()
    assert wait_for(qapp, lambda: win.stack.currentIndex() == PAGE_SUMMARY, 120)
    assert win._model.files == 2
    copied = [p for p in (tmp_path / "hdd").rglob("*")
              if p.suffix.lower() in (".jpg", ".mov") and "_Manifest" not in p.parts]
    assert len(copied) == 2                                                     # solo lo marcado llegó al destino
    # miniaturas solo en local: las 2 fotos; el MOV sintético no es decodificable y queda "sin vista previa"
    assert len(list((tmp_path / "hdd/iPhone_Photos/_Manifest/thumbs").glob("*.jpg"))) == 2


def test_phase_panel_shows_all_phases_upfront_and_their_progress(win, qapp):
    p = win.phases
    assert set(p.state.values()) == {"waiting"}                                 # visibles de antemano
    p.update_phase("device", 5, 10, "5 de 10", False)
    assert p.state["device"] == "running" and p._bar["device"].value() == 5 and p._bar["device"].maximum() == 10
    p.update_phase("library", 0, 0, "Recorriendo carpetas", False)              # total desconocido: indeterminada
    assert p._bar["library"].maximum() == 0 and p.state["device"] == "done" and p.state["manifest"] == "done"
    p.update_phase("library", 1, 1, "listo", True)
    assert p.state["library"] == "done" and p.state["compare"] == "waiting"
    p.reset()
    assert set(p.state.values()) == {"waiting"}


def test_pause_and_resume_buttons(win, qapp):
    from iphone_backup.transfer.engine import EngineControl
    win._control = EngineControl()
    win.toggle_pause()
    assert win.btn_pause.text() == "Reanudar" and not win._control._running.is_set()
    win.toggle_pause()
    assert win.btn_pause.text() == "Pausar" and win._control._running.is_set()
