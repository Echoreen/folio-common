"""“Folio was updated” — a thin bar at the top of Folio and Numa (the Importer).

Every 30 seconds it checks whether any of the app's code files (or the shared folio-common code) changed since the app started (an
update from Claude, a git pull). If so it shows: “Folio was updated. Restart to use the new
version.” with Restart now / Later. Restart closes the app normally (so it saves and stops cleanly)
and opens it again a moment later, with the same command it was started with.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton

COMMON = Path(__file__).resolve().parent          # folio-common/folio_common


def app_folders() -> tuple:
    """The code this app runs: its own folder (Folio's, or Numa's folio_editor) plus the shared code
    (folio_common and folio_nfo), so an update to either shows the bar."""
    import __main__
    main = Path(getattr(__main__, "__file__", "") or ".").resolve().parent
    return (main, COMMON, COMMON.parent / "folio_nfo")


FOLDERS = None                                     # worked out when the bar is made (app_folders)
CHECK_EVERY_MS = 30_000


def code_stamp(folders=None) -> tuple:
    """(newest change time, number of files) of the .py files Folio runs from."""
    newest, count = 0.0, 0
    for d in (folders if folders is not None else app_folders()):
        try:
            for e in os.scandir(d):
                if e.name.endswith(".py") and e.is_file():
                    count += 1
                    newest = max(newest, e.stat().st_mtime)
        except OSError:
            pass
    return newest, count


def restart_command() -> list[str]:
    """The command this app was started with ("python3 -m folio_editor.importer", "python3 folio.py")."""
    argv = list(getattr(sys, "orig_argv", []) or [])
    if not argv:
        argv = [sys.executable] + sys.argv
    argv[0] = sys.executable
    return argv


def restart_later(delay: float = 1.5) -> None:
    """Start the same command again after a short wait (so this copy has closed first), detached."""
    cmd = ["sh", "-c", f'sleep {delay}; exec "$@"', "sh"] + restart_command()
    subprocess.Popen(cmd, cwd=os.getcwd(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class UpdateBanner(QFrame):
    """Hidden until the code changes. `window` is closed (normally) on Restart now."""

    def __init__(self, window, app_name: str = "Folio", folders=FOLDERS):
        super().__init__()
        self.window, self.folders = window, tuple(folders or app_folders())
        self.started = code_stamp(self.folders)
        self.dismissed: tuple | None = None
        self.setObjectName("updatebar")
        self.setStyleSheet("#updatebar { background: #2b2414; border-bottom: 1px solid #5a4a22; }"
                           " #updatebar QLabel { color: #f1d9a0; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 6, 14, 6)
        h.setSpacing(10)
        self.text = QLabel(f"{app_name} was updated. Restart to use the new version.")
        h.addWidget(self.text, 1)
        now = QPushButton("Restart now", objectName="primary")
        now.setCursor(Qt.CursorShape.PointingHandCursor)
        now.clicked.connect(self.restart)
        later = QPushButton("Later", objectName="link")
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        later.clicked.connect(self.later)
        h.addWidget(now)
        h.addWidget(later)
        self.hide()
        self.timer = QTimer(self)
        self.timer.setInterval(CHECK_EVERY_MS)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def check(self) -> bool:
        stamp = code_stamp(self.folders)
        if stamp != self.started and stamp != self.dismissed:
            self.show()
            return True
        return False

    def later(self):
        self.dismissed = code_stamp(self.folders)     # ask again only if it changes once more
        self.hide()

    def restart(self):
        restart_later()
        self.window.close()
        QApplication.instance().quit()
