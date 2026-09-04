"""Session status tab: catalog, user, scan number, output files, console log."""

import time
from pathlib import Path

from qtpy.QtCore import QSettings
from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QCheckBox
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

#: Key the *New data file* group's preview comes back under.  Its own
#: one-off request rather than a member of ``KERNEL_EXPRESSIONS``: it depends
#: on what has been typed, and it walks a SPEC file with spec2nexus, which is
#: not something to do once a second for a field nobody is looking at.
SPEC_PREVIEW_KEY = "spec_preview"

#: Debounce before the preview is asked for, a clone of ``HklTab``'s reference
#: and preset timers.  Long enough that typing a name is one request, not one
#: per keystroke.
SPEC_PREVIEW_DELAY_MS = 400

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
        self._spec_preview = None
        self._spec_pending = False
        self._spec_primed = False
        self._spec_reset_state = False
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
                    ("base_name", "File base name"),
                    ("sample", "Sample"),
                    ("exp_path", "Experiment path"),
                    ("cwd", "Working directory"),
                ],
            )
        )
        layout.addWidget(self._build_spec_group())
        layout.addWidget(self._build_log_group())
        layout.addStretch(1)

        # Runs only while logging, so an idle session pays nothing for it.
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(LOG_STATUS_INTERVAL_MS)
        self._log_timer.timeout.connect(self._refresh_log_status)

        self._spec_timer = QTimer(self)
        self._spec_timer.setSingleShot(True)
        self._spec_timer.setInterval(SPEC_PREVIEW_DELAY_MS)
        self._spec_timer.timeout.connect(self._request_spec_preview)
        self._show_spec_preview(None)

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

    # -- new data file --------------------------------------------------------

    def _build_spec_group(self):
        """One name, one button: a fresh SPEC file and a matching base name.

        Starting a data file today means knowing ``newSpecFile("name")`` and
        that the scan counter is ``RE.md["scan_id"]``.  Neither is discoverable
        here, and both have traps -- the counter is one behind, and a name that
        already exists silently refuses the reset -- so the group previews what
        the button would do before it is pressed.

        The field is the *file base name*: pressing the button names the SPEC
        file from it **and** sets ``experiment.file_base_name``, so
        ``09_01_jwkim.dat`` and ``jwkim_00001_master.hdf`` cannot drift apart.

        The sample is in the same group and applied by the same button, since
        ``experiment_path`` is ``base_experiment_path / sample`` and is the
        folder the masters go in: previewing the SPEC file against the typed
        base name but the masters against the *old* sample would answer half
        the question.  Either field may be left blank to keep what is in
        force, so changing only the sample is one field and one button.
        """
        box = QGroupBox("New data file")
        form = QFormLayout(box)

        self._spec_sample = QLineEdit()
        self._spec_sample.setPlaceholderText("keep the current sample")
        self._spec_sample.setToolTip(
            "The sub-folder under the base experiment path that the HDF "
            "masters are written to.  Created if it does not exist.  Leave "
            "blank to keep the current sample."
        )
        self._spec_sample.textChanged.connect(self._on_spec_name_changed)
        self._spec_sample.returnPressed.connect(self._start_new_file)
        sample_row = QHBoxLayout()
        sample_row.addWidget(self._spec_sample, 1)
        form.addRow("Sample:", sample_row)

        self._spec_name = QLineEdit()
        self._spec_name.setPlaceholderText("e.g. jwkim")
        self._spec_name.setToolTip(
            "Names the SPEC file (MM_DD_<name>.dat) and becomes the file base "
            "name the HDF masters are built from.  Leave blank to keep the "
            "current base name, which names the file already open."
        )
        self._spec_name.textChanged.connect(self._on_spec_name_changed)
        self._spec_name.returnPressed.connect(self._start_new_file)
        self._spec_button = QPushButton("Start new file")
        self._spec_button.clicked.connect(self._start_new_file)
        name_row = QHBoxLayout()
        name_row.addWidget(self._spec_name, 1)
        name_row.addWidget(self._spec_button)
        form.addRow("Base name:", name_row)

        self._spec_path_label = value_label()
        self._spec_file_label = value_label()
        self._spec_master_label = value_label()
        for caption, label in (
            ("Experiment path:", self._spec_path_label),
            ("SPEC file:", self._spec_file_label),
            ("Masters:", self._spec_master_label),
        ):
            row = QHBoxLayout()
            row.addWidget(label)
            row.addStretch(1)
            form.addRow(caption, row)

        # An indicator, not a control: there is no "append without resetting"
        # option to offer, and on an existing file the counter follows the file
        # whatever anyone ticks.  Deliberately *not* disabled -- a disabled
        # widget receives no events, so its tooltip, which is where the reason
        # lives, would never appear.  Enabled and made deaf instead: the
        # attribute stops the mouse, NoFocus stops the space bar, and the
        # toggled guard undoes anything that gets through either.
        self._spec_reset = QCheckBox()
        self._spec_reset.setFocusPolicy(Qt.NoFocus)
        self._spec_reset.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._spec_reset.toggled.connect(self._restore_reset_indicator)
        reset_row = QHBoxLayout()
        reset_row.addWidget(self._spec_reset)
        reset_row.addStretch(1)
        form.addRow("Scan number:", reset_row)

        self._spec_status = value_label()
        self._spec_status.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.addWidget(self._spec_status, 1)
        form.addRow("Status:", status_row)
        return box

    def _set_reset_indicator(self, checked, text, tooltip):
        """Show whether the counter will be reset, without emitting a toggle."""
        self._spec_reset_state = checked
        blocked = self._spec_reset.blockSignals(True)
        self._spec_reset.setChecked(checked)
        self._spec_reset.blockSignals(blocked)
        self._spec_reset.setText(text)
        self._spec_reset.setToolTip(tooltip)

    def _restore_reset_indicator(self, checked):
        """Undo a toggle that got past the mouse and focus guards."""
        if checked != self._spec_reset_state:
            blocked = self._spec_reset.blockSignals(True)
            self._spec_reset.setChecked(self._spec_reset_state)
            self._spec_reset.blockSignals(blocked)

    def _on_spec_name_changed(self):
        """Restart the debounce; the preview is stale from this moment on."""
        self._spec_preview = None
        self._update_spec_controls()
        self._spec_timer.start()

    def _request_spec_preview(self):
        """Ask the kernel what this name would do.  Reads only; moves nothing.

        Goes out on the invisible channel via :meth:`BaseTab.request`, which
        merges into the next *idle* poll -- so a preview asked for while a scan
        is running simply waits for it rather than queueing behind it.
        """
        title = self._spec_name.text().strip()
        sample = self._spec_sample.text().strip()
        self._spec_pending = True
        self._update_spec_controls()
        self.request({SPEC_PREVIEW_KEY: f"_gui_spec_preview({title!r}, {sample!r})"})

    def _show_spec_preview(self, preview):
        """Render a ``_gui_spec_preview`` reply, or the empty state."""
        self._spec_preview = preview if isinstance(preview, dict) else None
        preview = self._spec_preview

        if preview is None:
            self._spec_path_label.setText(PLACEHOLDER)
            self._spec_file_label.setText(PLACEHOLDER)
            self._spec_master_label.setText(PLACEHOLDER)
            self._set_reset_indicator(False, "—", "")
            self._spec_status.setText("Type a base name.")
            self._update_spec_controls()
            return

        self._show_exp_path(preview)

        if not preview.get("available") or preview.get("error"):
            self._spec_file_label.setText(PLACEHOLDER)
            self._spec_master_label.setText(PLACEHOLDER)
            self._set_reset_indicator(False, "—", "")
            self._spec_status.setText(
                preview.get("error") or "The SPEC writer could not be read."
            )
            self._update_spec_controls()
            return

        exists = bool(preview.get("spec_exists"))
        scans = preview.get("spec_scans")
        next_scan = preview.get("next_scan")
        spec_file = preview.get("spec_file") or PLACEHOLDER
        if exists:
            plural = "" if scans == 1 else "s"
            self._spec_file_label.setText(f"{spec_file} — exists, {scans} scan{plural}")
        else:
            self._spec_file_label.setText(f"{spec_file} — new")
        self._spec_file_label.setToolTip(f"In {preview.get('spec_dir') or '?'}")

        master = preview.get("master_file")
        if not master:
            self._spec_master_label.setText(
                f"{PLACEHOLDER} (experiment_setup() has not been run)"
            )
            self._spec_master_label.setToolTip("")
        else:
            free = "exists" if preview.get("master_exists") else "free"
            self._spec_master_label.setText(f"{Path(master).name} — {free}")
            self._spec_master_label.setToolTip(master)

        if preview.get("reset"):
            self._set_reset_indicator(
                True,
                f"will be reset — the next scan is #{next_scan}",
                "A new SPEC file starts the numbering again.",
            )
        else:
            self._set_reset_indicator(
                False,
                f"not reset — the next scan is #{next_scan}",
                "The file exists, so the counter follows the file.  Forcing it "
                "back would put duplicate #S numbers in one SPEC file.",
            )

        lines = []
        if preview.get("sample_change"):
            lines.append(
                f"The sample becomes {preview.get('sample')!r}"
                f"{'' if preview.get('exp_exists') else ' (a new folder)'}."
            )
        if preview.get("title_default"):
            lines.append(
                f"Keeping the base name {preview.get('title')!r}, so this is "
                "the SPEC file already open."
            )
        if exists:
            lines.append(
                f"Appending. The next scan will be #{next_scan}, not #1. "
                "Choose a base name that is not in use to start at 1."
            )
        else:
            lines.append(f"Ready. The next scan will be #{next_scan}.")
        if preview.get("master_exists"):
            lines.append(
                f"! {master} already exists; the next scan would raise FileExistsError."
            )
        if not preview.get("enabled"):
            lines.append(
                "! SPEC data files are disabled in iconfig.yml, so nothing "
                "would be written to this file."
            )
        self._spec_status.setText("  ".join(lines))
        self._update_spec_controls()

    def _show_exp_path(self, preview):
        """Render the folder the masters would go in, and whether it exists."""
        exp_path = preview.get("exp_path")
        if not exp_path:
            self._spec_path_label.setText(
                f"{PLACEHOLDER} (experiment_setup() has not been run)"
            )
            self._spec_path_label.setToolTip("")
            return
        state = "exists" if preview.get("exp_exists") else "will be created"
        self._spec_path_label.setText(f"{exp_path} — {state}")
        if preview.get("sample_change"):
            self._spec_path_label.setToolTip(
                f"The sample changes from {preview.get('sample_current')!r} "
                f"to {preview.get('sample')!r}."
            )
        else:
            self._spec_path_label.setToolTip("The sample is unchanged.")

    def _update_spec_controls(self):
        """Gate the button on an idle kernel, a name, and a fresh preview."""
        preview = self._spec_preview
        usable = bool(preview and preview.get("available") and not preview.get("error"))
        idle = self._kernel_state == "idle"
        self._spec_button.setEnabled(usable and idle and not self._spec_pending)
        if preview and preview.get("error"):
            tip = preview["error"]
        elif self._spec_pending or not usable:
            tip = "Waiting for the kernel to report what this name would do."
        elif not idle:
            tip = "The kernel is busy; wait for the current scan to finish."
        else:
            tip = (
                "Start this SPEC file, adopt the name as "
                "experiment.file_base_name, and apply the sample."
            )
        self._spec_button.setToolTip(tip)

    def _start_new_file(self):
        """Start the file, through the console.

        Not ``execute_once()``: this reframes the whole data record -- the SPEC
        file, the base name the HDF masters are built from, and the scan
        counter -- so it belongs in the history and the transcript, the same
        reasoning as the Agent tab's Approve and the HKL tab's Move.
        """
        if not self._spec_button.isEnabled():
            return
        title = self._spec_name.text().strip()
        sample = self._spec_sample.text().strip()
        if not self.run_in_console(f"_gui_new_spec_file({title!r}, {sample!r})"):
            self._spec_status.setText("No console available.")
            return
        self._spec_status.setText("Starting… the console has the result.")
        self._spec_pending = True
        self._update_spec_controls()
        # Re-preview once the kernel is idle again, so the group shows the file
        # that now exists rather than the one that was about to.
        self._spec_timer.start()

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
        if "base_name" in values:
            self._set("base_name", values["base_name"])
            # A placeholder, not the text: an empty field means "keep it", and
            # writing into it would fight whoever is typing.
            base_name = values["base_name"]
            self._spec_name.setPlaceholderText(
                f"keep {base_name}" if base_name else "e.g. jwkim"
            )
            if not self._spec_primed:
                # Nothing has been typed, but there is still something to
                # show: the file already open and the sample in force.  Set
                # first, so a reply that never comes cannot loop.
                self._spec_primed = True
                self._spec_timer.start()
        # Absent whenever experiment_setup() has not been run yet.
        self._set("sample", values.get("sample"))
        self._set("exp_path", values.get("exp_path"))
        if "sample" in values:
            sample = values["sample"]
            self._spec_sample.setPlaceholderText(
                f"keep {sample}" if sample else "keep the current sample"
            )
        if "re_state" in values:
            self._re_state = values["re_state"]
        if SPEC_PREVIEW_KEY in values:
            # The name field itself is never written from a poll reply -- it is
            # the user's text, and they may still be typing into it.
            self._spec_pending = False
            self._show_spec_preview(values[SPEC_PREVIEW_KEY])
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
        self._update_spec_controls()
