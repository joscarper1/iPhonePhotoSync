import logging

from iphonephotosync.applog import get_user_logger, setup_logging


def flush():
    for name in ("iphonephotosync", "iphonephotosync.user"):
        for h in logging.getLogger(name).handlers:
            h.flush()


def test_separate_user_and_technical_logs(tmp_path):
    setup_logging(tmp_path, level="DEBUG")
    get_user_logger().info("Se copiaron 10 elementos")
    logging.getLogger("iphonephotosync.transfer").debug("detalle técnico interno")
    flush()
    user = (tmp_path / "usuario.log").read_text(encoding="utf-8")
    tech = (tmp_path / "tecnico.log").read_text(encoding="utf-8")
    assert "Se copiaron 10 elementos" in user and "detalle técnico" not in user
    assert "detalle técnico interno" in tech and "Se copiaron" not in tech


def test_tech_level_is_configurable(tmp_path):
    setup_logging(tmp_path, level="WARNING")
    logging.getLogger("iphonephotosync.x").info("info oculto")
    logging.getLogger("iphonephotosync.x").warning("aviso visible")
    flush()
    tech = (tmp_path / "tecnico.log").read_text(encoding="utf-8")
    assert "aviso visible" in tech and "info oculto" not in tech


def test_setup_twice_does_not_duplicate(tmp_path):
    setup_logging(tmp_path)
    setup_logging(tmp_path)
    get_user_logger().info("una sola vez")
    flush()
    assert (tmp_path / "usuario.log").read_text(encoding="utf-8").count("una sola vez") == 1
    setup_logging(tmp_path / "otro")  # libera los handlers anteriores (Windows bloquea archivos abiertos)
