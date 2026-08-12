"""Scan tab: build and run a local_scans plan from a form.

The tab composes the exact command it will run and shows it before you press
Scan, then executes it **in the console** via ``BaseTab.run_in_console()``.
That keeps a scan an ordinary Bluesky command: session RunEngine, visible in
history, drawn live by the Scan plot tab, and interruptible with Ctrl-C.

Argument order differs between the plans and is easy to get wrong by hand --
``ascan`` takes ``motor, start, stop, ..., num_points, time`` while
``grid_scan`` takes ``motor, start, stop, num, ..., time`` -- so the form
encodes the difference rather than leaving it to memory.
"""

import logging

from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QDoubleSpinBox
from qtpy.QtWidgets import QGridLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QSpinBox
from qtpy.QtWidgets import QVBoxLayout

from ..scancode import AXIS_ORDINALS
from ..scancode import MAX_TRAJECTORY_AXES
from ..scancode import PLANS
from ..scancode import active_axes
from ..scancode import axis_limit
from ..scancode import format_number
from ..scancode import format_scan_call
from ..scancode import plan_shape
from ..scancode import scan_arguments
from .base import BaseTab
from .base import value_label

logger = logging.getLogger(__name__)

OPTIONS_KEY = "scan_options"
OPTIONS_EXPR = "_gui_scan_options()"

SECONDS = "seconds"
MONITOR_COUNTS = "monitor counts"

#: Rough per-point overhead used only for the duration estimate.
OVERHEAD_S = 0.35

#: Keeps the numeric fields from sprawling when the window is wide; the label
#: columns get no stretch, so a label stays next to the field it names.
VALUE_WIDTH = 110
AXIS_WIDTH = 220


#: Format a float without trailing noise, for the command preview.
_num = format_number


class _AxisRow:
    """One motor / start / stop / points row."""

    def __init__(self, grid, row, label, on_change):
        self.label = QLabel(label)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.setMinimumWidth(AXIS_WIDTH)
        self.combo.currentTextChanged.connect(lambda _t: on_change())
        self.start = QDoubleSpinBox()
        self.stop = QDoubleSpinBox()
        for box in (self.start, self.stop):
            box.setRange(-1e6, 1e6)
            box.setDecimals(4)
            box.setKeyboardTracking(False)
            box.valueChanged.connect(lambda _v: on_change())
        self.points = QSpinBox()
        self.points.setRange(1, 1000000)
        self.points.setValue(11)
        self.points.setKeyboardTracking(False)
        self.points.valueChanged.connect(lambda _v: on_change())
        for box in (self.start, self.stop, self.points):
            box.setMaximumWidth(VALUE_WIDTH)

        self.start_label = QLabel("start")
        self.stop_label = QLabel("stop")
        self.points_label = QLabel("points")

        for column, widget in enumerate(
            (
                self.label,
                self.combo,
                self.start_label,
                self.start,
                self.stop_label,
                self.stop,
                self.points_label,
                self.points,
            )
        ):
            grid.addWidget(widget, row, column)

    def widgets(self):
        return (
            self.label,
            self.combo,
            self.start_label,
            self.start,
            self.stop_label,
            self.stop,
            self.points_label,
            self.points,
        )

    def set_visible(self, visible, with_points):
        for widget in self.widgets():
            widget.setVisible(visible)
        self.points_label.setVisible(visible and with_points)
        self.points.setVisible(visible and with_points)

    def set_enabled(self, enabled):
        for widget in self.widgets():
            widget.setEnabled(enabled)

    @property
    def axis(self):
        return self.combo.currentText().strip()


class ScanTab(BaseTab):
    """Compose a scan and run it in the console."""

    title = "Scan"

    def __init__(self, parent=None):
        """Build the plan selector, detector list, axis rows and preview."""
        super().__init__(parent)
        self._kernel_idle = False
        self._options = {}
        self._detector_boxes = {}

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_plan_row())
        layout.addWidget(self._build_detector_group())
        layout.addWidget(self._build_axis_group())
        layout.addWidget(self._build_count_group())
        layout.addWidget(self._build_command_group())
        layout.addStretch(1)

        self._status = QLabel("Waiting for the session…")
        layout.addWidget(self._status)
        self._on_plan_changed()

    # -- construction -----------------------------------------------------

    def _build_plan_row(self):
        box = QGroupBox("Plan")
        row = QHBoxLayout(box)
        self._plan_combo = QComboBox()
        self._plan_combo.addItems(list(PLANS))
        self._plan_combo.setCurrentText("ascan")
        self._plan_combo.currentIndexChanged.connect(self._on_plan_changed)
        row.addWidget(self._plan_combo)
        row.addStretch(1)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh)
        row.addWidget(self._refresh_button)
        return box

    def _build_detector_group(self):
        box = QGroupBox("Detectors")
        outer = QVBoxLayout(box)
        self._detector_row = QHBoxLayout()
        outer.addLayout(self._detector_row)
        self._detector_note = QLabel(
            "Only detectors with a preset_monitor can be scanned. Anything "
            "else — temperature, capacitance — goes in Detectors → extra "
            "devices."
        )
        self._detector_note.setWordWrap(True)
        outer.addWidget(self._detector_note)
        return box

    def _build_axis_group(self):
        box = QGroupBox("Axes")
        grid = QGridLayout(box)
        self._axis_rows = []
        self._axis_enables = []
        line = 0
        for index in range(MAX_TRAJECTORY_AXES):
            if index:
                enable = QCheckBox(AXIS_ORDINALS[index])
                enable.toggled.connect(self._on_plan_changed)
                grid.addWidget(enable, line, 0, 1, 2)
                self._axis_enables.append(enable)
                line += 1
            self._axis_rows.append(
                _AxisRow(grid, line, f"Axis {index + 1}", self._update_preview)
            )
            line += 1

        self._axis_note = QLabel(
            "A third axis is available for ascan and lup. The grid plans keep "
            "two, because the live image is 2D."
        )
        self._axis_note.setWordWrap(True)
        grid.addWidget(self._axis_note, line, 0, 1, 9)

        # Without this every column shares the leftover width equally, and a
        # left-aligned label then sits a long way from the field it names.
        for column in (0, 2, 4, 6):
            grid.setColumnStretch(column, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(8, 3)
        self._axis1, self._axis2, self._axis3 = self._axis_rows
        self._axis2_enable, self._axis3_enable = self._axis_enables
        return box

    def _build_count_group(self):
        box = QGroupBox("Counting")
        grid = QGridLayout(box)

        self._shared_points_label = QLabel("Points")
        self._shared_points = QSpinBox()
        self._shared_points.setRange(1, 1000000)
        self._shared_points.setValue(51)
        self._shared_points.setKeyboardTracking(False)
        self._shared_points.valueChanged.connect(self._update_preview)
        self._shared_points.setMaximumWidth(VALUE_WIDTH)
        grid.addWidget(self._shared_points_label, 0, 0)
        grid.addWidget(self._shared_points, 0, 1)

        grid.addWidget(QLabel("Time per point"), 0, 2)
        self._time = QDoubleSpinBox()
        self._time.setRange(0.001, 1e6)
        self._time.setDecimals(3)
        self._time.setValue(1.0)
        self._time.setKeyboardTracking(False)
        self._time.valueChanged.connect(self._update_preview)
        self._time.setMaximumWidth(VALUE_WIDTH)
        grid.addWidget(self._time, 0, 3)

        self._time_unit = QComboBox()
        self._time_unit.addItems([SECONDS, MONITOR_COUNTS])
        self._time_unit.setToolTip(
            "local_scans reads a negative time as 'count until the monitor "
            "channel reaches N counts'."
        )
        self._time_unit.currentIndexChanged.connect(self._on_unit_changed)
        grid.addWidget(self._time_unit, 0, 4)

        self._fixq = QCheckBox("Hold HKL fixed (fixq)")
        self._fixq.toggled.connect(self._update_preview)
        grid.addWidget(self._fixq, 1, 0, 1, 5)

        # Labels hug their fields; the trailing column absorbs the rest.
        for column in (0, 2):
            grid.setColumnStretch(column, 0)
        grid.setColumnStretch(5, 1)
        return box

    def _build_command_group(self):
        box = QGroupBox("Command")
        outer = QVBoxLayout(box)
        self._command_label = value_label()
        self._command_label.setWordWrap(True)
        outer.addWidget(self._command_label)
        self._estimate_label = QLabel()
        outer.addWidget(self._estimate_label)
        row = QHBoxLayout()
        row.addStretch(1)
        self._scan_button = QPushButton("Scan")
        self._scan_button.clicked.connect(self._run)
        row.addWidget(self._scan_button)
        outer.addLayout(row)
        return box

    # -- kernel exchange --------------------------------------------------

    def refresh(self):
        """Re-read the axis and detector lists."""
        self.request({OPTIONS_KEY: OPTIONS_EXPR})

    def on_kernel_state(self, state):
        """Nothing may be launched or edited while a plan is running."""
        self._kernel_idle = state == "idle"
        if state == "idle" and not self._options:
            self.refresh()
        if state == "dead":
            self._status.setText("Kernel is not running.")
        self._update_enabled()

    def on_kernel_values(self, values):
        """Populate axis combos and detector checkboxes."""
        options = values.get(OPTIONS_KEY)
        if not isinstance(options, dict):
            return
        self._options = options
        paths = [p for p, _ in options.get("axes", [])]
        for row in self._axis_rows:
            current = row.axis
            row.combo.clear()
            row.combo.addItems(paths)
            row.combo.setCurrentText(current if current in paths else "")

        while self._detector_row.count():
            item = self._detector_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._detector_boxes.clear()
        selected = set(options.get("detectors_selected") or [])
        for name in options.get("detector_candidates") or []:
            box = QCheckBox(name)
            box.setChecked(name in selected)
            box.toggled.connect(self._update_preview)
            self._detector_boxes[name] = box
            self._detector_row.addWidget(box)
        self._detector_row.addStretch(1)
        monitor = options.get("monitor") or "?"
        if selected:
            self._status.setText(
                f"{len(paths)} axes. Monitor: {monitor}. "
                f"counters detectors: {', '.join(sorted(selected))}."
            )
        else:
            self._status.setText(
                f"{len(paths)} axes. No detectors selected yet — run "
                "counters() in the console, or tick one above."
            )
        self._update_preview()

    def on_document(self, name, doc):
        """A finished run may have changed the counters selection."""
        if name == "stop":
            self.refresh()

    # -- form logic -------------------------------------------------------

    def _active_axes(self):
        """How many rows are in play: each extra one needs the one before it."""
        return active_axes(
            self._plan_combo.currentText(),
            [enable.isChecked() for enable in self._axis_enables],
        )

    def _on_plan_changed(self, *_args):
        plan = self._plan_combo.currentText()
        needs_axes, per_axis_points = plan_shape(plan)
        limit = axis_limit(plan)
        active = self._active_axes()

        for index, row in enumerate(self._axis_rows):
            row.set_visible(index < active, per_axis_points)
        for index, enable in enumerate(self._axis_enables, start=1):
            # The box that adds row `index` appears once that row is allowed
            # and every row before it is already switched on.
            enable.setVisible(index < limit and index <= active)
        self._axis_note.setVisible(needs_axes and limit < MAX_TRAJECTORY_AXES)

        # count() takes a number of readings, not a trajectory, so the shared
        # points box doubles as its `num`.
        self._shared_points_label.setText("Readings" if not needs_axes else "Points")
        shared = (not needs_axes) or (not per_axis_points)
        self._shared_points_label.setVisible(shared)
        self._shared_points.setVisible(shared)
        self._fixq.setVisible(needs_axes)
        self._update_preview()

    def _on_unit_changed(self, *_args):
        counts = self._time_unit.currentText() == MONITOR_COUNTS
        # Passing an explicit detectors= list makes the plan skip
        # _setup_detectors(), which is where the negative-time checks live.
        # So in monitor-counts mode the selection is left at the default.
        for box in self._detector_boxes.values():
            box.setEnabled(not counts)
        if counts:
            self._detector_note.setText(
                "Counting to monitor counts uses the counters selection, so "
                "that local_scans can validate the monitor and its scaler. "
                "Change it with counters() or the Detectors tab."
            )
        else:
            self._detector_note.setText(
                "Only detectors with a preset_monitor can be scanned. Anything "
                "else — temperature, capacitance — goes in Detectors → extra "
                "devices."
            )
        self._update_preview()

    def _selected_detectors(self):
        return [n for n, b in self._detector_boxes.items() if b.isChecked()]

    def _time_value(self):
        value = self._time.value()
        if self._time_unit.currentText() == MONITOR_COUNTS:
            return -value
        return value

    def _rows(self):
        """Return the (axis, start, stop, points) strings for the live rows."""
        return [
            (
                row.axis,
                _num(row.start.value()),
                _num(row.stop.value()),
                str(row.points.value()),
            )
            for row in self._axis_rows[: self._active_axes()]
        ]

    def _total_points(self):
        plan = self._plan_combo.currentText()
        needs_axes, per_axis_points = plan_shape(plan)
        if not needs_axes or not per_axis_points:
            return self._shared_points.value()
        total = 1
        for row in self._axis_rows[: self._active_axes()]:
            total *= row.points.value()
        return total

    def _update_preview(self, *_args):
        plan = self._plan_combo.currentText()
        rows = self._rows()
        shared_points = str(self._shared_points.value())
        time_text = _num(self._time_value())

        # Validate the positional arguments first, so an unfinished axis row is
        # reported before anything about detectors.
        _args_unused, problem = scan_arguments(plan, rows, shared_points, time_text)
        detectors = None
        if not problem:
            # Omit detectors= when the selection is unchanged: an explicit list
            # makes the plan skip _setup_detectors() and its validation.
            chosen = self._selected_detectors()
            default = list(self._options.get("detectors_selected") or [])
            if self._time_unit.currentText() != MONITOR_COUNTS and sorted(
                chosen
            ) != sorted(default):
                if not chosen:
                    problem = "Select at least one detector."
                else:
                    detectors = chosen

        call = None
        if not problem:
            call, problem = format_scan_call(
                plan,
                rows,
                shared_points,
                time_text,
                fixq=self._fixq.isChecked(),
                detectors=detectors,
            )
        if problem:
            self._command_label.setText("—")
            self._estimate_label.setText(problem)
            self._command = None
            self._update_enabled()
            return

        self._command = f"RE({call})"
        self._command_label.setText(self._command)

        points = self._total_points()
        if self._time_unit.currentText() == SECONDS:
            seconds = points * (self._time.value() + OVERHEAD_S)
            self._estimate_label.setText(
                f"{points} points, about {self._format_duration(seconds)}"
            )
        else:
            self._estimate_label.setText(f"{points} points, counting to monitor")
        self._update_enabled()

    @staticmethod
    def _format_duration(seconds):
        if seconds < 90:
            return f"{seconds:.0f} s"
        minutes, rest = divmod(int(seconds), 60)
        if minutes < 60:
            return f"{minutes} min {rest} s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h {minutes} min"

    def _update_enabled(self):
        ready = self._kernel_idle and bool(self._options)
        for widget in (
            self._plan_combo,
            self._shared_points,
            self._time,
            self._time_unit,
            self._fixq,
            *self._axis_enables,
        ):
            widget.setEnabled(ready)
        for row in self._axis_rows:
            row.set_enabled(ready)
        counts = self._time_unit.currentText() == MONITOR_COUNTS
        for box in self._detector_boxes.values():
            box.setEnabled(ready and not counts)
        self._scan_button.setEnabled(ready and bool(getattr(self, "_command", None)))

    # -- run --------------------------------------------------------------

    def _run(self):
        command = getattr(self, "_command", None)
        if not command:
            return
        # No confirmation: the command is on screen, and Ctrl-C in the console
        # aborts exactly as for a typed scan.
        if self.run_in_console(command):
            self._status.setText(f"Running: {command}")
        else:
            self._status.setText("No console available to run the scan.")
