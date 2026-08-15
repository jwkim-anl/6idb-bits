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

from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QAbstractItemView
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QDoubleSpinBox
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

#: Give the kernel this long to report itself busy before treating "not busy"
#: as "the move finished".
MOVE_GRACE_S = 2.0
#: Stop following a move after this long rather than spinning forever.
MOVE_TIMEOUT_S = 600.0

LATTICE_RANGES = {
    "a": (0.01, 1000.0),
    "b": (0.01, 1000.0),
    "c": (0.01, 1000.0),
    "alpha": (0.01, 179.99),
    "beta": (0.01, 179.99),
    "gamma": (0.01, 179.99),
}


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

        body = QWidget()
        stack = QVBoxLayout(body)
        stack.addWidget(self._build_header())
        stack.addWidget(self._build_sample_group())
        stack.addWidget(self._build_reflection_group())
        stack.addWidget(self._build_mode_group())
        stack.addWidget(self._build_current_group())
        stack.addWidget(self._build_calc_group())
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
            spin = _spin(low, high, 1.0 if name in "abc" else 90.0)
            self._lattice_boxes[name] = spin
            grid.addWidget(QLabel(name), 0, column)
            grid.addWidget(spin, 1, column)
        self._apply_lattice_button = QPushButton("Apply lattice")
        self._apply_lattice_button.clicked.connect(self._apply_lattice)
        grid.addWidget(self._apply_lattice_button, 1, 6)
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
        self._preset_host = QWidget()
        self._preset_grid = QGridLayout(self._preset_host)
        self._preset_grid.setContentsMargins(0, 0, 0, 0)
        self._preset_rows = {}
        self._preset_axes = []
        form.addRow("Fixed angles:", self._preset_host)
        caption = QLabel(
            "Ticked axes are held at the value shown when solving; unticked "
            "axes follow the motor.  Presets change the calculation only — "
            "nothing moves."
        )
        caption.setWordWrap(True)
        form.addRow("", caption)

        self._presets_label = value_label()
        form.addRow("In effect:", self._presets_label)
        return box

    def _rebuild_preset_rows(self, axes):
        """Build one Fix checkbox + angle box per constant axis of the mode.

        Only rebuilt when the axis list itself changes: the values are
        refreshed on every state read, and tearing the widgets down each time
        would pull the box out from under whoever is typing in it.
        """
        axes = list(axes or [])
        if axes == self._preset_axes:
            return
        self._preset_axes = axes

        for check, spin, unit in self._preset_rows.values():
            for widget in (check, spin, unit):
                # setParent(None) as well as deleteLater(): a deferred delete
                # is not processed until the event loop comes round, and until
                # then the old widget still paints over its former cell.
                self._preset_grid.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._preset_rows = {}

        if not axes:
            return
        for row, axis in enumerate(axes):
            check = QCheckBox(axis)
            check.toggled.connect(self._on_preset_changed)
            spin = _spin(-360.0, 360.0, 0.0, decimals=4)
            spin.valueChanged.connect(self._on_preset_changed)
            unit = QLabel("deg")
            self._preset_grid.addWidget(check, row, 0)
            self._preset_grid.addWidget(spin, row, 1)
            self._preset_grid.addWidget(unit, row, 2)
            self._preset_rows[axis] = (check, spin, unit)
        self._preset_grid.setColumnStretch(3, 1)

    def _preset_values(self):
        """Return ``{axis: angle}`` for the ticked axes only."""
        return {
            axis: spin.value()
            for axis, (check, spin, _unit) in self._preset_rows.items()
            if check.isChecked()
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
            label = value_label()
            self._current[key] = label
            form.addRow(f"{caption}:", label)
        return box

    def _build_calc_group(self):
        box = QGroupBox("Calculate and move")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._calc_boxes = {}
        for name in ("h", "k", "l"):
            spin = _spin(-100.0, 100.0, 0.0, decimals=4)
            self._calc_boxes[name] = spin
            row.addWidget(QLabel(name))
            row.addWidget(spin)
        row.addWidget(QLabel("ψ"))
        self._psi_box = _spin(-360.0, 360.0, 0.0, decimals=4)
        row.addWidget(self._psi_box)
        self._calc_button = QPushButton("Calculate")
        self._calc_button.clicked.connect(self._calculate)
        row.addWidget(self._calc_button)
        self._move_button = QPushButton("Move")
        self._move_button.clicked.connect(self._move)
        self._move_button.setEnabled(False)
        row.addWidget(self._move_button)
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
        # Keep the cheap readout coming while this tab is on screen.
        if self.isVisible() and self._kernel_idle:
            self._request_position()

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
            self._rebuild_preset_rows(constant_axes)
            for axis, (check, spin, _unit) in self._preset_rows.items():
                value = presets.get(axis)
                check.setChecked(value is not None)
                if value is not None:
                    spin.setValue(value)
            self._presets_label.setText(
                ", ".join(f"{k}={v:g}" for k, v in presets.items())
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
            self._current["angles"].setText(
                "  ".join(f"{k}={v:.4f}" for k, v in reals.items() if v is not None)
            )
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
        ):
            widget.setEnabled(editable)
        # Move only after a successful calculation, so the confirmation can
        # only ever show angles the solver actually returned.
        self._move_button.setEnabled(editable and bool(self._calculated))
        mode = (self._state or {}).get("mode", "")
        self._psi_box.setEnabled(editable and "psi_constant" in mode)

    # -- actions ----------------------------------------------------------

    def _on_device_changed(self, _index):
        self._state = {}
        self._calculated = None
        self._update_banner()
        self._status.setText(f"Switched to {self.device}.")
        self.refresh()

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
        """Write the fixed angles (fired by the debounce timer)."""
        if not self._state:
            return
        self._act(f"_gui_hkl_set_presets({self.device!r}, {self._preset_values()!r})")

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
        psi = self._psi_box.value() if self._psi_box.isEnabled() else None
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
        # in the box that asks whether to go there.
        fixed = self._preset_values()
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
