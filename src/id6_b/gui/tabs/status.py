"""Session status tab: catalog, user, scan number, output files, console log."""

import time
from pathlib import Path

from qtpy.QtCore import QSettings
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QFileDialog
from qtpy.QtWidgets import QFormLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLineEdit
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QVBoxLayout

from .base import PLACEHOLDER
from .base import BaseTab
from .base import value_label

#: Kernel states that mean the RunEngine cannot be queried right now.
_BUSY_STATES = ("busy", "dead")

#: Shared with ``app.py`` and ``tabs/macro.py``, which re-declare them for the
#: same reason: importing from ``app`` would be circular.
SETTINGS_ORG = "APS"
SETTINGS_APP = "id6b-gui"
LOG_PATH_KEY = "console/log_path"
LOG_ENABLED_KEY = "console/log_enabled"

#: Used when nothing is remembered yet, under the kernel's working directory --
#: where the data already goes -- rather than the GUI process's cwd.
DEFAULT_LOG_NAME = "console.log"

#: How often the "12.4 kB since 15:04" line is refreshed.  Its own timer, not
#: ``kernel_values_changed``: those stop arriving during a scan, which is
#: exactly when watching the log grow is worth anything.
LOG_STATUS_INTERVAL_MS = 1000


def _format_size(count):
    """Render a byte count compactly, for the log status line."""
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} kB"
    return f"{count / (1024 * 1024):.1f} MB"


class StatusTab(BaseTab):
    """Overview of the current Bluesky session."""

    title = "Session"

    def __init__(self, parent=None):
        """Build the Session, Scan, Files and Console log groups."""
        super().__init__(parent)

        self._fields = {}
        self._re_state = None
        self._kernel_state = None
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        layout = QVBoxLayout(self)
        layout.addWidget(
            self._group(
                "Session",
                [
                    ("catalog", "Catalog"),
                    ("n_runs", "Runs in catalog"),
                    ("login_id", "User"),
                    ("proposal_id", "Proposal"),
                    ("beamline_id", "Beamline"),
                    ("instrument_name", "Instrument"),
                ],
            )
        )
        layout.addWidget(
            self._group(
                "Scan",
                [
                    ("scan_id", "Last scan #"),
                    ("re_state", "RunEngine"),
                    ("kernel_state", "Kernel"),
                ],
            )
        )
        layout.addWidget(
            self._group(
                "Files",
                [
                    ("spec_file", "SPEC file"),
                    ("sample", "Sample"),
                    ("exp_path", "Experiment path"),
                    ("cwd", "Working directory"),
                ],
            )
        )
        layout.addWidget(self._build_log_group())
        layout.addStretch(1)

        # Runs only while logging, so an idle session pays nothing for it.
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(LOG_STATUS_INTERVAL_MS)
        self._log_timer.timeout.connect(self._refresh_log_status)

    def _group(self, title, fields):
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(form.labelAlignment())
        for key, caption in fields:
            label = value_label()
            self._fields[key] = label
            row = QHBoxLayout()
            row.addWidget(label)
            row.addStretch(1)
            form.addRow(f"{caption}:", row)
        return box

    def _set(self, key, value):
        label = self._fields.get(key)
        if label is None:
            return
        if value is None or value == "":
            label.setText(PLACEHOLDER)
        else:
            label.setText(str(value))

    # -- console log ----------------------------------------------------------

    def _build_log_group(self):
        """Path, Browse, Start/Stop and a live status line for the transcript.

        The console is the only record of what was done to the instrument, and
        it does not survive: qtconsole trims its scrollback, Restart clears the
        pane, and closing the window loses everything.  This group points that
        traffic at a file.
        """
        box = QGroupBox("Console log")
        form = QFormLayout(box)

        self._log_path_edit = QLineEdit()
        self._log_path_edit.setPlaceholderText(str(self._default_path()))
        self._log_path_edit.setToolTip(
            "File to append the console's input and output to.  Missing "
            "directories are created."
        )
        self._log_browse = QPushButton("Browse…")
        self._log_browse.clicked.connect(self._browse_log)
        path_row = QHBoxLayout()
        path_row.addWidget(self._log_path_edit, 1)
        path_row.addWidget(self._log_browse)
        form.addRow("File:", path_row)

        self._log_button = QPushButton("Start logging")
        self._log_button.clicked.connect(self._toggle_log)
        hint = QLabel("Appended to, never overwritten.")
        hint.setEnabled(False)
        button_row = QHBoxLayout()
        button_row.addWidget(self._log_button)
        button_row.addWidget(hint)
        button_row.addStretch(1)
        form.addRow("", button_row)

        self._log_status = value_label()
        status_row = QHBoxLayout()
        status_row.addWidget(self._log_status)
        status_row.addStretch(1)
        form.addRow("Status:", status_row)
        return box

    def _default_path(self):
        """Where to log when nothing has been chosen yet."""
        base = Path(self.session_cwd) if self.session_cwd else Path.cwd()
        return base / DEFAULT_LOG_NAME

    def sync_transcript(self):
        """Show the state of the shared transcript.

        Called by ``MainWindow`` once ``transcript`` and ``session_cwd`` are
        set -- which is *after* the window has already auto-resumed logging, so
        the tab has to read the transcript rather than assume it is idle.
        """
        transcript = self.transcript
        if transcript is not None and transcript.active:
            self._log_path_edit.setText(str(transcript.path))
        elif not self._log_path_edit.text().strip():
            remembered = self._settings.value(LOG_PATH_KEY, "", type=str)
            self._log_path_edit.setText(remembered or str(self._default_path()))
        self._log_path_edit.setPlaceholderText(str(self._default_path()))
        self._update_log_controls()

    def _browse_log(self):
        """Pick a log file.

        ``DontConfirmOverwrite`` because an existing file is appended to, so
        Qt's "replace it?" prompt would be describing something that does not
        happen.
        """
        current = self._log_path_edit.text().strip() or str(self._default_path())
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Console log file",
            current,
            "Log files (*.log);;All files (*)",
            options=QFileDialog.DontConfirmOverwrite,
        )
        if path:
            self._log_path_edit.setText(path)

    def _toggle_log(self):
        """Start logging to the named file, or stop the current log."""
        transcript = self.transcript
        if transcript is None:
            self._log_status.setText("no transcript available")
            return
        if transcript.active:
            transcript.stop()
            self._settings.setValue(LOG_ENABLED_KEY, False)
        else:
            path = self._log_path_edit.text().strip() or str(self._default_path())
            self._log_path_edit.setText(path)
            if transcript.start(path):
                self._settings.setValue(LOG_PATH_KEY, str(transcript.path))
                self._settings.setValue(LOG_ENABLED_KEY, True)
            else:
                # Remember the choice anyway: an unwritable path is usually a
                # mount that is not up yet, and the next launch may well work.
                self._settings.setValue(LOG_PATH_KEY, path)
        self._update_log_controls()

    def _update_log_controls(self):
        """Match the button, the path field and the timer to the log state."""
        transcript = self.transcript
        active = transcript is not None and transcript.active
        self._log_button.setText("Stop logging" if active else "Start logging")
        self._log_button.setEnabled(transcript is not None)
        # Locked while writing, so the file cannot be swapped out from under an
        # open handle -- stop first.
        self._log_path_edit.setEnabled(not active)
        self._log_browse.setEnabled(not active)
        if active:
            self._log_timer.start()
        else:
            self._log_timer.stop()
        self._refresh_log_status()

    def _refresh_log_status(self):
        """One line: what is being written, how much of it, and since when."""
        transcript = self.transcript
        if transcript is None:
            self._log_status.setText(PLACEHOLDER)
            return
        if self._log_timer.isActive() and not transcript.active:
            # The transcript closed itself -- a full disk, a vanished mount.
            # Stop the timer first, so re-entering here cannot recurse.
            self._log_timer.stop()
            self._update_log_controls()
            return
        if transcript.active:
            since = time.strftime("%H:%M", time.localtime(transcript.started_at))
            self._log_status.setText(
                f"writing to {transcript.path.name} — "
                f"{_format_size(transcript.bytes_written)} since {since}"
            )
        elif transcript.error:
            # Covers both a refused start and a log that stopped itself on a
            # full disk, which the user would otherwise never hear about.
            self._log_status.setText(f"not logging — {transcript.error}")
        else:
            self._log_status.setText("not logging")

    def on_metadata(self, metadata):
        """Fill the fields carried by the RunEngine metadata file."""
        self._set("catalog", metadata.get("databroker_catalog"))
        self._set("login_id", metadata.get("login_id"))
        self._set("proposal_id", metadata.get("proposal_id"))
        self._set("beamline_id", metadata.get("beamline_id"))
        self._set("instrument_name", metadata.get("instrument_name"))
        self._set("scan_id", metadata.get("scan_id"))

    def on_kernel_values(self, values):
        """Fill the fields that only the kernel can answer."""
        if "n_runs" in values:
            self._set("n_runs", values["n_runs"])
        if "spec_file" in values:
            self._set("spec_file", values["spec_file"])
        if "cwd" in values:
            self._set("cwd", values["cwd"])
        # Absent whenever experiment_setup() has not been run yet.
        self._set("sample", values.get("sample"))
        self._set("exp_path", values.get("exp_path"))
        if "re_state" in values:
            self._re_state = values["re_state"]
        self._refresh_state()

    def on_kernel_state(self, state):
        """Track kernel liveness and refresh the derived RunEngine state."""
        self._kernel_state = state
        self._refresh_state()

    def _refresh_state(self):
        """Show kernel state, and derive RunEngine state when it is busy.

        A running plan holds the shell channel, so ``RE.state`` cannot be
        polled mid-scan; busy is reported as ``running`` instead of going
        stale.
        """
        self._set("kernel_state", self._kernel_state)
        if self._kernel_state == "busy":
            self._set("re_state", "running")
        elif self._kernel_state in _BUSY_STATES:
            self._set("re_state", None)
        else:
            self._set("re_state", self._re_state)
