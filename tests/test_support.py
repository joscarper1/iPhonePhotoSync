"""Componente «Apoyar este proyecto»: configuración/validación (sin Qt) e interfaz (Qt offscreen)."""
import os

import pytest

from iphonephotosync.support.config import SupportConfig, build_model

GH, KOFI = "https://github.com/sponsors/alice", "https://ko-fi.com/alice"


def cfg(**over):
    d = {"enabled": True, "projectName": "Demo",
         "providers": {"githubSponsors": {"enabled": True, "url": GH}, "koFi": {"enabled": True, "url": KOFI}},
         "donationOptions": {"oneTime": True, "recurring": True}}
    d.update(over)
    return SupportConfig.from_dict(d)


def urls(m, kind=None):
    return [(l.provider.id, l.kind, l.url) for l in m.links if kind in (None, l.kind)]


def test_available_with_both_providers_and_kinds():
    m = build_model(cfg())
    assert m.available and len(m.links) == 4
    assert ("githubSponsors", "one_time", GH + "?frequency=one-time") in urls(m)
    assert ("githubSponsors", "recurring", GH + "?frequency=recurring") in urls(m)
    assert ("koFi", "one_time", KOFI) in urls(m) and ("koFi", "recurring", KOFI) in urls(m)


def test_disabled_or_no_providers_is_unavailable():
    assert not build_model(cfg(enabled=False)).available
    assert not build_model(cfg(providers={})).available
    assert not build_model(SupportConfig.from_dict(None)).available
    assert not build_model(cfg(providers={"koFi": {"enabled": False, "url": KOFI}})).available


def test_disabled_provider_hidden_and_single_provider():
    m = build_model(cfg(providers={"githubSponsors": {"enabled": False, "url": GH}, "koFi": {"enabled": True, "url": KOFI}}))
    assert {l.provider.id for l in m.links} == {"koFi"}


def test_donation_options_respected():
    m = build_model(cfg(donationOptions={"oneTime": True, "recurring": False}))
    assert m.kinds == ["one_time"]
    m = build_model(cfg(donationOptions={"oneTime": False, "recurring": True}))
    assert m.kinds == ["recurring"]
    assert not build_model(cfg(donationOptions={"oneTime": False, "recurring": False})).available


@pytest.mark.parametrize("bad", [
    "", "   ", None, 5, "https://github.com/sponsors/", "https://github.com/sponsors/USERNAME",
    "http://github.com/sponsors/alice", "https://evil.com/sponsors/alice", "https://github.com.evil.com/sponsors/alice",
    "https://user:pw@github.com/sponsors/alice", "https://github.com:8443/sponsors/alice",
    "https://github.com/sponsors/alice?x=1", "https://github.com/sponsors/alice#f", "javascript:alert(1)",
    "https://github.com/sponsors/al ice", "https://github.com/alice", "https://[::1/sponsors/alice"])
def test_malformed_or_foreign_urls_rejected(bad):
    m = build_model(cfg(providers={"githubSponsors": {"enabled": True, "url": bad}}))
    assert not m.available


def test_url_normalized_and_www_accepted():
    m = build_model(cfg(providers={"koFi": {"enabled": True, "url": "https://www.ko-fi.com/alice/"}}))
    assert urls(m, "one_time") == [("koFi", "one_time", KOFI)]


def test_provider_cannot_point_to_other_provider_host():
    m = build_model(cfg(providers={"koFi": {"enabled": True, "url": GH}}))
    assert not m.available


def test_texts_translatable_and_overridable():
    m = build_model(cfg(language="en", texts={"footer": "Thanks!", "bogus": "x"}))
    assert m.texts["title"] == "Enjoying this tool?" and m.texts["footer"] == "Thanks!" and "bogus" not in m.texts
    assert build_model(cfg(title="Custom", description="Desc")).heading == "Custom"
    assert build_model(cfg()).texts["menu_item"] == "Apoyar este proyecto"


def test_reuse_with_other_project_only_changes_config():
    other = SupportConfig.from_dict({"projectName": "Otra app", "providers": {
        "koFi": {"enabled": True, "url": "https://ko-fi.com/bob"}}, "donationOptions": {"oneTime": True, "recurring": False}})
    assert urls(build_model(other)) == [("koFi", "one_time", "https://ko-fi.com/bob")]


def test_app_default_config_ships_without_personal_urls():
    from iphonephotosync.support_config import SUPPORT
    assert not build_model(SupportConfig.from_dict(SUPPORT)).available      # hasta que se configure una URL real


# ------------------------------------------------------------------------------------------------ interfaz
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def labels(w):
    return [x.text() for x in w.findChildren(QtWidgets.QLabel)]


def test_dialog_buttons_open_exact_links(qapp):
    from iphonephotosync.support.dialog import SupportDialog
    opened = []
    d = SupportDialog(build_model(cfg()), opener=opened.append)
    assert [b.text().split("  ")[-1] for b in d.buttons] == ["GitHub Sponsors", "Ko-fi", "GitHub Sponsors", "Ko-fi"]
    assert opened == []                                            # nada se abre solo
    for b in d.buttons:
        b.click()
    m = build_model(cfg())
    assert opened == [u for k in m.kinds for _, _, u in urls(m, k)]       # agrupado por modalidad
    t = labels(d)
    assert "Demo" in t and "Hacer una contribución puntual" in t and "Convertirte en patrocinador mensual" in t
    assert not any("completad" in x.lower() or "gracias por tu donación" in x.lower() for x in t)


def test_dialog_single_provider_single_kind_no_empty_sections(qapp):
    from iphonephotosync.support.dialog import SupportDialog
    m = build_model(cfg(providers={"koFi": {"enabled": True, "url": KOFI}}, donationOptions={"oneTime": True, "recurring": False}))
    d = SupportDialog(m)
    assert len(d.buttons) == 1 and "Convertirte en patrocinador mensual" not in labels(d)


def test_dialog_options_logos_footer_and_plain_text(qapp):
    from iphonephotosync.support.dialog import SupportDialog
    d = SupportDialog(build_model(cfg(appearance={"showProviderLogos": False, "showFooterMessage": False},
                                      projectName="<b>X</b>")))
    assert d.buttons[0].text() == "GitHub Sponsors" and not any("Cada aportación" in x for x in labels(d))
    assert "<b>X</b>" in labels(d)                                  # se muestra literal, no como HTML


def test_dialog_closes_with_escape_and_button(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from iphonephotosync.support.dialog import SupportDialog
    d = SupportDialog(build_model(cfg(appearance={"displayMode": "window"})))
    assert not d.isModal()
    d.show()
    QTest.keyClick(d, Qt.Key_Escape)
    assert not d.isVisible()
    d2 = SupportDialog(build_model(cfg()))
    assert d2.isModal()
    d2.show()
    d2.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Close).click()
    assert not d2.isVisible()


def test_menu_installed_only_when_available(qapp):
    from iphonephotosync.support import install_support_menu
    w = QtWidgets.QMainWindow()
    assert install_support_menu(w, cfg(enabled=False)) is None
    assert w.menuBar().actions() == []                              # sin menú si no hay nada que ofrecer
    a = install_support_menu(w, cfg())
    assert a is not None and a.text() == "Apoyar este proyecto" and len(w.menuBar().actions()) == 1


def test_main_window_default_has_no_menu_and_configured_has(qapp, tmp_path):
    from iphonephotosync.app.workflow import UserSettings, Workflow
    from iphonephotosync.ui.main_window import MainWindow
    s = UserSettings(dest=str(tmp_path), use_gps=False, workers=1)
    (tmp_path / "iphone/DCIM").mkdir(parents=True)
    w = MainWindow(Workflow(s, simulate=tmp_path / "iphone"), s, simulated=True, save_settings=False)
    assert w.support_action is None and w.menuBar().actions() == []
    w.wf.disconnect()
    w2 = MainWindow(Workflow(s, simulate=tmp_path / "iphone"), s, simulated=True, save_settings=False, support=cfg())
    assert w2.support_action is not None
    w2.wf.disconnect()
