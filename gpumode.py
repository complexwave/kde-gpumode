#!/usr/bin/env python3
"""Small desktop widget (kept below windows): GPU temp/fan/power + mode switch (talks to gpumoded).

  gpumode.py             show the widget
  gpumode.py get         print current mode
  gpumode.py set MODE    switch mode (for scripts / keyboard shortcuts)
"""

import json
import os
import socket
import sys
import pynvml as nv
from PyQt5.QtCore import QPoint, QSettings, Qt, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

SOCK = os.environ.get("GPUMODE_SOCK", "/run/gpumode.sock")
POLL_INTERVAL_MS = 2000

STYLE = """
QWidget#box { background-color: rgba(30, 30, 30, 230); border-radius: 6px; }
QLabel { color: #ddd; }
QPushButton {
  color: #aaa; background: #333; border: 1px solid #555;
  border-radius: 4px; padding: 3px 12px;
}
QPushButton:checked { color: #111; background: #4fc3f7; border-color: #4fc3f7; }
QPushButton:disabled { color: #555; }
"""


def daemon(req):
    """Send one request to gpumoded. Returns reply dict or None if unreachable."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(SOCK)
            s.sendall(req.encode())
            return json.loads(s.recv(4096).decode())
    except (OSError, ValueError):
        return None


def temp_color(t):
    return "#81c784" if t < 60 else "#ffb74d" if t < 75 else "#e57373"


def gpu_rows():
    rows = []
    for i in range(nv.nvmlDeviceGetCount()):
        h = nv.nvmlDeviceGetHandleByIndex(i)
        name = nv.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode()
        name = name.replace("NVIDIA GeForce ", "")
        t = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
        fan = nv.nvmlDeviceGetFanSpeed(h)
        draw = nv.nvmlDeviceGetPowerUsage(h) // 1000
        limit = nv.nvmlDeviceGetPowerManagementLimit(h) // 1000
        rows.append(
            f'<span style="color:#aaa;">{name:<11}</span>'
            f' <span style="color:{temp_color(t)};">{t:>3}°C</span>'
            f' <span style="color:#ccc;">{fan:>3}%</span>'
            f' <span style="color:#ccc;">{draw:>3}/{limit}W</span>'
        )
    return "<pre style='margin:0'>" + "\n".join(rows) + "</pre>"


class GpuWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self._drag = None
        self._buttons = {}
        self._group = QButtonGroup(self)

        box = QWidget(objectName="box")
        self.label = QLabel()
        self.label.setTextFormat(Qt.RichText)
        self.label.setFont(QFont("monospace", 10))
        self.btn_row = QHBoxLayout()
        self.status = QLabel()

        inner = QVBoxLayout(box)
        inner.setContentsMargins(12, 8, 12, 8)
        inner.addWidget(self.label)
        inner.addLayout(self.btn_row)
        inner.addWidget(self.status)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        self.settings = QSettings("gpumode", "gpumode")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(POLL_INTERVAL_MS)
        self._refresh()
        self.adjustSize()
        self._restore_pos()

    def _refresh(self, reply=None):
        try:
            self.label.setText(gpu_rows())
        except nv.NVMLError as e:
            self.label.setText(f'<span style="color:#e57373;">NVML: {e}</span>')
        reply = reply or daemon("get")
        if reply is None:
            self.status.setText('<span style="color:#e57373;">gpumoded down</span>')
            self.status.show()
            for b in self._buttons.values():
                b.setEnabled(False)
            return
        self.status.hide()
        if list(self._buttons) != reply["modes"]:  # first reply, or config reloaded
            for b in self._buttons.values():
                self._group.removeButton(b)
                b.deleteLater()
            self._buttons = {}
            for m in reply["modes"]:
                b = QPushButton(m, checkable=True)
                b.clicked.connect(lambda _, m=m: self._refresh(daemon(f"set {m}")))
                self._group.addButton(b)
                self.btn_row.addWidget(b)
                self._buttons[m] = b
            QTimer.singleShot(0, self.adjustSize)
        for m, b in self._buttons.items():
            b.setEnabled(True)
            b.setChecked(m == reply["mode"])

    def _restore_pos(self):
        pos = self.settings.value("pos")
        if not isinstance(pos, QPoint):
            screen = QApplication.primaryScreen().availableGeometry()
            pos = QPoint(screen.right() - self.width() - 10, screen.top() + 10)
        self.move(pos)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPos() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.settings.setValue("pos", self.pos())

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.addAction("Quit", QApplication.quit)
        menu.exec_(e.globalPos())


def cli(args):
    reply = daemon(" ".join(args))
    if reply is None:
        sys.exit("gpumoded not reachable")
    if args[0] == "set" and reply["mode"] != args[-1]:
        sys.exit(f"no such mode (have: {', '.join(reply['modes'])})")
    print(reply["mode"])


def main():
    if sys.argv[1:2] in (["get"], ["set"]):
        return cli(sys.argv[1:3])
    nv.nvmlInit()
    os.environ.pop("SESSION_MANAGER", None)  # started by systemd, not by session restore
    app = QApplication(sys.argv)
    w = GpuWidget()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
