"""HKL tab: diffractometer orientation, current position, and hkl → angles.

Everything here is backed by the ``_gui_hkl_*`` helpers installed in the kernel
by :mod:`id6_b.gui.hkl_bridge`, which mirror the call sequences in
``id6_b.utils.hkl_utils_pete``.

This tab *writes instrument state* -- samples, reflections, UB, mode, presets
are shared with the console session, so an edit here is as real as one typed at
the prompt.  Two guards follow from that: every mutating control is disabled
unless the kernel is idle, and a move is confirmed and then run **through the
console** so it appears in history and can be interrupted normally.
"""

import logging
import time
from pathlib import Path

from qtpy.QtCore import QSettings
from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QAbstractItemView
from qtpy.QtWidgets import QAbstractSpinBox
from qtpy.QtWidgets import QButtonGroup
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QDoubleSpinBox
from qtpy.QtWidgets import QFileDialog
from qtpy.QtWidgets import QFormLayout
from qtpy.QtWidgets import QGridLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QHeaderView
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLineEdit
from qtpy.QtWidgets import QMessageBox
from qtpy.QtWidgets import QProgressBar
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QRadioButton
from qtpy.QtWidgets import QScrollArea
from qtpy.QtWidgets import QSplitter
from qtpy.QtWidgets import QTableWidget
from qtpy.QtWidgets import QTableWidgetItem
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from ..hkl_bridge import DIFFRACTOMETERS
from ..hkl_bridge import POSITION_KEY
from ..hkl_bridge import STATE_KEY
from .base import PLACEHOLDER
from .base import BaseTab
from .base import value_label
from .diffract_panel import Diffract3DPanel

logger = logging.getLogger(__name__)

#: Reply key for a calculation request.
CALC_KEY = "hkl_calc"
#: Reply key for the status string returned by a mutating helper.
ACTION_KEY = "hkl_action"
#: Reply key for the listing of configuration files on disk.
CONFIG_FILES_KEY = "hkl_config_files"
#: Reply key for the listing of previous scans carrying an orientation.
CONFIG_SCANS_KEY = "hkl_config_scans"

#: Name rule for a configuration file, the same one
#: :data:`id6_b.utils.hkl_utils_pete.CONFIG_SUFFIX` applies.  Declared here
#: rather than imported: importing that module in the *GUI* process builds a
#: second RunEngine and resolves four devices out of ``oregistry``.
CONFIG_SUFFIX = "_6idb_config.yml"

#: How many of the newest catalog runs the scan picker inspects.  Each one
#: costs roughly 40 ms, which is why this is a button and not a poll.
CONFIG_SCAN_LIMIT = 15

#: Keep the picker to about six rows; the left pane already shares its
#: width with the 3D model.
CONFIG_TABLE_HEIGHT = 150

#: Wait this long after dispatching a console command before looking for
#: the kernel to be idle again.
CONFIG_RELOAD_MS = 1200

#: Where the last save or browse happened.  A fourth local copy of the
#: organisation/application names -- ``app.py``, ``macro.py`` and
#: ``status.py`` each declare their own, and importing from ``app.py``
#: would be circular.
SETTINGS_ORG = "APS"
SETTINGS_APP = "id6b-gui"
CONFIG_DIR_KEY = "hklconfig/directory"

#: Give the kernel this long to report itself busy before treating "not busy"
#: as "the move finished".
MOVE_GRACE_S = 2.0
#: Stop following a move after this long rather than spinning forever.
MOVE_TIMEOUT_S = 600.0

#: Cap on a numeric entry box, as in the Scan and Macro tabs.  A row of
#: widgets with no stretch factor shares its surplus width out among all of
#: them, so an uncapped box grows and drags its label away from it.
VALUE_WIDTH = 110

#: Cap on the six lattice boxes, which sat at their own ``sizeHint`` -- 112 px
#: for a/b/c, 102 for the angles -- and left the row looking padded.
#:
#: This is the narrowest they go while still *showing* their value.  At four
#: decimals ``120.0000`` is 73 px of glyphs and the frame costs 6 more, so a
#: literal half of ``VALUE_WIDTH`` (55) scrolls every box: even ``5.4310``
#: needs 53 px against the 49 px such a box leaves for text.  Dropping the
#: spin arrows (see :func:`_lattice_spin`) is what buys the rest of the
#: reduction. Going narrower means fewer decimals, not a smaller number here.
LATTICE_WIDTH = 80

LATTICE_RANGES = {
    "a": (0.01, 1000.0),
    "b": (0.01, 1000.0),
    "c": (0.01, 1000.0),
    "alpha": (0.01, 179.99),
    "beta": (0.01, 179.99),
    "gamma": (0.01, 179.99),
}


def _lattice_spin(minimum, maximum, value):
    """A lattice-constant box, half the width of an ordinary value box.

    The spin arrows go with the width: they cost about 18 px that the text
    needs at :data:`LATTICE_WIDTH`, and nobody sets a lattice constant by
    clicking an arrow — it is typed, or it comes from the sample. Scrolling
    the wheel over the box still steps the value.
    """
    box = _spin(minimum, maximum, value)
    box.setButtonSymbols(QAbstractSpinBox.NoButtons)
    box.setMaximumWidth(LATTICE_WIDTH)
    box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return box


def _spin(minimum, maximum, value=0.0, decimals=4):
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setValue(value)
    box.setKeyboardTracking(False)
    return box


class HklTab(BaseTab):
    """Orientation, readout and hkl → angles for the diffractometer."""

    title = "HKL"

    #: Provides its own scroll area.
    scrollable = False

    def __init__(self, parent=None):
        """Build the scrollable stack of orientation sections."""
        super().__init__(parent)
        self._state = {}
        self._kernel_idle = False
        self._real_fields = []
        self._lattice_names = []
        self._calculated = None
        self._loading = False
        self._config_rows = []
        self._configs_listed = False
        self._kernel_cwd = ""

        # Coalesces edits to the three psi-reference boxes into one write.
        # Each write briefly switches the diffractometer into a psi_constant
        # mode (see _gui_hkl_set_azimuth), so tabbing through h2/k2/l2 should
        # not do that three times over.
        self._reference_timer = QTimer(self)
        self._reference_timer.setSingleShot(True)
        self._reference_timer.setInterval(400)
        self._reference_timer.timeout.connect(self._apply_reference)

        # Same treatment for the fixed-angle boxes: one write once the user
        # has stopped editing, rather than one per keystroke or per tick.
        self._presets_timer = QTimer(self)
        self._presets_timer.setSingleShot(True)
        self._presets_timer.setInterval(400)
        self._presets_timer.timeout.connect(self._apply_presets)

        # Move progress.  The kernel's shell channel is blocked for the whole
        # move, so progress cannot be polled through it -- the readbacks are
        # watched directly over Channel Access from this process instead.
        self._move_timer = QTimer(self)
        self._move_timer.setInterval(200)
        self._move_timer.timeout.connect(self._update_progress)
        self._move_start = None
        self._move_target = None
        self._move_pvs = {}
        self._move_started_at = 0.0
        self._last_reals = {}

        # Re-read the tab after a configuration was saved or restored in
        # the console.  run_in_console is fire-and-forget and the kernel
        # state signal only fires on *transitions*, so a command that
        # starts and finishes between two 1 Hz polls produces none at all.
        # This waits for idle by re-arming itself rather than by listening
        # for an edge.
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(CONFIG_RELOAD_MS)
        self._reload_timer.timeout.connect(self._reload_after_console)

        body = QWidget()
        stack = QVBoxLayout(body)
        stack.addWidget(self._build_header())
        stack.addWidget(self._build_sample_group())
        stack.addWidget(self._build_reflection_group())
        stack.addWidget(self._build_mode_group())
        stack.addWidget(self._build_current_group())
        stack.addWidget(self._build_calc_group())
        stack.addWidget(self._build_config_group())
        stack.addStretch(1)

        area = QScrollArea()
        area.setWidget(body)
        area.setWidgetResizable(True)

        # Controls left, 3D model right, on a splitter so either side can be
        # given more room.
        self.model3d = Diffract3DPanel(self)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(area)
        split.addWidget(self.model3d)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self._status = QLabel("Waiting for the session…")
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        layout.addWidget(self._status)

        # Nothing has been read yet, so nothing may be pressed yet.  Without
        # this the controls are live for the second before the first poll
        # answers -- harmless for a combo box, not for Save to file.
        self._update_enabled()

    # -- construction -----------------------------------------------------

    def _build_header(self):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Diffractometer:"))
        self._device_combo = QComboBox()
        self._device_combo.addItems(DIFFRACTOMETERS)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        row.addWidget(self._device_combo)
        self._device_banner = QLabel()
        row.addWidget(self._device_banner)
        row.addStretch(1)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh)
        row.addWidget(self._refresh_button)
        self._update_banner()
        return box

    def _build_sample_group(self):
        box = QGroupBox("Sample")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._sample_combo = QComboBox()
        self._sample_combo.currentIndexChanged.connect(self._on_sample_selected)
        row.addWidget(self._sample_combo, 1)
        self._sample_name = QLineEdit()
        self._sample_name.setPlaceholderText("new sample name")
        row.addWidget(self._sample_name, 1)
        self._add_sample_button = QPushButton("Add")
        self._add_sample_button.clicked.connect(self._add_sample)
        row.addWidget(self._add_sample_button)
        self._remove_sample_button = QPushButton("Remove")
        self._remove_sample_button.clicked.connect(self._remove_sample)
        row.addWidget(self._remove_sample_button)
        outer.addLayout(row)

        grid = QGridLayout()
        self._lattice_boxes = {}
        for column, name in enumerate(("a", "b", "c", "alpha", "beta", "gamma")):
            low, high = LATTICE_RANGES[name]
            spin = _lattice_spin(low, high, 1.0 if name in "abc" else 90.0)
            self._lattice_boxes[name] = spin
            grid.addWidget(QLabel(name), 0, column)
            grid.addWidget(spin, 1, column)
            grid.setColumnStretch(column, 0)
        self._apply_lattice_button = QPushButton("Apply lattice")
        self._apply_lattice_button.clicked.connect(self._apply_lattice)
        grid.addWidget(self._apply_lattice_button, 1, 6)
        # Send the surplus width to an empty column on the right rather than
        # into the boxes, the pattern tabs/scan.py documents.  Without it the
        # narrowed boxes would simply be re-stretched back.
        grid.setColumnStretch(7, 1)
        outer.addLayout(grid)
        return box

    def _build_reflection_group(self):
        box = QGroupBox("Reflections")
        outer = QVBoxLayout(box)

        self._reflection_table = QTableWidget(0, 0)
        self._reflection_table.verticalHeader().setVisible(False)
        self._reflection_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._reflection_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._reflection_table.itemChanged.connect(self._on_reflection_edited)
        self._reflection_table.setMinimumHeight(140)
        outer.addWidget(self._reflection_table)

        row = QHBoxLayout()
        self._add_here_button = QPushButton("Add at current angles")
        self._add_here_button.clicked.connect(self._add_reflection_here)
        row.addWidget(self._add_here_button)
        self._remove_reflection_button = QPushButton("Remove selected")
        self._remove_reflection_button.clicked.connect(self._remove_reflection)
        row.addWidget(self._remove_reflection_button)
        self._first_button = QPushButton("Set as first")
        self._first_button.clicked.connect(lambda: self._set_orienting(0))
        row.addWidget(self._first_button)
        self._second_button = QPushButton("Set as second")
        self._second_button.clicked.connect(lambda: self._set_orienting(1))
        row.addWidget(self._second_button)
        row.addStretch(1)
        self._ub_button = QPushButton("Compute UB")
        self._ub_button.clicked.connect(self._compute_ub)
        row.addWidget(self._ub_button)
        outer.addLayout(row)
        return box

    def _build_mode_group(self):
        box = QGroupBox("Mode")
        form = QFormLayout(box)
        self._mode_combo = QComboBox()
        self._mode_combo.currentIndexChanged.connect(self._on_mode_selected)
        form.addRow("Mode:", self._mode_combo)
        self._constant_label = value_label()
        form.addRow("Constant axes:", self._constant_label)

        # One row per axis the mode holds constant, rebuilt when the mode
        # changes.  Ticked means "solve for this hkl with the axis at *this*
        # angle"; unticked means "use whatever the motor currently reads".
        #
        # Plus one row per *extra* the mode defines -- psi, in a psi_constant
        # mode.  An extra is held constant just as surely as mu or nu, but it
        # is not a real axis, so it never appears in ``constant_axis_names``
        # and would otherwise have nowhere to be shown.  It has no checkbox:
        # a mode that defines an extra always uses it, so there is no
        # "follow the motor" state to switch to.
        self._preset_host = QWidget()
        self._preset_grid = QGridLayout(self._preset_host)
        self._preset_grid.setContentsMargins(0, 0, 0, 0)
        self._preset_rows = {}
        self._preset_axes = []
        self._extra_rows = {}
        self._extra_axes = []
        form.addRow("Fixed angles:", self._preset_host)
        caption = QLabel(
            "Ticked axes are held at the value shown when solving; unticked "
            "axes follow the motor.  A row marked (always) is a parameter of "
            "the mode itself and is always in force.  Both change the "
            "calculation only — nothing moves."
        )
        caption.setWordWrap(True)
        form.addRow("", caption)

        self._presets_label = value_label()
        form.addRow("In effect:", self._presets_label)
        return box

    def _rebuild_preset_rows(self, axes, extras):
        """Build the mode's fixed-angle rows: constant axes, then extras.

        Only rebuilt when either list itself changes: the values are
        refreshed on every state read, and tearing the widgets down each time
        would pull the box out from under whoever is typing in it.
        """
        axes = list(axes or [])
        extras = list(extras or [])
        if axes == self._preset_axes and extras == self._extra_axes:
            return
        self._preset_axes = axes
        self._extra_axes = extras

        rows = list(self._preset_rows.values()) + list(self._extra_rows.values())
        for widgets in rows:
            for widget in widgets:
                # setParent(None) as well as deleteLater(): a deferred delete
                # is not processed until the event loop comes round, and until
                # then the old widget still paints over its former cell.
                self._preset_grid.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._preset_rows = {}
        self._extra_rows = {}

        row = 0
        for axis in axes:
            check = QCheckBox(axis)
            check.setToolTip(
                f"Hold {axis} at the value shown when solving.  Unticked, "
                f"{axis} follows its motor."
            )
            check.toggled.connect(self._on_preset_changed)
            spin = _spin(-360.0, 360.0, 0.0, decimals=4)
            spin.valueChanged.connect(self._on_preset_changed)
            unit = QLabel("deg")
            self._preset_grid.addWidget(check, row, 0)
            self._preset_grid.addWidget(spin, row, 1)
            self._preset_grid.addWidget(unit, row, 2)
            self._preset_rows[axis] = (check, spin, unit)
            row += 1
        for name in extras:
            label = QLabel(f"{name} (always)")
            label.setToolTip(
                f"{name} is a parameter of mode "
                f"{(self._state or {}).get('mode', '')}, not an axis.  The "
                "mode always solves with the value shown, so there is "
                "nothing to untick."
            )
            spin = _spin(-360.0, 360.0, 0.0, decimals=4)
            spin.valueChanged.connect(self._on_preset_changed)
            unit = QLabel("deg")
            self._preset_grid.addWidget(label, row, 0)
            self._preset_grid.addWidget(spin, row, 1)
            self._preset_grid.addWidget(unit, row, 2)
            self._extra_rows[name] = (label, spin, unit)
            row += 1
        self._preset_grid.setColumnStretch(3, 1)

    def _preset_values(self):
        """Return ``{axis: angle}`` for the ticked axes only."""
        return {
            axis: spin.value()
            for axis, (check, spin, _unit) in self._preset_rows.items()
            if check.isChecked()
        }

    def _extra_values(self):
        """Return ``{name: value}`` for the mode's extras -- all of them."""
        return {
            name: spin.value()
            for name, (_label, spin, _unit) in self._extra_rows.items()
        }

    def _build_current_group(self):
        box = QGroupBox("Current position")
        form = QFormLayout(box)
        self._current = {}
        for key, caption in (
            ("hkl", "H K L"),
            ("angles", "Angles"),
            ("two_theta", "2θ"),
            ("psi", "ψ"),
            ("psi_reference", "ψ reference (h2 k2 l2)"),
            ("beam", "λ (energy)"),
        ):
            if key == "angles":
                form.addRow(f"{caption}:", self._build_angles_widget())
                continue
            label = value_label()
            self._current[key] = label
            form.addRow(f"{caption}:", label)
        return box

    def _build_angles_widget(self):
        """Two stacked rows for the angles: names above, values below.

        As one line -- ``mu=0.0000  eta=0.0000  …`` -- six axes repeat the
        name and the ``=`` on every reading and run to roughly twice the
        width.  A column per axis says each name once and lets the numbers
        line up under it.
        """
        holder = QWidget()
        self._angle_grid = QGridLayout(holder)
        self._angle_grid.setContentsMargins(0, 0, 0, 0)
        self._angle_grid.setHorizontalSpacing(14)
        self._angle_grid.setVerticalSpacing(0)
        self._angle_cells = {}
        self._angle_names = []
        # Shown until the first reading arrives, and again if the axes ever
        # come back empty -- an empty grid would silently look like a bug.
        self._angle_empty = value_label()
        self._angle_grid.addWidget(self._angle_empty, 0, 0)
        return holder

    def _set_angles(self, reals):
        """Fill the angle columns, rebuilding them if the axes changed."""
        names = [name for name, value in reals.items() if value is not None]
        if names != self._angle_names:
            self._angle_names = names
            for widget in list(self._angle_cells.values()) + [self._angle_empty]:
                # setParent(None) as well as deleteLater(), the trap
                # _rebuild_preset_rows documents: a deferred delete leaves the
                # old widget painting over its former cell until the event
                # loop gets to it.
                self._angle_grid.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
            self._angle_cells = {}
            self._angle_empty = value_label()

            if not names:
                self._angle_grid.addWidget(self._angle_empty, 0, 0)
                return

            for column, name in enumerate(names):
                caption = QLabel(name)
                caption.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                cell = value_label()
                cell.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._angle_grid.addWidget(caption, 0, column)
                self._angle_grid.addWidget(cell, 1, column)
                self._angle_cells[name] = cell
                self._angle_grid.setColumnStretch(column, 0)
            # Surplus to the right, so the columns stay next to each other
            # instead of being spread across the group.
            self._angle_grid.setColumnStretch(len(names), 1)

        for name in names:
            self._angle_cells[name].setText(f"{reals[name]:.4f}")

    def _build_calc_group(self):
        box = QGroupBox("Calculate and move")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._calc_boxes = {}
        for name in ("h", "k", "l"):
            spin = _spin(-100.0, 100.0, 0.0, decimals=4)
            # Cap the box and let the trailing stretch below take the slack.
            # Uncapped, the row's surplus width was shared out over every
            # widget in it, so each one-character label was stretched to
            # ~107 px and its value box ended up an inch to the right of it.
            spin.setMaximumWidth(VALUE_WIDTH)
            self._calc_boxes[name] = spin
            label = QLabel(name)
            label.setBuddy(spin)
            row.addWidget(label)
            row.addWidget(spin)
            row.addSpacing(12)
        # No ψ box here: in a psi_constant mode ψ is one of the mode's fixed
        # values and lives in the Mode group with the other ones.  It used to
        # be in both places, and the two disagreed -- typing ψ under Fixed
        # angles and then pressing Calculate solved against this box's stale
        # value instead.
        self._calc_button = QPushButton("Calculate")
        self._calc_button.clicked.connect(self._calculate)
        row.addWidget(self._calc_button)
        self._move_button = QPushButton("Move")
        self._move_button.clicked.connect(self._move)
        self._move_button.setEnabled(False)
        row.addWidget(self._move_button)
        row.addStretch(1)
        outer.addLayout(row)

        self._calc_result = value_label()
        outer.addWidget(self._calc_result)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        ref = QHBoxLayout()
        ref.addWidget(QLabel("ψ reference:"))
        self._ref_boxes = {}
        for name in ("h2", "k2", "l2"):
            spin = _spin(-100.0, 100.0, 0.0, decimals=4)
            # Applied automatically -- no button.  Keyboard tracking is off
            # (see _spin), so a typed value lands on Enter or focus-out rather
            # than once per keystroke.
            spin.valueChanged.connect(self._on_reference_changed)
            self._ref_boxes[name] = spin
            ref.addWidget(QLabel(name))
            ref.addWidget(spin)
        ref.addWidget(QLabel("(applied automatically)"))
        ref.addStretch(1)
        outer.addLayout(ref)
        return box

    def _build_config_group(self):
        """Save the orientation to a file, or restore one from file or scan.

        An orientation costs beam time to build and until now could only be
        saved or restored by typing one of the three functions in
        ``utils/hkl_utils_pete.py``.  None of them can be called from here:
        all three block on ``input()`` and this kernel runs with
        ``allow_stdin=False``.  The group drives the non-interactive core
        those functions were refactored onto, so the file it writes is the
        file the console writes.
        """
        box = QGroupBox("Configuration")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Save the current orientation:"))
        self._save_config_button = QPushButton("Save to file…")
        self._save_config_button.clicked.connect(self._save_config)
        row.addWidget(self._save_config_button)
        row.addStretch(1)
        outer.addLayout(row)

        source = QHBoxLayout()
        source.addWidget(QLabel("Load from:"))
        self._source_group = QButtonGroup(self)
        self._file_radio = QRadioButton("File")
        self._file_radio.setToolTip(
            f"A *{CONFIG_SUFFIX} file in the session's working directory, "
            "or any other reached with Browse…"
        )
        self._scan_radio = QRadioButton("Previous scan")
        self._scan_radio.setToolTip(
            "The orientation saved inside a run by the configuration run "
            "wrapper.  Not every run carries one.  Runs are listed by uid: "
            "the scan counter is reset with every new SPEC file, so the "
            "numbers repeat."
        )
        self._file_radio.setChecked(True)
        for button in (self._file_radio, self._scan_radio):
            self._source_group.addButton(button)
            source.addWidget(button)
        self._config_refresh_button = QPushButton("Refresh")
        self._config_refresh_button.setToolTip(
            "Re-read the list.  Not polled: a directory walk, or ~40 ms a run "
            "for the catalog, is not something to do once a second."
        )
        self._config_refresh_button.clicked.connect(self._refresh_configs)
        source.addWidget(self._config_refresh_button)
        source.addStretch(1)
        outer.addLayout(source)
        # Connected last, so setChecked above cannot fire a listing while the
        # rest of the group is still being built.
        self._source_group.buttonToggled.connect(self._on_config_source)

        # One table for both sources, its columns relabelled when the source
        # changes -- fewer widgets than two tables, and this pane is already
        # sharing its width with the 3D model.
        self._config_table = QTableWidget(0, 3)
        self._config_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._config_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._config_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._config_table.verticalHeader().setVisible(False)
        self._config_table.setMaximumHeight(CONFIG_TABLE_HEIGHT)
        self._config_table.itemSelectionChanged.connect(self._update_enabled)
        outer.addWidget(self._config_table)

        how = QHBoxLayout()
        self._clear_group = QButtonGroup(self)
        self._replace_radio = QRadioButton("Replace everything")
        self._replace_radio.setToolTip(
            "Discard the samples, reflections and UB in the session and put "
            "the saved ones in their place."
        )
        self._merge_radio = QRadioButton("Add to what's there")
        self._merge_radio.setToolTip(
            "Keep the current samples and add the saved ones alongside them.  "
            "A saved sample with the same name replaces its namesake."
        )
        # Replace is the default, matching hklpy2's own restore() and the
        # console prompt's [o]verwrite.
        self._replace_radio.setChecked(True)
        for button in (self._replace_radio, self._merge_radio):
            self._clear_group.addButton(button)
            how.addWidget(button)
        how.addStretch(1)
        outer.addLayout(how)

        actions = QHBoxLayout()
        self._load_config_button = QPushButton("Load selected")
        self._load_config_button.clicked.connect(self._load_config)
        actions.addWidget(self._load_config_button)
        self._browse_config_button = QPushButton("Browse…")
        self._browse_config_button.setToolTip(
            "Load a configuration file from outside the working directory."
        )
        self._browse_config_button.clicked.connect(self._browse_config)
        actions.addWidget(self._browse_config_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        self._config_status = value_label()
        # Wrapped, or the pane cannot shrink.  value_label() is monospace and
        # these sentences run past a hundred characters, so an unwrapped label
        # reports a minimumSizeHint over a thousand pixels wide and drags the
        # whole HKL tab out with it -- worst on the scan source, whose status
        # is the longest.
        self._config_status.setWordWrap(True)
        outer.addWidget(self._config_status)
        return box

    # -- kernel exchange --------------------------------------------------

    @property
    def device(self):
        """Name of the diffractometer the tab is acting on."""
        return self._device_combo.currentText()

    def refresh(self):
        """Re-read the full orientation state."""
        self.request({STATE_KEY: f"_gui_hkl_state({self.device!r})"})

    def _request_position(self):
        self.request({POSITION_KEY: f"_gui_hkl_position({self.device!r})"})

    def _act(self, call):
        """Run a mutating helper and refresh from whatever it did."""
        if self.poller is None:
            return
        self.request({ACTION_KEY: call})

    def on_kernel_state(self, state):
        """Track idleness: nothing may be edited while a plan is running."""
        self._kernel_idle = state == "idle"
        if state in ("idle", "dead"):
            self._finish_progress()
        if state == "idle" and not self._state:
            self.refresh()
        if state == "dead":
            self._status.setText("Kernel is not running.")
        self._update_enabled()

    def on_kernel_values(self, values):
        """Consume state, position, action and calculation replies."""
        if STATE_KEY in values:
            state = values[STATE_KEY]
            if isinstance(state, dict) and state.get("device") == self.device:
                self._state = state
                self._apply_state(state)
        if POSITION_KEY in values:
            position = values[POSITION_KEY]
            if isinstance(position, dict) and position.get("device") == self.device:
                self._apply_position(position)
        if ACTION_KEY in values:
            self._status.setText(str(values[ACTION_KEY]))
            self.refresh()
        if CALC_KEY in values:
            self._apply_calculation(values[CALC_KEY])
        if CONFIG_FILES_KEY in values:
            self._show_config_files(values[CONFIG_FILES_KEY])
        if CONFIG_SCANS_KEY in values:
            self._show_config_scans(values[CONFIG_SCANS_KEY])
        # The kernel's *live* working directory, which is where a relative
        # path it is handed would land.  BaseTab.session_cwd is the directory
        # the kernel was launched in and goes stale the moment anything
        # chdirs -- starting a new SPEC file does.
        if values.get("cwd"):
            self._kernel_cwd = str(values["cwd"])
        # Keep the cheap readout coming while this tab is on screen.
        if self.isVisible() and self._kernel_idle:
            self._request_position()
        # First listing, once the session is up.  Guarded by the flag rather
        # than by an idle *transition*, which may already have happened by the
        # time this tab is built.
        if self._kernel_idle and not self._configs_listed:
            self._refresh_configs()

    def on_document(self, name, doc):
        """A finished run may have moved the diffractometer."""
        if name == "stop":
            self.refresh()

    # -- view updates -----------------------------------------------------

    def _apply_state(self, state):
        self._loading = True
        try:
            self._real_fields = state.get("real_fields") or []
            self._lattice_names = state.get("lattice_names") or []

            samples = state.get("samples") or {}
            self._sample_combo.clear()
            self._sample_combo.addItems(sorted(samples))
            current = state.get("sample")
            index = self._sample_combo.findText(current or "")
            if index >= 0:
                self._sample_combo.setCurrentIndex(index)
            for name, spin in self._lattice_boxes.items():
                value = (samples.get(current) or {}).get(name)
                if value is not None:
                    spin.setValue(value)

            self._fill_reflections(state)

            self._mode_combo.clear()
            self._mode_combo.addItems(state.get("modes") or [])
            index = self._mode_combo.findText(state.get("mode") or "")
            if index >= 0:
                self._mode_combo.setCurrentIndex(index)
            constant_axes = state.get("constant_axes") or []
            self._constant_label.setText(", ".join(constant_axes) or PLACEHOLDER)
            presets = state.get("presets") or {}
            extras = state.get("extras") or {}
            self._rebuild_preset_rows(constant_axes, state.get("extra_axes"))
            for axis, (check, spin, _unit) in self._preset_rows.items():
                value = presets.get(axis)
                check.setChecked(value is not None)
                if value is not None:
                    spin.setValue(value)
            for name, (_label, spin, _unit) in self._extra_rows.items():
                value = extras.get(name)
                if value is not None:
                    spin.setValue(value)
            in_effect = dict(presets)
            in_effect.update({k: v for k, v in extras.items() if k in self._extra_rows})
            self._presets_label.setText(
                ", ".join(f"{k}={v:g}" for k, v in in_effect.items())
                or ("none — every constant axis follows its motor")
            )

            reference = state.get("psi_reference") or {}
            self.model3d.set_orientation(state.get("ub"), reference)
            for name, spin in self._ref_boxes.items():
                if name in reference and reference[name] is not None:
                    spin.setValue(reference[name])
        finally:
            self._loading = False
        self._apply_position(state)
        self._update_enabled()

    def _fill_reflections(self, state):
        reflections = state.get("reflections") or []
        headers = ["key", "h", "k", "l"] + list(self._real_fields) + ["orienting"]
        table = self._reflection_table
        table.clear()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(reflections))
        for row, ref in enumerate(reflections):
            values = [ref["key"]]
            values += [ref["pseudos"].get(f) for f in ("h", "k", "l")]
            values += [ref["reals"].get(f) for f in self._real_fields]
            values += [ref.get("tag", "")]
            for column, value in enumerate(values):
                if isinstance(value, float):
                    item = QTableWidgetItem(f"{value:.4f}")
                else:
                    item = QTableWidgetItem("" if value is None else str(value))
                if column == 0 or column == len(headers) - 1:
                    # The key and the orienting tag are derived, not editable.
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(row, column, item)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)

    def _apply_position(self, data):
        pseudos = data.get("pseudos") or {}
        reals = data.get("reals") or {}
        if reals:
            self._last_reals = dict(reals)
            self.model3d.set_current(reals)
        if pseudos:
            self._current["hkl"].setText(
                "  ".join(f"{k}={v:.5f}" for k, v in pseudos.items() if v is not None)
            )
        if reals:
            self._set_angles(reals)
        two_theta = data.get("two_theta")
        self._current["two_theta"].setText(
            f"{two_theta:.5f}" if two_theta is not None else PLACEHOLDER
        )
        psi = data.get("psi")
        self._current["psi"].setText(f"{psi:.5f}" if psi is not None else PLACEHOLDER)
        reference = data.get("psi_reference") or {}
        if reference:
            self._current["psi_reference"].setText(
                " ".join(f"{reference.get(n, 0):g}" for n in ("h2", "k2", "l2"))
            )
        wavelength, energy = data.get("wavelength"), data.get("energy")
        if wavelength is not None and energy is not None:
            self._current["beam"].setText(f"{wavelength:.5f} Å ({energy:.5f} keV)")

    def _apply_calculation(self, result):
        if not isinstance(result, dict):
            return
        if result.get("error"):
            self._calculated = None
            self._calc_result.setText(f"No solution: {result['error']}")
        else:
            reals = result.get("reals") or {}
            self._calculated = reals
            self.model3d.set_calculated(reals)
            self._calc_result.setText(
                "  ".join(f"{k}={v:.4f}" for k, v in reals.items())
            )
        self._update_enabled()

    def _update_banner(self):
        if self.device == "psic":
            self._device_banner.setText("real motors")
            self._device_banner.setStyleSheet("color: #b00; font-weight: bold;")
        else:
            self._device_banner.setText("simulated")
            self._device_banner.setStyleSheet("color: #060; font-weight: bold;")

    def _update_enabled(self):
        editable = self._kernel_idle and bool(self._state)
        for widget in (
            self._add_sample_button,
            self._remove_sample_button,
            self._apply_lattice_button,
            self._add_here_button,
            self._remove_reflection_button,
            self._first_button,
            self._second_button,
            self._ub_button,
            self._calc_button,
            self._mode_combo,
            self._sample_combo,
            self._reflection_table,
            *self._ref_boxes.values(),
            *(w for row in self._preset_rows.values() for w in row[:2]),
            *(row[1] for row in self._extra_rows.values()),
            self._save_config_button,
            self._config_refresh_button,
            self._config_table,
            self._file_radio,
            self._scan_radio,
            self._replace_radio,
            self._merge_radio,
        ):
            widget.setEnabled(editable)
        # Move only after a successful calculation, so the confirmation can
        # only ever show angles the solver actually returned.
        self._move_button.setEnabled(editable and bool(self._calculated))

        # Browse reaches a file, so it means nothing against the scan source.
        self._browse_config_button.setEnabled(
            editable and self._config_source() == "file"
        )
        # Say why Load is off in its tooltip rather than leaving it to be
        # discovered by pressing it.
        selected = self._selected_config() is not None
        self._load_config_button.setEnabled(editable and selected)
        if not self._kernel_idle:
            reason = "Wait until the kernel is idle."
        elif not selected:
            reason = "Select a row in the list above first."
        else:
            reason = "Restore the selected configuration into this session."
        self._load_config_button.setToolTip(reason)

    # -- actions ----------------------------------------------------------

    def _on_device_changed(self, _index):
        self._state = {}
        self._calculated = None
        self._update_banner()
        self._status.setText(f"Switched to {self.device}.")
        self.refresh()
        # The scan listing is summarised for a particular diffractometer, so
        # it is answering the wrong question now.
        self._configs_listed = False
        self._clear_config_table()

    def _on_sample_selected(self, _index):
        if self._loading or not self._state:
            return
        name = self._sample_combo.currentText()
        if name and name != self._state.get("sample"):
            self._act(f"_gui_hkl_select_sample({self.device!r}, {name!r})")

    def _on_mode_selected(self, _index):
        if self._loading or not self._state:
            return
        mode = self._mode_combo.currentText()
        if mode and mode != self._state.get("mode"):
            self._act(f"_gui_hkl_set_mode({self.device!r}, {mode!r})")

    def _add_sample(self):
        name = self._sample_name.text().strip()
        if not name:
            self._status.setText("Enter a name for the new sample.")
            return
        values = {n: box.value() for n, box in self._lattice_boxes.items()}
        self._act(
            "_gui_hkl_add_sample("
            f"{self.device!r}, {name!r}, {values['a']!r}, {values['b']!r}, "
            f"{values['c']!r}, {values['alpha']!r}, {values['beta']!r}, "
            f"{values['gamma']!r})"
        )
        self._sample_name.clear()

    def _remove_sample(self):
        name = self._sample_combo.currentText()
        if not name:
            return
        self._act(f"_gui_hkl_remove_sample({self.device!r}, {name!r})")

    def _apply_lattice(self):
        values = {
            name: self._lattice_boxes[name].value()
            for name in (self._lattice_names or list(self._lattice_boxes))
            if name in self._lattice_boxes
        }
        self._act(f"_gui_hkl_set_lattice({self.device!r}, {values!r})")

    def _selected_key(self):
        row = self._reflection_table.currentRow()
        if row < 0:
            return None
        item = self._reflection_table.item(row, 0)
        return item.text() if item is not None else None

    def _add_reflection_here(self):
        pseudos = {n: self._calc_boxes[n].value() for n in ("h", "k", "l")}
        self._act(f"_gui_hkl_add_reflection({self.device!r}, {pseudos!r}, None)")

    def _remove_reflection(self):
        key = self._selected_key()
        if key is None:
            self._status.setText("Select a reflection row first.")
            return
        self._act(f"_gui_hkl_remove_reflection({self.device!r}, {key!r})")

    def _set_orienting(self, slot):
        key = self._selected_key()
        if key is None:
            self._status.setText("Select a reflection row first.")
            return
        order = list((self._state or {}).get("order") or [])
        first = key if slot == 0 else (order[0] if order else None)
        second = key if slot == 1 else (order[1] if len(order) > 1 else None)
        if first is None or second is None:
            self._status.setText(
                "Both orienting reflections are needed; set the other one too."
            )
            return
        self._act(f"_gui_hkl_set_orienting({self.device!r}, {first!r}, {second!r})")

    def _compute_ub(self):
        self._act(f"_gui_hkl_compute_ub({self.device!r})")

    def _on_reflection_edited(self, item):
        if self._loading or not self._state:
            return
        row = item.row()
        key_item = self._reflection_table.item(row, 0)
        if key_item is None:
            return
        key = key_item.text()
        pseudos, reals = {}, {}
        for column in range(1, self._reflection_table.columnCount() - 1):
            header = self._reflection_table.horizontalHeaderItem(column)
            cell = self._reflection_table.item(row, column)
            if header is None or cell is None:
                continue
            try:
                value = float(cell.text())
            except ValueError:
                self._status.setText(f"'{cell.text()}' is not a number.")
                return
            name = header.text()
            if name in ("h", "k", "l"):
                pseudos[name] = value
            else:
                reals[name] = value
        self._act(
            f"_gui_hkl_edit_reflection({self.device!r}, {key!r}, "
            f"{pseudos!r}, {reals!r})"
        )

    def _on_preset_changed(self, _value):
        """Queue a fixed-angle write after the user stops editing."""
        if self._loading or not self._state:
            return
        self._presets_timer.start()

    def _apply_presets(self):
        """Write the fixed angles and mode extras (fired by the timer)."""
        if not self._state:
            return
        self._act(
            f"_gui_hkl_set_fixed({self.device!r}, {self._preset_values()!r}, "
            f"{self._extra_values()!r})"
        )

    def _on_reference_changed(self, _value):
        """Queue a psi-reference write after the user stops editing."""
        if self._loading or not self._state:
            return
        self._reference_timer.start()

    def _apply_reference(self):
        """Write the psi reference vector (fired by the debounce timer)."""
        if not self._state:
            return
        values = {n: self._ref_boxes[n].value() for n in ("h2", "k2", "l2")}
        self._act(
            f"_gui_hkl_set_azimuth({self.device!r}, {values['h2']!r}, "
            f"{values['k2']!r}, {values['l2']!r})"
        )

    def _calculate(self):
        h = self._calc_boxes["h"].value()
        k = self._calc_boxes["k"].value()
        pos_l = self._calc_boxes["l"].value()
        psi = self._extra_values().get("psi")
        # The fixed angles go with the request rather than being left to the
        # debounce timer, so Calculate pressed straight after typing cannot
        # solve against the previous value.
        self.request(
            {
                CALC_KEY: (
                    f"_gui_hkl_calc({self.device!r}, {h!r}, {k!r}, {pos_l!r}, "
                    f"{psi!r}, {self._preset_values()!r})"
                )
            }
        )
        self._calc_result.setText("Calculating…")

    def _move(self):
        if not self._calculated:
            return
        h = self._calc_boxes["h"].value()
        k = self._calc_boxes["k"].value()
        pos_l = self._calc_boxes["l"].value()
        angles = "\n".join(f"    {k2} = {v:.4f}" for k2, v in self._calculated.items())
        warning = "REAL MOTORS WILL MOVE.\n\n" if self.device == "psic" else ""
        # A fixed angle decides which of many solutions this is, so it belongs
        # in the box that asks whether to go there.  ψ decides it just as much,
        # so the mode's extras are listed with the presets.
        fixed = dict(self._preset_values())
        fixed.update(self._extra_values())
        fixed_text = (
            "\nFixed: " + ", ".join(f"{a}={v:g}" for a, v in fixed.items()) + "\n"
            if fixed
            else ""
        )
        answer = QMessageBox.question(
            self,
            "Move diffractometer",
            f"{warning}Move {self.device} to\n\n"
            f"    H K L = {h:g} {k:g} {pos_l:g}\n{fixed_text}\n{angles}\n\n"
            "The command runs in the console and can be interrupted there.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._status.setText("Move cancelled.")
            return
        code = (
            f"RE(bps.mv({self.device}.h, {h!r}, "
            f"{self.device}.k, {k!r}, {self.device}.l, {pos_l!r}))"
        )
        if self.run_in_console(code):
            self._status.setText(f"Move sent to the console: {code}")
            self._start_progress()
        else:
            self._status.setText("No console available to run the move.")

    # -- move progress ----------------------------------------------------

    def _start_progress(self):
        """Begin following the move over Channel Access."""
        self._move_target = dict(self._calculated or {})
        self._move_start = dict(self._last_reals)
        self._move_pvs = dict((self._state or {}).get("real_pvs") or {})
        self._move_started_at = time.monotonic()
        self._progress.setVisible(True)
        if self._trackable_axes():
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
            self._progress.setFormat("moving… %p%")
        else:
            # Soft axes (psic_sim) have no PVs to watch, so all we honestly
            # know is that the move is running.
            self._progress.setRange(0, 0)
            self._progress.setFormat("moving…")
        self._move_timer.start()

    def _trackable_axes(self):
        """Axes with a readback PV and a non-trivial distance to travel."""
        axes = []
        for axis, target in (self._move_target or {}).items():
            start = (self._move_start or {}).get(axis)
            pv_name = self._move_pvs.get(axis)
            if start is None or not pv_name:
                continue
            if abs(target - start) < 1e-4:
                continue
            axes.append((axis, pv_name, start, target))
        return axes

    def _update_progress(self):
        elapsed = time.monotonic() - self._move_started_at
        # kernel_state_changed only fires on transitions, and a quick move can
        # start and finish inside one poll interval -- so completion is decided
        # from the *current* state, after a grace period long enough for the
        # kernel to have gone busy in the first place.
        if elapsed > MOVE_GRACE_S and self.poller is not None:
            if self.poller.kernel_state != "busy":
                self._finish_progress()
                return
        if elapsed > MOVE_TIMEOUT_S:
            self._move_timer.stop()
            self._progress.setRange(0, 100)
            self._progress.setFormat("move status unknown")
            return

        axes = self._trackable_axes()
        if not axes:
            return
        try:
            from epics import PV
        except Exception:  # noqa: BLE001 - no CA available in this process
            self._move_timer.stop()
            self._progress.setRange(0, 0)
            return
        if not hasattr(self, "_pv_cache"):
            self._pv_cache = {}

        fractions = []
        for _axis, pv_name, start, target in axes:
            pv = self._pv_cache.get(pv_name)
            if pv is None:
                pv = self._pv_cache[pv_name] = PV(pv_name, auto_monitor=True)
            value = pv.get(timeout=0.2)
            if value is None:
                continue
            fraction = abs(float(value) - start) / abs(target - start)
            fractions.append(max(0.0, min(1.0, fraction)))
        if fractions:
            self._progress.setValue(int(100 * sum(fractions) / len(fractions)))

    def _finish_progress(self):
        """Called when the kernel goes idle again: the move is over."""
        if not self._move_timer.isActive():
            return
        self._move_timer.stop()
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._progress.setFormat("move complete")
        # Leave the finished bar up briefly so it is not just a flicker.
        QTimer.singleShot(2500, lambda: self._progress.setVisible(False))

    # -- configuration files ----------------------------------------------

    def _config_source(self):
        """Which of the two load sources is selected."""
        return "file" if self._file_radio.isChecked() else "scan"

    def _on_config_source(self, _button, checked):
        """A source radio changed.  Relist against the new one."""
        # buttonToggled fires twice -- once for the button losing the check,
        # once for the one gaining it.
        if not checked:
            return
        self._clear_config_table()
        self._configs_listed = False
        self._refresh_configs()
        self._update_enabled()

    def _clear_config_table(self):
        self._config_rows = []
        self._config_table.setRowCount(0)

    def _refresh_configs(self):
        """Ask the kernel for the listing the selected source needs."""
        if self.poller is None or not self._kernel_idle:
            return
        # Set before the reply arrives: the flag means "a listing has been
        # asked for", which is what stops the first-listing branch in
        # on_kernel_values firing again on every poll.
        self._configs_listed = True
        if self._config_source() == "file":
            self._config_status.setText("Listing configuration files…")
            self.request({CONFIG_FILES_KEY: "_gui_hklconf_files()"})
        else:
            self._config_status.setText("Reading the catalog…")
            self.request(
                {
                    CONFIG_SCANS_KEY: (
                        f"_gui_hklconf_scans({self.device!r}, {CONFIG_SCAN_LIMIT})"
                    )
                }
            )

    def _config_summary(self, row):
        """One cell describing what a configuration holds."""
        if row.get("error"):
            return f"! {row['error']}"
        sample = row.get("sample") or "(no sample)"
        text = f"{sample} — {row.get('reflections') or 0} reflection(s)"
        samples = row.get("samples") or 0
        if samples > 1:
            text += f", {samples} samples"
        return text

    def _fill_config_table(self, headers, rows, tooltips):
        self._config_table.setColumnCount(len(headers))
        self._config_table.setHorizontalHeaderLabels(headers)
        self._config_table.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                item.setToolTip(tooltips[r])
                self._config_table.setItem(r, c, item)
        header = self._config_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._update_enabled()

    def _show_config_files(self, payload):
        if not isinstance(payload, dict) or self._config_source() != "file":
            return  # a reply that arrived after the source changed
        where = payload.get("directory") or self._kernel_cwd
        self._kernel_cwd = where or self._kernel_cwd
        rows = [r for r in (payload.get("files") or []) if isinstance(r, dict)]
        self._config_rows = rows
        self._fill_config_table(
            ["File", "Contents", "Saved"],
            [
                (r.get("name", ""), self._config_summary(r), r.get("when", ""))
                for r in rows
            ],
            [str(r.get("path", "")) for r in rows],
        )
        if payload.get("error"):
            self._config_status.setText(f"! {payload['error']}")
        elif not rows:
            self._config_status.setText(
                f"No *{CONFIG_SUFFIX} files in {where or 'the working directory'}. "
                "Use Browse… to reach one elsewhere."
            )
        else:
            self._config_status.setText(f"{len(rows)} file(s) in {where}.")

    def _show_config_scans(self, payload):
        if not isinstance(payload, dict) or self._config_source() != "scan":
            return
        rows = [r for r in (payload.get("scans") or []) if isinstance(r, dict)]
        self._config_rows = rows
        cells = []
        tips = []
        for row in rows:
            uid = str(row.get("uid", ""))
            summary = self._config_summary(row)
            if not row.get("matches", True):
                # The run holds the orientation under another name.  psic and
                # psic_sim are the same geometry on different motors, so it
                # still restores; say which one it came from.
                summary += f"  [{row.get('device', '?')}]"
            cells.append(
                (f"#{row.get('scan_id', '?')}  {uid[:8]}", summary, row.get("when", ""))
            )
            tips.append(f"{uid}\n{row.get('plan', '')}")
        self._fill_config_table(["Scan", "Contents", "When"], cells, tips)
        if payload.get("error"):
            self._config_status.setText(f"! {payload['error']}")
        elif not rows:
            self._config_status.setText(
                f"None of the newest {CONFIG_SCAN_LIMIT} runs carries an "
                "hklpy2 orientation."
            )
        else:
            # The uid caveat is on the source radio's tooltip rather than
            # here: it is the same on every listing, and it is read before
            # the source is chosen, not after.
            self._config_status.setText(
                f"{len(rows)} of the newest {CONFIG_SCAN_LIMIT} runs carry an "
                "orientation, listed by uid."
            )

    def _selected_config(self):
        """The row dict behind the current selection, or None."""
        index = self._config_table.currentRow()
        if index < 0 or index >= len(self._config_rows):
            return None
        item = self._config_table.item(index, 0)
        if item is None or not item.isSelected():
            return None
        return self._config_rows[index]

    # -- configuration file dialogs ---------------------------------------

    def _config_dir(self):
        """Where a file dialog should open."""
        remembered = QSettings(SETTINGS_ORG, SETTINGS_APP).value(
            CONFIG_DIR_KEY, "", type=str
        )
        if remembered and Path(remembered).is_dir():
            return remembered
        if self._kernel_cwd and Path(self._kernel_cwd).is_dir():
            return self._kernel_cwd
        return str(self.session_cwd or Path.cwd())

    def _remember_config_dir(self, path):
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(
            CONFIG_DIR_KEY, str(Path(path).parent)
        )

    def _save_config(self):
        """Write the current orientation to a file chosen in a dialog."""
        suggested = str(Path(self._config_dir()) / f"{self.device}{CONFIG_SUFFIX}")
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Save the diffractometer configuration",
            suggested,
            f"Configuration files (*{CONFIG_SUFFIX});;All files (*)",
        )
        if not path:
            return
        if not path.endswith(CONFIG_SUFFIX):
            # The console's write_diffractometer_config_file appends the
            # suffix to whatever base name it is given, and the reader lists
            # only files carrying it, so a file without it would be written
            # and then never offered back.  Qt already asked about
            # overwriting the name that was typed, not this one, so ask again.
            path += CONFIG_SUFFIX
            if Path(path).exists():
                answer = QMessageBox.question(
                    self,
                    "Overwrite?",
                    f"'{Path(path).name}' already exists.\n\nOverwrite it?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    self._config_status.setText("Save cancelled.")
                    return
        self._remember_config_dir(path)
        # Absolute, always: the kernel resolves a relative path against its
        # own live working directory, which is not necessarily this one.
        code = f"_gui_hklconf_save({self.device!r}, {str(Path(path).resolve())!r})"
        if self.run_in_console(code):
            self._config_status.setText(f"Saving: {code}")
            self._reload_timer.start()
        else:
            self._config_status.setText("No console available to save.")

    def _browse_config(self):
        """Load a configuration file from outside the working directory."""
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Load a diffractometer configuration",
            self._config_dir(),
            f"Configuration files (*{CONFIG_SUFFIX});;All files (*)",
        )
        if not path:
            return
        self._remember_config_dir(path)
        # Nothing has read this file, so there is no summary to show; the
        # confirmation names it and leaves it at that.
        self._confirm_and_load({"path": path, "name": Path(path).name}, "file")

    def _load_config(self):
        row = self._selected_config()
        if row is not None:
            self._confirm_and_load(row, self._config_source())

    def _confirm_and_load(self, row, source):
        """Confirm, then restore the configuration in *row* via the console."""
        clear = self._replace_radio.isChecked()
        # Not "REAL MOTORS WILL MOVE" -- nothing moves.  What it does change
        # is where every later move goes, which is worth its own warning.
        warning = "REAL DIFFRACTOMETER.\n\n" if self.device == "psic" else ""
        if source == "file":
            what = f"    {row.get('name') or row.get('path')}"
        else:
            what = f"    scan #{row.get('scan_id', '?')}  ({row.get('uid', '')})"
        summary = ""
        if row.get("sample"):
            summary = f"\n    {self._config_summary(row)}"
        how = (
            "Replace everything: the samples, reflections and UB in the "
            "session now are discarded."
            if clear
            else "Add to what is there: the current samples are kept, and a "
            "saved sample with the same name replaces its namesake."
        )
        answer = QMessageBox.question(
            self,
            "Load configuration",
            f"{warning}Load into {self.device} from\n\n"
            f"{what}{summary}\n\n"
            f"{how}\n\n"
            "UB is recomputed from the restored reflections rather than "
            "taken from the saved matrix.  The wavelength and the mode are "
            "left as they are.  The command runs in the console.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._config_status.setText("Load cancelled.")
            return
        if source == "file":
            target = str(Path(row["path"]).resolve())
            code = f"_gui_hklconf_load_file({self.device!r}, {target!r}, {clear!r})"
        else:
            code = (
                f"_gui_hklconf_load_scan({self.device!r}, "
                f"{str(row.get('uid', ''))!r}, {clear!r})"
            )
        if self.run_in_console(code):
            self._config_status.setText(f"Loading: {code}")
            self._reload_timer.start()
        else:
            self._config_status.setText("No console available to load.")

    def _reload_after_console(self):
        """Re-read the tab once the console command has finished."""
        if not self._kernel_idle:
            self._reload_timer.start()
            return
        self.refresh()
        self._configs_listed = False
        self._refresh_configs()
