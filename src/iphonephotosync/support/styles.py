"""Estilos del diálogo. Solo roles de la paleta de Qt: se adapta solo al tema claro y oscuro del sistema."""

QSS = """
QDialog#supportDialog { background: palette(window); }
QLabel#supportIcon { color: palette(placeholder-text); font-size: 20px; }
QLabel#supportProject { font-size: 11pt; font-weight: 600; }
QLabel#supportTitle { font-size: 15pt; font-weight: 600; }
QLabel#supportSection { font-weight: 600; margin-top: 6px; }
QLabel#supportMuted { color: palette(placeholder-text); }
QPushButton.provider { min-height: 34px; padding: 4px 14px; text-align: left; }
QPushButton.provider:focus { border: 2px solid palette(highlight); }
"""
