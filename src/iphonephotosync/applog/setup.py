"""Configuración de logging.

* ``usuario.log``: eventos funcionales en español, nivel INFO. Sin rutas completas del dispositivo
  ni coordenadas: los mensajes de usuario deben ser seguros de compartir.
* ``tecnico.log``: diagnóstico (rotativo). Su nivel es configurable (DEBUG/INFO/WARNING).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

TECH_LOGGER = "iphonephotosync"
USER_LOGGER = "iphonephotosync.user"
_MARK = "_iphonephotosync_handler"


def _clear(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _MARK, False):
            logger.removeHandler(h)
            h.close()


def setup_logging(log_dir: Path | str, level: str = "INFO") -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    tech = logging.getLogger(TECH_LOGGER)
    user = logging.getLogger(USER_LOGGER)
    _clear(tech)
    _clear(user)
    tech.setLevel(logging.DEBUG)
    user.setLevel(logging.INFO)
    user.propagate = False  # el log de usuario no se mezcla con el técnico

    th = RotatingFileHandler(log_dir / "tecnico.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    th.setLevel(getattr(logging, level.upper(), logging.INFO))
    th.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    setattr(th, _MARK, True)
    tech.addHandler(th)

    uh = logging.FileHandler(log_dir / "usuario.log", encoding="utf-8")
    uh.setLevel(logging.INFO)
    uh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    setattr(uh, _MARK, True)
    user.addHandler(uh)


def get_user_logger() -> logging.Logger:
    return logging.getLogger(USER_LOGGER)
