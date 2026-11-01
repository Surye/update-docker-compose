#!/usr/bin/python3
"""KDE-friendly system tray updater for Docker Compose projects."""

from __future__ import annotations

import subprocess
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app_config import config_file, load_settings
from compose_backend import CheckResult, image_updates, status_file


def status_icon(color: QColor) -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(QColor("transparent"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    outline = QColor("#12304a")
    blue = QColor("#2496ed")
    groove = QColor("#d8efff")

    # Three Compose services arranged as a compact stack of containers.
    painter.setPen(QPen(outline, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(blue)
    painter.drawRoundedRect(4, 21, 20, 16, 3, 3)
    painter.drawRoundedRect(24, 21, 20, 16, 3, 3)
    painter.drawRoundedRect(14, 5, 20, 16, 3, 3)

    painter.setPen(QPen(groove, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for x in (9, 14, 19, 29, 34, 39):
        painter.drawLine(x, 23, x, 35)
    for x in (19, 24, 29):
        painter.drawLine(x, 7, x, 19)

    # A contrasting status badge stays legible in both light and dark panels.
    painter.setPen(QPen(QColor("#20242b"), 2.5))
    painter.setBrush(color)
    painter.drawEllipse(32, 1, 15, 15)
    painter.end()
    return QIcon(pixmap)


class Worker(QObject):
    finished = Signal(object)

    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation

    def run(self) -> None:
        try:
            result = image_updates()
        except RuntimeError as error:
            result = CheckResult((), (str(error),))
        except Exception as error:
            result = CheckResult((), (f"Check failed: {type(error).__name__}",))
        self.finished.emit(result)


class ComposeUpdater(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.app.setQuitOnLastWindowClosed(False)
        self.tray = QSystemTrayIcon(status_icon(QColor("#8b949e")), self.app)
        self.menu = QMenu()
        self.status_action = QAction("Checking Docker images…", self.menu)
        self.status_action.setEnabled(False)
        self.status_action.triggered.connect(self.open_cli)
        self.menu.addAction(self.status_action)
        self.pending_menu = self.menu.addMenu("Containers")
        self.pending_menu.menuAction().setVisible(False)
        self.menu.addSeparator()
        self.check_action = QAction("Check for updates", self.menu)
        self.check_action.triggered.connect(lambda: self.start_job("check"))
        self.menu.addAction(self.check_action)
        self.update_action = QAction("Run Docker Compose Updates", self.menu)
        self.update_action.triggered.connect(self.open_cli)
        self.menu.addAction(self.update_action)
        self.menu.addSeparator()
        open_action = QAction("Open configuration folder", self.menu)
        open_action.triggered.connect(self.open_configuration)
        self.menu.addAction(open_action)
        self.menu.addSeparator()
        quit_action = QAction("Quit", self.menu)
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)
        self.cli_process: subprocess.Popen | None = None
        self.cli_timer = QTimer(self)
        self.cli_timer.setInterval(1000)
        self.cli_timer.timeout.connect(self.poll_cli)
        self.tray.activated.connect(self.activate_tray)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Docker Compose updates: checking…")
        self.tray.show()

        self.thread: QThread | None = None
        self.last_result = CheckResult((), ())
        self.timer = QTimer(self)
        self.timer.setInterval(load_settings().check_interval_ms)
        self.timer.timeout.connect(lambda: self.start_job("check"))
        self.timer.start()
        self.status_modified = 0
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(1000)
        self.state_timer.timeout.connect(self.read_cached_status)
        self.state_timer.start()
        self.read_cached_status()
        QTimer.singleShot(1500, lambda: self.start_job("check"))

    def activate_tray(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_cli()

    def open_configuration(self) -> None:
        directory = config_file().parent
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(directory)])

    def open_cli(self) -> None:
        if self.cli_process is not None and self.cli_process.poll() is None:
            return
        desktop = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications/update-docker-compose.desktop"
        try:
            # Match Cachy-Update: GIO honors Terminal=true in the desktop launcher.
            self.cli_process = subprocess.Popen(
                ["gio", "launch", str(desktop)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError as error:
            self.tray.showMessage("Could not open updater", str(error), QSystemTrayIcon.MessageIcon.Warning)
            return
        self.cli_timer.start()

    def poll_cli(self) -> None:
        if self.cli_process is not None and self.cli_process.poll() is not None:
            if self.cli_process.returncode:
                self.tray.showMessage(
                    "Could not open updater", "Check the terminal application and run install.sh again.",
                    QSystemTrayIcon.MessageIcon.Warning,
                )
            if self.cli_process.stderr:
                self.cli_process.stderr.close()
            self.cli_process = None
            self.cli_timer.stop()

    def read_cached_status(self) -> None:
        try:
            file = status_file()
            modified = file.stat().st_mtime_ns
            if modified == self.status_modified:
                return
            state = json.loads(file.read_text())
            result = CheckResult(tuple(state["updates"]), tuple(state["errors"]))
        except (OSError, ValueError, KeyError, TypeError):
            return
        # An in-progress worker will deliver the newest status when it finishes.
        if self.thread is not None:
            return
        self.status_modified = modified
        self.last_result = result
        self.show_status()

    def start_job(self, operation: str) -> None:
        if self.thread is not None and self.thread.isRunning():
            return
        self.check_action.setEnabled(False)
        self.status_action.setEnabled(False)
        self.status_action.setText(
            "Checking Docker images…" if operation == "check" else "Updating Compose apps…"
        )
        self.tray.setIcon(status_icon(QColor("#d29922")))
        self.tray.setToolTip(self.status_action.text())
        self.thread = QThread(self)
        self.worker = Worker(operation)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.job_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.clear_thread)
        self.thread.start()

    def clear_thread(self) -> None:
        self.thread = None

    def job_finished(self, result: object) -> None:
        self.check_action.setEnabled(True)
        if isinstance(result, CheckResult):
            self.last_result = result
            self.show_status()
            if result.errors:
                self.tray.showMessage(
                    "Docker Compose check had errors",
                    "\n".join(result.errors[:5]),
                    QSystemTrayIcon.MessageIcon.Warning,
                )
    def show_status(self) -> None:
        updates = self.last_result.updates
        errors = self.last_result.errors
        if updates:
            label = f"{len(updates)} container update{'s' if len(updates) != 1 else ''} available"
            color = QColor("#f85149")
        elif errors:
            label = f"Docker check incomplete ({len(errors)} error{'s' if len(errors) != 1 else ''})"
            color = QColor("#d29922")
        else:
            label = "Docker containers are up to date"
            color = QColor("#3fb950")
        self.status_action.setText(label)
        self.status_action.setEnabled(bool(updates))
        self.pending_menu.clear()
        for update in updates:
            self.pending_menu.addAction(update, self.open_cli)
        self.pending_menu.menuAction().setVisible(bool(updates))
        detail = "\n".join(updates)
        if errors:
            detail = (detail + "\n" if detail else "") + "Check errors: " + "; ".join(errors)
        self.tray.setIcon(status_icon(color))
        self.tray.setToolTip(label + ("\n" + detail if detail else ""))

    def run(self) -> int:
        return self.app.exec()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray is available in this desktop session.", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(ComposeUpdater(app).run())
