"""Panel de fases del escaneo: todas visibles desde el inicio, cada una con su propia barra y estado."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QProgressBar

WAITING, RUNNING, DONE = "waiting", "running", "done"
MARK = {WAITING: "○", RUNNING: "▶", DONE: "✔"}


class PhasePanel(QGroupBox):
    def __init__(self, phases: list[tuple[str, str]], parent=None):
        super().__init__("Fases del escaneo", parent)
        self._order = [k for k, _ in phases]
        self._labels = dict(phases)
        self._mark, self._name, self._bar, self._detail, self.state = {}, {}, {}, {}, {}
        grid = QGridLayout(self)
        for row, (key, label) in enumerate(phases):
            self._mark[key] = QLabel()
            self._name[key] = QLabel(f"{row + 1}. {label}")
            bar = QProgressBar()
            bar.setFixedHeight(14)
            bar.setTextVisible(False)
            self._bar[key] = bar
            self._detail[key] = QLabel()
            grid.addWidget(self._mark[key], row, 0)
            grid.addWidget(self._name[key], row, 1)
            grid.addWidget(bar, row, 2)
            grid.addWidget(self._detail[key], row, 3)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(3, 260)
        self.reset()

    def reset(self) -> None:
        for key in self._order:
            self._set(key, WAITING, "En espera", 0, 1)

    def _set(self, key: str, state: str, detail: str, done: int, total: int) -> None:
        self.state[key] = state
        self._mark[key].setText(MARK[state])
        bold = state == RUNNING
        f = self._name[key].font()
        f.setBold(bold)
        self._name[key].setFont(f)
        bar = self._bar[key]
        if state == WAITING:
            bar.setRange(0, 1)
            bar.setValue(0)
        elif total <= 0:
            bar.setRange(0, 0)                     # se desconoce el total: barra indeterminada (animada)
        else:
            bar.setRange(0, total)
            bar.setValue(min(done, total))
        self._detail[key].setText(detail)

    def update_phase(self, key: str, done: int, total: int, detail: str, finished: bool) -> None:
        if key not in self._bar:
            return
        # las fases anteriores que no avisaron su fin se dan por terminadas (el orden es secuencial)
        for prev in self._order[: self._order.index(key)]:
            if self.state[prev] != DONE:
                self._set(prev, DONE, "Listo", 1, 1)
        self._set(key, DONE if finished else RUNNING, detail, done, total)
