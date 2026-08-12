"""Macro tab: build, edit, save and run a multi-step Bluesky plan.

An experiment is rarely one scan.  The usual shape is a loop -- set a
temperature, wait for it to settle, run a scan, move to the next temperature --
and writing that by hand means getting every ``yield from`` and every level of
indentation right.

A macro here is a **Bluesky plan**: one generator function, run as a single
``RE(macro())``, so Ctrl-C, ``RE.pause()`` and resume apply to the whole loop
rather than to whichever scan happens to be running::

    def temperature_series():
        for temperature in [100, 150, 200]:
            yield from mv(lakeshore340.setpoint, temperature)
            yield from bps.sleep(120)
            yield from ascan(psic.h, 0.9, 1.1, 51, 1.0)

Everything it calls is already in the session namespace: the ``local_scans``
plans (``startup.py:141``) and ``bps`` (``startup.py:110``).

Generation is **one way**.  The component chooser on the left writes code into
the editor on the right and never reads it back, so nothing typed by hand is
ever silently rewritten -- what is saved and run is exactly what is on screen.
"""

import ast
import logging
import re
import textwrap
from pathlib import Path

from qtpy.QtCore import QRegularExpression
from qtpy.QtCore import QSettings
from qtpy.QtGui import QColor
from qtpy.QtGui import QFont
from qtpy.QtGui import QFontDatabase
from qtpy.QtGui import QSyntaxHighlighter
from qtpy.QtGui import QTextCharFormat
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QFileDialog
from qtpy.QtWidgets import QGridLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLineEdit
from qtpy.QtWidgets import QMessageBox
from qtpy.QtWidgets import QPlainTextEdit
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QScrollArea
from qtpy.QtWidgets import QSplitter
from qtpy.QtWidgets import QStackedWidget
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from ..scancode import AXIS_ORDINALS
from ..scancode import MAX_TRAJECTORY_AXES
from ..scancode import PLANS
from ..scancode import active_axes
from ..scancode import axis_limit
from ..scancode import format_number
from ..scancode import format_scan_call
from ..scancode import plan_shape
from .base import BaseTab
from .base import value_label

logger = logging.getLogger(__name__)

#: Reply keys for the kernel helpers this tab uses.  ``scan_options`` and
#: ``device_table`` are the same expressions the Scan and Devices tabs ask for;
#: replies are broadcast to every tab, so asking costs nothing extra.
OPTIONS_KEY = "scan_options"
OPTIONS_EXPR = "_gui_scan_options()"
DEVICES_KEY = "device_table"
DEVICES_EXPR = "_gui_device_table()"
TARGETS_KEY = "macro_targets"

INDENT = "    "

#: Lines that an insert replaces rather than following.  This is what makes the
#: loop component usable: insert a loop, the cursor lands on its ``pass`` body,
#: and the next insert becomes the loop's first statement.  ``bps.null()`` is
#: the template's placeholder -- ``def f(): pass`` is not a generator, so an
#: empty macro needs a yield to be runnable at all.
PLACEHOLDERS = ("pass", "yield from bps.null()")

DEFAULT_MACRO_NAME = "my_macro"

TEMPLATE = '''\
"""Macro: {name}."""

import bluesky.plan_stubs as bps


def {name}():
    """Describe the macro here."""
    yield from bps.null()
'''

#: Where the last-used macro directory is remembered, alongside the console
#: font and background (see ``app.py``).
SETTINGS_ORG = "APS"
SETTINGS_APP = "id6b-gui"
MACRO_DIR_KEY = "macro/directory"

#: Generated lines longer than this are wrapped inside their brackets, so a
#: macro stays inside the project's 88-column limit.
WRAP_COLUMNS = 84

#: Refuse to expand a start/stop/steps loop beyond this many values -- past it
#: the literal list stops being something a person can read.
MAX_LOOP_VALUES = 200

#: Keeps the Scan component's fields from sprawling; the label columns get no
#: stretch, so a label stays next to the field it names.
VALUE_WIDTH = 110
AXIS_WIDTH = 200

_DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def _leading_space(text):
    return text[: len(text) - len(text.lstrip())]


def _quote(text):
    """Escape *text* for use inside a double-quoted f-string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _wrap_list(prefix, values, suffix, indent):
    """Return the lines for ``prefix[v, v, ...]suffix``, wrapped if long."""
    joined = ", ".join(values)
    single = f"{prefix}[{joined}]{suffix}"
    if len(indent) + len(single) <= WRAP_COLUMNS:
        return [single]
    lines = [f"{prefix}["]
    body, current = [], ""
    for value in values:
        piece = f"{value},"
        if current and len(current) + 1 + len(piece) > WRAP_COLUMNS - len(indent) - 4:
            body.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        body.append(current)
    lines += [INDENT + line for line in body]
    lines.append(f"]{suffix}")
    return lines


def _monospace(widget):
    widget.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    return widget


class PythonHighlighter(QSyntaxHighlighter):
    """Minimal Python highlighting: keywords, strings, comments, numbers."""

    KEYWORDS = (
        "and as assert async await break class continue def del elif else except "
        "finally for from global if import in is lambda nonlocal not or pass "
        "raise return try while with yield True False None"
    ).split()

    def __init__(self, document, dark=False):
        """Colour *document*, choosing shades that read on the current theme."""
        super().__init__(document)
        keyword = QColor("#8ab4ff") if dark else QColor("#0000c0")
        string = QColor("#98c379") if dark else QColor("#008000")
        comment = QColor("#9aa0a6") if dark else QColor("#808080")
        number = QColor("#d19a66") if dark else QColor("#a000a0")

        self._rules = []
        keyword_format = QTextCharFormat()
        keyword_format.setForeground(keyword)
        keyword_format.setFontWeight(QFont.Bold)
        for word in self.KEYWORDS:
            self._add(rf"\b{word}\b", keyword_format)

        number_format = QTextCharFormat()
        number_format.setForeground(number)
        self._add(r"\b\d+\.?\d*([eE][-+]?\d+)?\b", number_format)

        string_format = QTextCharFormat()
        string_format.setForeground(string)
        self._add(r'"[^"\\]*(\\.[^"\\]*)*"', string_format)
        self._add(r"'[^'\\]*(\\.[^'\\]*)*'", string_format)

        comment_format = QTextCharFormat()
        comment_format.setForeground(comment)
        comment_format.setFontItalic(True)
        self._add(r"#[^\n]*", comment_format)

    def _add(self, pattern, text_format):
        self._rules.append((QRegularExpression(pattern), text_format))

    def highlightBlock(self, text):  # noqa: N802 - Qt naming
        """Apply every rule to one line."""
        for expression, text_format in self._rules:
            match = expression.globalMatch(text)
            while match.hasNext():
                hit = match.next()
                self.setFormat(hit.capturedStart(), hit.capturedLength(), text_format)


class MacroEditor(QPlainTextEdit):
    """Plain text editor that never puts a literal tab into Python source."""

    def __init__(self, parent=None):
        """Build a monospace editor with soft tabs."""
        super().__init__(parent)
        _monospace(self)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(
            4 * self.fontMetrics().horizontalAdvance(" "),
        )

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        """Turn Tab into four spaces; a real tab would raise ``TabError``."""
        if event.key() == 0x01000001:  # Qt.Key_Tab
            self.insertPlainText(INDENT)
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------- components


class _Form(QWidget):
    """One component's form.  ``lines()`` returns the code it would insert."""

    #: Shown in the component chooser.
    label = "Component"

    def __init__(self, on_change, parent=None):
        """Report edits through *on_change* so the preview can update."""
        super().__init__(parent)
        self._changed = on_change

    def lines(self):
        """Return ``(lines, problem)``; *lines* is None when *problem* is set."""
        raise NotImplementedError

    def set_axes(self, paths):
        """Receive the scannable axis paths."""

    def set_devices(self, names):
        """Receive the root device names."""

    def _line(self, text="", placeholder=""):
        edit = QLineEdit(text)
        edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(lambda _t: self._changed())
        return edit


class LoopForm(_Form):
    """``for <var> in [...]:`` with a ``pass`` body."""

    label = "Loop"

    VALUES = "list of values"
    RANGE = "start / stop / steps"

    def __init__(self, on_change, parent=None):
        """Build the variable name and the two ways of giving its values."""
        super().__init__(on_change, parent)
        grid = QGridLayout(self)

        grid.addWidget(QLabel("Variable"), 0, 0)
        self._variable = self._line("temperature")
        grid.addWidget(self._variable, 0, 1, 1, 3)

        self._mode = QComboBox()
        self._mode.addItems([self.VALUES, self.RANGE])
        self._mode.currentIndexChanged.connect(self._on_mode)
        grid.addWidget(self._mode, 1, 0, 1, 4)

        self._values_label = QLabel("Values")
        self._values = self._line("100, 150, 200", "100, 150, 200")
        self._values.setToolTip(
            "Comma-separated values.  Text starting with [ or ( is used "
            "verbatim, so range(0, 10) and a list comprehension both work."
        )
        grid.addWidget(self._values_label, 2, 0)
        grid.addWidget(self._values, 2, 1, 1, 3)

        self._range_label = QLabel("Start / stop / steps")
        self._start = self._line("100")
        self._stop = self._line("200")
        self._steps = self._line("3")
        grid.addWidget(self._range_label, 3, 0)
        grid.addWidget(self._start, 3, 1)
        grid.addWidget(self._stop, 3, 2)
        grid.addWidget(self._steps, 3, 3)
        self._on_mode()

    def _on_mode(self, *_args):
        values = self._mode.currentText() == self.VALUES
        self._values_label.setVisible(values)
        self._values.setVisible(values)
        for widget in (self._range_label, self._start, self._stop, self._steps):
            widget.setVisible(not values)
        self._changed()

    def _expand_range(self):
        try:
            start = float(self._start.text())
            stop = float(self._stop.text())
            steps = int(self._steps.text())
        except ValueError:
            return None, "Start, stop and steps must be numbers."
        if steps < 1:
            return None, "Steps must be at least 1."
        if steps > MAX_LOOP_VALUES:
            return None, (
                f"More than {MAX_LOOP_VALUES} values -- write the loop with "
                "the Code component instead."
            )
        if steps == 1:
            points = [start]
        else:
            width = (stop - start) / (steps - 1)
            points = [start + index * width for index in range(steps)]
        return [format_number(point) for point in points], None

    def lines(self):
        """Return the ``for`` header plus an indented ``pass``."""
        variable = self._variable.text().strip()
        if not variable.isidentifier():
            return None, "The variable needs to be a valid Python name."

        if self._mode.currentText() == self.RANGE:
            values, problem = self._expand_range()
            if problem:
                return None, problem
        else:
            text = self._values.text().strip()
            if not text:
                return None, "Give the values to loop over."
            if text.startswith(("[", "(")) or "range(" in text:
                # Already an expression -- use it as written.
                return [f"for {variable} in {text}:", INDENT + "pass"], None
            values = [part.strip() for part in text.split(",") if part.strip()]
            if not values:
                return None, "Give the values to loop over."

        head = _wrap_list(f"for {variable} in ", values, ":", "")
        return head + [INDENT + "pass"], None


class SetForm(_Form):
    """``yield from mv(target, value)``."""

    label = "Set value"

    def __init__(self, on_change, on_device, parent=None):
        """Ask *on_device* to fetch the targets when the device changes."""
        super().__init__(on_change, parent)
        self._on_device = on_device
        self._requested = None
        grid = QGridLayout(self)

        grid.addWidget(QLabel("Device"), 0, 0)
        self._device = QComboBox()
        self._device.setEditable(True)
        self._device.setInsertPolicy(QComboBox.NoInsert)
        self._device.currentTextChanged.connect(self._request_targets)
        grid.addWidget(self._device, 0, 1)

        grid.addWidget(QLabel("Target"), 1, 0)
        self._target = QComboBox()
        self._target.setEditable(True)
        self._target.setInsertPolicy(QComboBox.NoInsert)
        self._target.currentTextChanged.connect(lambda _t: self._changed())
        grid.addWidget(self._target, 1, 1)

        grid.addWidget(QLabel("Value"), 2, 0)
        self._value = self._line("", "a number, or a loop variable")
        grid.addWidget(self._value, 2, 1)

        self._note = QLabel()
        self._note.setWordWrap(True)
        grid.addWidget(self._note, 3, 0, 1, 2)

    def set_devices(self, names):
        """Fill the device combo, keeping the current choice if still there."""
        current = self._device.currentText()
        blocked = self._device.blockSignals(True)
        self._device.clear()
        self._device.addItems(names)
        self._device.setCurrentText(current if current in names else "")
        self._device.blockSignals(blocked)
        if not current and names:
            self._device.setCurrentIndex(0)
        # A name chosen before the list arrived was not a known device then, so
        # its request was held back.  Now it is one, so ask.
        self._request_targets()

    def _request_targets(self, *_args):
        """Fetch the targets when the device becomes a name the kernel knows.

        Driven by ``currentTextChanged``, not ``currentIndexChanged``: the combo
        is editable, and ``setCurrentText`` on an editable combo only writes the
        line edit -- the index never moves, so an index signal misses both a
        typed name and a programmatic one.  Guarding on the name being in the
        list keeps the intermediate keystrokes from each firing a request.
        """
        name = self._device.currentText().strip()
        if name == self._requested:
            return
        if name and self._device.findText(name) < 0:
            return  # still being typed
        self._requested = name
        self._target.clear()
        if not name:
            self._note.clear()
            self._changed()
            return
        self._note.setText(f"Reading what can be set on {name}…")
        self._on_device(name)
        self._changed()

    def set_targets(self, rows):
        """Fill the target combo from ``_gui_macro_targets()`` rows."""
        current = self._target.currentText()
        paths = [row[0] for row in rows]
        self._target.clear()
        self._target.addItems(paths)
        self._target.setCurrentText(current if current in paths else "")
        movers = sum(1 for row in rows if row[2] == "positioner")
        if not paths:
            self._note.setText("Nothing settable found on this device.")
        else:
            self._note.setText(
                f"{movers} movable, {len(paths) - movers} writable signals."
            )
        if paths and not self._target.currentText():
            self._target.setCurrentIndex(0)
        self._changed()

    def lines(self):
        """Return the ``mv`` call."""
        target = self._target.currentText().strip()
        value = self._value.text().strip()
        if not target:
            return None, "Choose something to set."
        if not value:
            return None, "Give a value."
        return [f"yield from mv({target}, {value})"], None


class WaitForm(_Form):
    """``yield from bps.sleep(seconds)``."""

    label = "Wait"

    def __init__(self, on_change, parent=None):
        """Build the single seconds field."""
        super().__init__(on_change, parent)
        row = QHBoxLayout(self)
        row.addWidget(QLabel("Seconds"))
        self._seconds = self._line("60", "60, or an expression")
        row.addWidget(self._seconds)
        row.addStretch(1)

    def lines(self):
        """Return the sleep call."""
        seconds = self._seconds.text().strip()
        if not seconds:
            return None, "Give a number of seconds."
        return [f"yield from bps.sleep({seconds})"], None


class ScanRow:
    """One axis / start / stop / points row inside :class:`ScanForm`."""

    def __init__(self, form, grid, row, label):
        """Add the row's widgets to *grid* at *row*."""
        self.label = QLabel(label)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.setMinimumWidth(AXIS_WIDTH)
        self.combo.currentTextChanged.connect(lambda _t: form._changed())
        self.start = form._line("", "start")
        self.stop = form._line("", "stop")
        self.points = form._line("11", "points")
        for box in (self.start, self.stop, self.points):
            box.setMaximumWidth(VALUE_WIDTH)
        self.points_label = QLabel("points")
        widgets = (
            self.label,
            self.combo,
            self.start,
            self.stop,
            self.points_label,
            self.points,
        )
        for column, widget in enumerate(widgets):
            grid.addWidget(widget, row, column)
        self._widgets = widgets

    def set_visible(self, visible, with_points):
        """Show or hide the row, and its points field independently."""
        for widget in self._widgets:
            widget.setVisible(visible)
        self.points_label.setVisible(visible and with_points)
        self.points.setVisible(visible and with_points)

    def values(self):
        """Return the row as the string tuple ``format_scan_call`` wants."""
        return (
            self.combo.currentText().strip(),
            self.start.text().strip(),
            self.stop.text().strip(),
            self.points.text().strip(),
        )


class ScanForm(_Form):
    """``yield from ascan(...)`` and the other ``local_scans`` plans."""

    label = "Scan"

    def __init__(self, on_change, parent=None):
        """Build the plan selector, the two axis rows and the timing fields."""
        super().__init__(on_change, parent)
        outer = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Plan"))
        self._plan = QComboBox()
        self._plan.addItems(list(PLANS))
        self._plan.setCurrentText("ascan")
        self._plan.currentIndexChanged.connect(self._on_plan)
        top.addWidget(self._plan)
        top.addStretch(1)
        outer.addLayout(top)

        grid = QGridLayout()
        self._rows = []
        self._enables = []
        line = 0
        for index in range(MAX_TRAJECTORY_AXES):
            if index:
                enable = QCheckBox(AXIS_ORDINALS[index])
                enable.toggled.connect(self._on_plan)
                grid.addWidget(enable, line, 0, 1, 2)
                self._enables.append(enable)
                line += 1
            self._rows.append(ScanRow(self, grid, line, f"Axis {index + 1}"))
            line += 1
        # All-zero stretch spreads the leftover width across every column, so a
        # left-aligned label drifts away from its field.  Pin the label columns
        # and let the trailing one take the slack.
        for column in (0, 4):
            grid.setColumnStretch(column, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(6, 3)
        outer.addLayout(grid)
        self._axis1, self._axis2, self._axis3 = self._rows
        self._second, self._third = self._enables

        bottom = QGridLayout()
        self._shared_label = QLabel("Points")
        self._shared = self._line("51")
        self._shared.setMaximumWidth(VALUE_WIDTH)
        bottom.addWidget(self._shared_label, 0, 0)
        bottom.addWidget(self._shared, 0, 1)
        bottom.addWidget(QLabel("Time per point"), 0, 2)
        self._time = self._line("1.0", "seconds, or negative for monitor counts")
        self._time.setMaximumWidth(VALUE_WIDTH)
        bottom.addWidget(self._time, 0, 3)
        self._fixq = QCheckBox("Hold HKL fixed (fixq)")
        self._fixq.toggled.connect(lambda _c: self._changed())
        bottom.addWidget(self._fixq, 1, 0, 1, 4)
        for column in (0, 2):
            bottom.setColumnStretch(column, 0)
        bottom.setColumnStretch(4, 1)
        outer.addLayout(bottom)

        self._note = QLabel(
            "Fields are free text, so a loop variable works here too: "
            "start = centre - 0.1. A third axis is available for ascan and "
            "lup; the grid plans keep two, because the live image is 2D."
        )
        self._note.setWordWrap(True)
        outer.addWidget(self._note)
        self._on_plan()

    def set_axes(self, paths):
        """Fill every axis combo, keeping any current choice."""
        for row in self._rows:
            current = row.combo.currentText()
            row.combo.clear()
            row.combo.addItems(paths)
            row.combo.setCurrentText(current if current in paths else "")

    def _active(self):
        """How many axis rows are in play."""
        return active_axes(
            self._plan.currentText(), [e.isChecked() for e in self._enables]
        )

    def _on_plan(self, *_args):
        plan = self._plan.currentText()
        takes_axes, per_axis = plan_shape(plan)
        limit = axis_limit(plan)
        active = self._active()
        for index, row in enumerate(self._rows):
            row.set_visible(index < active, per_axis)
        for index, enable in enumerate(self._enables, start=1):
            # The box that adds row `index` appears once that row is allowed
            # and every row before it is already switched on.
            enable.setVisible(index < limit and index <= active)
        shared = not takes_axes or not per_axis
        self._shared_label.setText("Readings" if not takes_axes else "Points")
        self._shared_label.setVisible(shared)
        self._shared.setVisible(shared)
        self._fixq.setVisible(takes_axes)
        self._changed()

    def lines(self):
        """Return the plan call, as a ``yield from``."""
        plan = self._plan.currentText()
        rows = [row.values() for row in self._rows[: self._active()]]
        call, problem = format_scan_call(
            plan,
            rows,
            self._shared.text().strip(),
            self._time.text().strip(),
            fixq=self._fixq.isChecked(),
        )
        if problem:
            return None, problem
        return [f"yield from {call}"], None


class PrintForm(_Form):
    """``print(f"...")`` -- a progress note in the console."""

    label = "Print"

    def __init__(self, on_change, parent=None):
        """Build the message field."""
        super().__init__(on_change, parent)
        outer = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Message"))
        self._text = self._line("", "T = {temperature} K")
        row.addWidget(self._text)
        outer.addLayout(row)
        note = QLabel("An f-string: {braces} are filled in from the macro.")
        note.setWordWrap(True)
        outer.addWidget(note)

    def lines(self):
        """Return the print call."""
        text = self._text.text()
        if not text.strip():
            return None, "Give something to print."
        return [f'print(f"{_quote(text)}")'], None


class CodeForm(_Form):
    """Free text, inserted verbatim -- the escape hatch."""

    label = "Code"

    def __init__(self, on_change, parent=None):
        """Build the multi-line code box."""
        super().__init__(on_change, parent)
        outer = QVBoxLayout(self)
        self._text = _monospace(QPlainTextEdit())
        self._text.setPlaceholderText("# any Python; use yield from for plans")
        self._text.setMinimumHeight(80)
        self._text.textChanged.connect(lambda: self._changed())
        outer.addWidget(self._text)
        note = QLabel(
            "The only place the plan rule can be broken: a bare put() runs "
            "when the generator is built, not in sequence."
        )
        note.setWordWrap(True)
        outer.addWidget(note)

    def lines(self):
        """Return the typed lines, with their common indentation removed."""
        text = textwrap.dedent(self._text.toPlainText()).strip("\n")
        if not text.strip():
            return None, "Nothing to insert."
        return text.split("\n"), None


# ---------------------------------------------------------------------- tab


class MacroTab(BaseTab):
    """Compose a macro on the left, edit the code on the right."""

    title = "Macro"

    #: Owns its own layout: the editor must fill the pane, not scroll inside a
    #: page whose minimum height would pin the splitter open.
    scrollable = False

    def __init__(self, parent=None):
        """Build the component chooser, the editor and the file buttons."""
        super().__init__(parent)
        self._kernel_idle = False
        self._path = None
        self._dirty = False
        self._loaded_source = None

        splitter = QSplitter(self)
        splitter.addWidget(self._build_builder())
        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._new(confirm=False)
        self._update_preview()
        self._update_enabled()

    # -- construction -----------------------------------------------------

    def _build_builder(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        row = QHBoxLayout()
        row.addWidget(QLabel("Component"))
        self._component = QComboBox()
        row.addWidget(self._component, 1)
        outer.addLayout(row)

        # Assigned before the forms exist: their constructors set up widgets
        # whose signals already fire, and _update_preview reads this list.
        self._forms = []
        self._forms += [
            LoopForm(self._update_preview),
            SetForm(self._update_preview, self._fetch_targets),
            WaitForm(self._update_preview),
            ScanForm(self._update_preview),
            PrintForm(self._update_preview),
            CodeForm(self._update_preview),
        ]
        self._stack = QStackedWidget()
        for form in self._forms:
            self._component.addItem(form.label)
            self._stack.addWidget(form)
        self._component.currentIndexChanged.connect(self._on_component)
        outer.addWidget(self._stack)

        box = QGroupBox("Insert")
        inner = QVBoxLayout(box)
        self._preview = value_label()
        self._preview.setWordWrap(True)
        inner.addWidget(self._preview)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._insert_button = QPushButton("Insert")
        self._insert_button.clicked.connect(self._insert_component)
        buttons.addWidget(self._insert_button)
        inner.addLayout(buttons)
        outer.addWidget(box)
        outer.addStretch(1)

        holder = QScrollArea()
        holder.setWidget(page)
        holder.setWidgetResizable(True)
        holder.setFrameShape(QScrollArea.NoFrame)
        return holder

    def _build_editor(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        self._editor = MacroEditor()
        palette_is_dark = self._editor.palette().base().color().lightness() < 128
        self._highlighter = PythonHighlighter(self._editor.document(), palette_is_dark)
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.cursorPositionChanged.connect(self._update_position)
        outer.addWidget(self._editor, 1)

        files = QHBoxLayout()
        for text, slot in (
            ("New", self._new),
            ("Open…", self._open),
            ("Save", self._save),
            ("Save as…", self._save_as),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            files.addWidget(button)
        files.addStretch(1)
        self._load_button = QPushButton("Load")
        self._load_button.setToolTip(
            "Define the macro in the session.  Nothing is executed."
        )
        self._load_button.clicked.connect(self._load)
        files.addWidget(self._load_button)
        self._run_button = QPushButton("Run")
        self._run_button.setToolTip("RE(macro()) in the console.")
        self._run_button.clicked.connect(self._run)
        files.addWidget(self._run_button)
        outer.addLayout(files)

        bar = QHBoxLayout()
        self._position = QLabel()
        bar.addWidget(self._position)
        bar.addStretch(1)
        self._status = QLabel()
        bar.addWidget(self._status)
        outer.addLayout(bar)
        return page

    # -- kernel exchange --------------------------------------------------

    def refresh(self):
        """Re-read the axis and device lists."""
        self.request({OPTIONS_KEY: OPTIONS_EXPR, DEVICES_KEY: DEVICES_EXPR})

    def on_kernel_state(self, state):
        """Load and Run are only safe while the kernel is idle."""
        self._kernel_idle = state == "idle"
        if state == "idle":
            self.refresh()
        self._update_enabled()

    def on_kernel_values(self, values):
        """Fill the axis, device and target lists as replies arrive."""
        options = values.get(OPTIONS_KEY)
        if isinstance(options, dict):
            paths = [path for path, _class in options.get("axes", [])]
            for form in self._forms:
                form.set_axes(paths)
        rows = values.get(DEVICES_KEY)
        if isinstance(rows, list):
            names = [row[0] for row in rows]
            for form in self._forms:
                form.set_devices(names)
        targets = values.get(TARGETS_KEY)
        if isinstance(targets, list):
            for form in self._forms:
                if isinstance(form, SetForm):
                    form.set_targets(targets)

    def _fetch_targets(self, device):
        self.request({TARGETS_KEY: f"_gui_macro_targets({device!r})"})

    # -- component insertion ----------------------------------------------

    def _on_component(self, index):
        self._stack.setCurrentIndex(index)
        self._update_preview()

    def _current_form(self):
        index = self._component.currentIndex()
        if 0 <= index < len(self._forms):
            return self._forms[index]
        return None

    def _update_preview(self, *_args):
        form = self._current_form()
        if form is None or not hasattr(self, "_preview"):
            # Still building: a form's own widgets fire on construction.
            return
        lines, problem = form.lines()
        if problem:
            self._preview.setText(problem)
            self._insert_button.setEnabled(False)
            return
        self._preview.setText("\n".join(lines))
        self._insert_button.setEnabled(True)

    def _indent_at(self, block):
        """Return the indentation an insert on *block* should use.

        A blank line has no indentation of its own, so it takes the previous
        non-blank line's -- one level deeper if that line opens a block.  Qt
        does not auto-indent, so without this every Enter would drop an insert
        back to column zero.
        """
        text = block.text()
        if text.strip():
            return _leading_space(text)
        previous = block.previous()
        while previous.isValid() and not previous.text().strip():
            previous = previous.previous()
        if not previous.isValid():
            return ""
        text = previous.text()
        indent = _leading_space(text)
        if text.rstrip().endswith(":"):
            indent += INDENT
        return indent

    def _insert_component(self):
        form = self._current_form()
        if form is None:
            return
        lines, problem = form.lines()
        if problem:
            return
        self._insert(lines)
        self._editor.setFocus()

    def _insert(self, lines):
        """Write *lines* into the editor at the cursor, indented to match."""
        cursor = self._editor.textCursor()
        block = cursor.block()
        indent = self._indent_at(block)
        text = block.text()

        cursor.beginEditBlock()
        if not text.strip() or text.strip() in PLACEHOLDERS:
            # Replace the line: a placeholder is there to be filled in, and a
            # blank line is where the cursor already is.
            cursor.movePosition(QTextCursor.StartOfBlock)
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        else:
            cursor.movePosition(QTextCursor.EndOfBlock)
            cursor.insertText("\n")
        first = cursor.blockNumber()
        cursor.insertText("\n".join((indent + line) if line else "" for line in lines))
        cursor.endEditBlock()

        # Land on the new block's placeholder, so the next insert fills it.
        target = next(
            (
                first + offset
                for offset, line in enumerate(lines)
                if line.strip() in PLACEHOLDERS
            ),
            None,
        )
        if target is not None:
            block = self._editor.document().findBlockByNumber(target)
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.EndOfBlock)
        self._editor.setTextCursor(cursor)

    # -- editor state -----------------------------------------------------

    def _on_text_changed(self):
        self._dirty = True
        # Run may only ever call a definition the session actually has.
        if self._loaded_source is not None:
            if self._editor.toPlainText() != self._loaded_source:
                self._loaded_source = None
        self._update_position()
        self._update_enabled()

    def _update_position(self):
        cursor = self._editor.textCursor()
        name = self._path.name if self._path else "untitled"
        mark = " • modified" if self._dirty else ""
        self._position.setText(
            f"line {cursor.blockNumber() + 1}, col {cursor.positionInBlock() + 1}"
            f"      {name}{mark}"
        )

    def macro_name(self):
        """Return the name of the first top-level ``def``, or None."""
        match = _DEF_RE.search(self._editor.toPlainText())
        return match.group(1) if match else None

    def _set_text(self, text, path=None, dirty=False):
        self._editor.setPlainText(text)
        self._path = path
        self._dirty = dirty
        self._loaded_source = None
        self._update_position()
        self._update_enabled()

    def _confirm_discard(self):
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved macro",
            "The macro has unsaved changes.  Discard them?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Discard

    # -- files ------------------------------------------------------------

    def _macro_dir(self):
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        remembered = settings.value(MACRO_DIR_KEY, "", type=str)
        if remembered and Path(remembered).is_dir():
            return Path(remembered)
        return Path(self.session_cwd or Path.cwd()) / "macros"

    def _remember_dir(self, path):
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(
            MACRO_DIR_KEY, str(Path(path).parent)
        )

    def _new(self, confirm=True):
        """Start a fresh macro from the template."""
        if confirm and not self._confirm_discard():
            return
        self._set_text(TEMPLATE.format(name=DEFAULT_MACRO_NAME))
        self._goto_placeholder()
        self._status.setText("New macro.")

    def _goto_placeholder(self):
        document = self._editor.document()
        for number in range(document.blockCount()):
            block = document.findBlockByNumber(number)
            if block.text().strip() in PLACEHOLDERS:
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.EndOfBlock)
                self._editor.setTextCursor(cursor)
                return

    def _open(self):
        """Read a saved macro back into the editor."""
        if not self._confirm_discard():
            return
        directory = self._macro_dir()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open macro", str(directory), "Python files (*.py);;All files (*)"
        )
        if not path:
            return
        try:
            text = Path(path).read_text()
        except OSError as exc:
            self._status.setText(f"Could not open: {exc}")
            return
        self._set_text(text, path=Path(path))
        self._remember_dir(path)
        self._status.setText(f"Opened {path}.")

    def _save(self):
        """Save to the current file, asking for a name the first time."""
        if self._path is None:
            return self._save_as()
        return self._write(self._path)

    def _save_as(self):
        """Save under a new name."""
        directory = self._macro_dir()
        name = self.macro_name() or DEFAULT_MACRO_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("Could not create %s.", directory, exc_info=True)
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Save macro",
            str(directory / f"{name}.py"),
            "Python files (*.py);;All files (*)",
        )
        if not path:
            return False
        return self._write(Path(path))

    def _write(self, path):
        text = self._editor.toPlainText()
        if not text.endswith("\n"):
            text += "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError as exc:
            self._status.setText(f"Could not save: {exc}")
            return False
        self._path = path
        self._dirty = False
        self._remember_dir(path)
        self._update_position()
        self._status.setText(f"Saved {path}.")
        return True

    # -- load and run -------------------------------------------------------

    def _load(self):
        """Define the macro in the session, after checking it compiles."""
        source = self._editor.toPlainText()
        name = str(self._path) if self._path else "<macro>"
        try:
            compile(source, name, "exec")
        except SyntaxError as exc:
            self._status.setText(f"Line {exc.lineno}: {exc.msg}")
            self._goto_line(exc.lineno)
            return
        macro = self.macro_name()
        if macro is None:
            self._status.setText("No top-level def found -- nothing to load.")
            return
        if not self._is_generator(source, macro):
            self._status.setText(
                f"{macro}() has no yield, so RE({macro}()) will not run it. "
                "A macro must be a plan."
            )
            return
        if not self.run_in_console(source):
            self._status.setText("No console available.")
            return
        self._loaded_source = source
        self._status.setText(f"Loaded {macro}() into the session.")
        self._update_enabled()

    @staticmethod
    def _is_generator(source, name):
        """Return whether *name* is defined as a generator in *source*."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return any(
                    isinstance(child, (ast.Yield, ast.YieldFrom))
                    for child in ast.walk(node)
                )
        return False

    def _goto_line(self, number):
        if not number:
            return
        block = self._editor.document().findBlockByNumber(max(0, number - 1))
        if block.isValid():
            self._editor.setTextCursor(QTextCursor(block))

    def _run(self):
        """Run the loaded macro through the console."""
        macro = self.macro_name()
        if macro is None or self._loaded_source is None:
            return
        command = f"RE({macro}())"
        if self.run_in_console(command):
            self._status.setText(f"Running: {command}")
        else:
            self._status.setText("No console available.")

    def _update_enabled(self):
        self._load_button.setEnabled(self._kernel_idle)
        loaded = self._loaded_source is not None
        self._run_button.setEnabled(self._kernel_idle and loaded)
        if not loaded:
            self._run_button.setToolTip("Load the macro first.")
        else:
            self._run_button.setToolTip(f"RE({self.macro_name()}())")
