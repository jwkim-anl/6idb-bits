"""Live scan plot, fed by documents streamed from the kernel.

Every plottable detector field in the run gets a curve and a check box, so a
scan with several detectors can be filtered down to the interesting ones.  The
fields hinted by the run -- which is what ``counters`` controls -- are ticked
by default, matching what BestEffortCallback draws inline in the console.

The selectors sit in a panel down the right-hand side of the canvas: a **Plot**
column of check boxes and a **Mon** column of radio buttons.  Picking a monitor
divides every curve by that field point by point, which is how a detector is
normalised against I0 -- the usual way to take the incident-flux drift of a
scan out of the data.

Data is retained **raw** for all fields regardless of tick state, so enabling a
curve part-way through a scan shows its full history, and changing the monitor
recomputes every curve from the values already recorded rather than only
affecting points from there on.

Under the plot is the **peak row**: ``cen``/``com``/``max``/``min``/``fwhm`` for
one chosen curve, a dotted vertical marker on the plot for each of the four that
has a position, and a button that moves the scanned axis onto each.  The
statistics come from
:func:`~id6_b.utils.peak_statistics.peak_statistics` fed the *plotted* arrays,
so a monitor selection is inherited for free -- what the markers show and the
buttons move to is the peak of the curve on screen, not of the raw detector.

**Pop out** moves the whole plot into a separate window, so it stays in view
while another tab is on top.  It is a *move*, not a copy: the canvas, the
series and the selectors are one widget that is reparented, so there is nothing
to keep in step and a window opened part-way through a scan already has the
points so far.  The tab shows a notice with a button to bring it back, as does
closing the window.
"""

import logging

import numpy
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from qtpy.QtCore import QSettings
from qtpy.QtCore import Qt
from qtpy.QtCore import Signal
from qtpy.QtWidgets import QButtonGroup
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QGridLayout
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QRadioButton
from qtpy.QtWidgets import QScrollArea
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from ...utils.peak_statistics import peak_statistics
from .base import BaseTab
from .base import value_label

logger = logging.getLogger(__name__)

#: Stream that BEC plots from.
PRIMARY = "primary"

#: Width of the selector panel.  Detector fields have long names
#: (``lambda250k_stats1_total``), so the panel scrolls rather than pushing the
#: canvas out of the way.
SELECTOR_WIDTH = 260

#: Where the pop-out window remembers its size and position between sessions.
#: A fourth local copy of the organisation/application pair, as in ``app.py``,
#: ``macro.py``, ``status.py`` and ``hkl.py`` -- importing them from ``app.py``
#: would be circular.
SETTINGS_ORG = "APS"
SETTINGS_APP = "id6b-gui"
GEOMETRY_KEY = "scanplot/window_geometry"

#: Fallback size of the pop-out window the first time it is opened.
WINDOW_SIZE = (900, 650)

#: Reuses the Scan tab's helper -- its reply is broadcast to every tab, so
#: asking for it here costs no extra round-trip.
OPTIONS_KEY = "scan_options"
OPTIONS_EXPR = "_gui_scan_options()"

#: The four peak statistics that have a position on the x axis, in the order
#: they are shown, each with the colour of its marker line.  The same colour is
#: used for the statistic's name in the peak row and for its "Go to" button,
#: which is how a line on the plot is identified -- deliberately *not* through
#: the matplotlib legend, which belongs to the curves.  These are picked away
#: from the default curve cycle, and the markers are dotted where curves are
#: solid, so a marker still reads as a marker if a curve lands on the same hue.
MARKER_STYLES = (
    ("cen", "#d62728"),
    ("com", "#1f9e5a"),
    ("max", "#7b3fb5"),
    ("min", "#c77f00"),
)

#: What each statistic means, for the buttons and the toggle.
MARKER_TOOLTIPS = {
    "cen": "Midpoint of the two half-maximum crossings — what BEC prints as cen.",
    "com": "Centre of mass: the intensity-weighted mean position.",
    "max": "Position of the largest plotted value.",
    "min": "Position of the smallest plotted value.",
}


def _format(value):
    """Format a position for display *and* for the generated command.

    ``%.10g`` because the two must be the same string: a button that says
    ``1.0234`` and then moves to ``1.0234000000000002`` is a button you cannot
    trust.  Ten significant digits keeps hkl pseudo-axis precision while
    dropping the float noise that ``repr`` would show.
    """
    return f"{float(value):.10g}"


class _PlotWindow(QWidget):
    """Top-level window holding the plot while it is detached from the tab.

    Parented to the session window with the ``Qt.Window`` flag rather than left
    parentless.  A parentless top-level widget keeps the Qt application alive,
    so closing the session window would leave the process running with an
    orphaned plot on screen; with a parent, Qt closes and destroys this along
    with it.
    """

    #: Emitted from :meth:`closeEvent`, so the tab takes the plot back rather
    #: than losing it with the window.
    closing = Signal()

    def __init__(self, parent=None):
        """Build an empty window with a layout for the plot to be moved into."""
        super().__init__(parent)
        self.setWindowFlag(Qt.Window)
        self.setWindowTitle("Scan plot — 6-ID-B")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

    def closeEvent(self, event):
        """Hand the plot back to the tab before the window goes away."""
        self.closing.emit()
        super().closeEvent(event)


class ScanPlotTab(BaseTab):
    """Plot the running scan, point by point, one curve per detector field."""

    title = "Scan plot"

    #: The canvas should fill the pane, not scroll inside it.
    scrollable = False

    def __init__(self, parent=None):
        """Build an empty canvas, toolbar and (initially empty) selector panel."""
        super().__init__(parent)

        self._figure = Figure(tight_layout=True)
        self._axes = self._figure.add_subplot(111)
        self._canvas = FigureCanvas(self._figure)

        self._status = QLabel("Waiting for a scan…")

        # Everything the plot is made of goes in one child widget, so popping
        # out is a single reparent rather than a rebuild: there is one canvas,
        # one set of series and one selector panel whether it is showing in the
        # tab or in its own window, which is what keeps a window opened
        # mid-scan from starting empty.
        self._body = QWidget()
        layout = QVBoxLayout(self._body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._build_toolbar_row())
        middle = QHBoxLayout()
        middle.addWidget(self._canvas, 1)
        middle.addWidget(self._build_selector_panel(), 0)
        layout.addLayout(middle, 1)
        layout.addWidget(self._build_peak_row())
        layout.addWidget(self._status)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._body, 1)
        outer.addWidget(self._build_detached_notice(), 1)

        self._window = None
        self._monitor_group = None
        self._axis_fields = {}
        self._kernel_idle = False
        self._scan_running = False
        self._reset_state()
        self._clear_selectors()
        self._draw_placeholder()
        self._update_peak()

    def _build_toolbar_row(self):
        """The matplotlib navigation toolbar, with the pop-out button after it."""
        self._detach_button = QPushButton("Pop out")
        self._detach_button.setToolTip(
            "Show the plot in its own window, so it stays visible while you "
            "work in another tab."
        )
        self._detach_button.clicked.connect(self._toggle_window)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(NavigationToolbar(self._canvas, self))
        row.addStretch(1)
        row.addWidget(self._detach_button)
        return row

    def _build_detached_notice(self):
        """What the tab shows in place of the plot while it is popped out."""
        label = QLabel("The scan plot is in its own window.")
        label.setAlignment(Qt.AlignCenter)

        button = QPushButton("Bring it back here")
        button.clicked.connect(self._reattach)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(button)
        button_row.addStretch(1)

        self._notice = QWidget()
        column = QVBoxLayout(self._notice)
        column.addStretch(1)
        column.addWidget(label)
        column.addLayout(button_row)
        column.addStretch(1)
        self._notice.setVisible(False)
        return self._notice

    # -- pop-out window ---------------------------------------------------

    def _toggle_window(self):
        """Pop the plot out into its own window, or put it back."""
        if self._window is None:
            self._detach()
        else:
            self._reattach()

    def _detach(self):
        """Move the plot into a separate window."""
        if self._window is not None:
            return
        # self.window() is resolved here rather than in __init__, where the tab
        # is not yet inside the session window and would give the wrong parent.
        window = _PlotWindow(self.window())
        window.closing.connect(self._reattach)
        window.layout().addWidget(self._body)

        geometry = QSettings(SETTINGS_ORG, SETTINGS_APP).value(GEOMETRY_KEY)
        if geometry is None or not window.restoreGeometry(geometry):
            window.resize(*WINDOW_SIZE)

        self._window = window
        self._detach_button.setText("Put it back")
        self._detach_button.setToolTip("Return the plot to the Scan plot tab.")
        self._notice.setVisible(True)
        window.show()
        window.raise_()

    def _reattach(self):
        """Move the plot back into the tab and drop the window."""
        window = self._window
        if window is None:
            return
        # Cleared first: closing the window calls straight back in here through
        # the closing signal, and this is what makes that second call a no-op.
        self._window = None
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(
            GEOMETRY_KEY, window.saveGeometry()
        )
        self.layout().insertWidget(0, self._body, 1)
        self._notice.setVisible(False)
        self._detach_button.setText("Pop out")
        self._detach_button.setToolTip(
            "Show the plot in its own window, so it stays visible while you "
            "work in another tab."
        )
        window.close()
        window.deleteLater()

    def _build_peak_row(self):
        """Peak statistics for one curve, the marker toggle and the four buttons."""
        self._peak_combo = QComboBox()
        self._peak_combo.setToolTip("Which curve to find the peak of.")
        # Real channel names are long ("lambda250k_stats1_total", "Ion Chamber
        # 2"); at the width Qt picks from the empty combo they all elide to the
        # same few characters, so there is no telling which curve is selected.
        self._peak_combo.setMinimumWidth(150)
        self._peak_combo.currentTextChanged.connect(lambda _text: self._update_peak())

        self._peak_values = value_label("")

        self._markers_box = QCheckBox("Markers")
        self._markers_box.setChecked(True)
        self._markers_box.setToolTip(
            "Draw a dotted vertical line on the plot at each statistic, in the "
            "colour its name is printed in."
        )
        self._markers_box.toggled.connect(lambda _state: self._update_peak())

        # Two lines, not one.  The five statistics at full precision plus four
        # buttons do not fit across the default 1100 px window, and a QHBoxLayout
        # does not wrap -- it clips the label instead, which silently ate "min"
        # and "fwhm".  Splitting gives the numbers the whole width and costs one
        # row of the canvas.
        values_row = QHBoxLayout()
        values_row.setContentsMargins(0, 0, 0, 0)
        self._peak_label = QLabel("Peak of")
        values_row.addWidget(self._peak_label)
        values_row.addWidget(self._peak_combo)
        values_row.addWidget(self._peak_values, 1)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.addWidget(self._markers_box)
        buttons_row.addSpacing(12)

        # One button per statistic, coloured to match its marker so the button,
        # the name in the peak row and the line on the plot are one thing.
        self._peak_buttons = {}
        for name, colour in MARKER_STYLES:
            button = QPushButton(f"Go to {name}")
            button.setStyleSheet(f"color: {colour};")
            button.clicked.connect(lambda _checked=False, n=name: self._go_to(n))
            self._peak_buttons[name] = button
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(values_row)
        column.addLayout(buttons_row)

        self._peak_row = QWidget()
        self._peak_row.setLayout(column)
        return self._peak_row

    def _build_selector_panel(self):
        """The Plot / Mon columns, down the right-hand side of the canvas."""
        self._selector_grid = QGridLayout()
        self._selector_grid.setHorizontalSpacing(10)
        self._selector_grid.setVerticalSpacing(2)
        # The field names live in the check box labels, so give that column the
        # width and keep the narrow radio column pinned beside it.
        self._selector_grid.setColumnStretch(0, 1)
        self._selector_grid.setColumnStretch(1, 0)

        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addLayout(self._selector_grid)
        inner.addStretch(1)

        self._selector_area = QScrollArea()
        self._selector_area.setWidget(holder)
        self._selector_area.setWidgetResizable(True)
        self._selector_area.setFixedWidth(SELECTOR_WIDTH)
        return self._selector_area

    # -- state ------------------------------------------------------------

    def _reset_state(self):
        self._descriptor_uid = None
        self._x_field = None
        self._x_data = []
        # field -> {"y": raw values, "line": Line2D|None, "box": QCheckBox,
        #           "radio": QRadioButton}
        self._series = {}
        self._monitor = None
        self._start_time = None
        self._use_elapsed_time = False
        # 2D (mesh) state, used when the run is a grid scan.
        self._grid = None  # {"slow","fast","shape","extents"}
        self._grid_field = None
        self._grid_data = None
        self._grid_cells = []
        self._image = None
        self._colorbar = None
        # Peak statistics of the field named in the peak combo, or None.
        self._peak_stats = None
        # statistic -> the axvline drawn for it.  Emptied here rather than
        # removed: _on_start clears the axes straight afterwards, which takes
        # the artists with it.
        self._marker_lines = {}

    def _clear_selectors(self):
        while self._selector_grid.count():
            item = self._selector_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) as well as deleteLater(): the deferred delete
                # is not processed until the event loop gets round to it, and
                # until then the old row is still a child of the panel and
                # paints on top of the new one at its former position.
                widget.setParent(None)
                widget.deleteLater()
        # A fresh exclusive group each run; the old one dies with its buttons.
        self._monitor_group = QButtonGroup(self)
        self._monitor_group.setExclusive(True)

        plot_header = QLabel("Plot")
        monitor_header = QLabel("Mon")
        monitor_header.setToolTip(
            "Divide every plotted curve by this field, point by point."
        )
        for column, header in enumerate((plot_header, monitor_header)):
            header.setStyleSheet("font-weight: bold;")
            self._selector_grid.addWidget(header, 0, column)

        # "no monitor" is a real member of the group, so choosing it is how the
        # division is switched off again.
        self._no_monitor = QRadioButton()
        self._no_monitor.setChecked(True)
        self._no_monitor.setToolTip("Plot the raw detector values.")
        self._monitor_group.addButton(self._no_monitor)
        self._no_monitor.toggled.connect(
            lambda checked: self._set_monitor(None) if checked else None
        )
        label = QLabel("no monitor")
        label.setStyleSheet("font-style: italic;")
        self._selector_grid.addWidget(label, 1, 0)
        self._selector_grid.addWidget(self._no_monitor, 1, 1, Qt.AlignCenter)

    # -- document handling ------------------------------------------------

    def on_document(self, name, doc):
        """Consume one Bluesky document."""
        handler = getattr(self, f"_on_{name}", None)
        if handler is None:
            return
        try:
            handler(doc)
        except Exception:  # noqa: BLE001 - a bad document must not kill the tab
            logger.exception("Failed to handle %r document.", name)

    def _on_start(self, doc):
        # Fresh axes per run.  The colourbar has to go before the state that
        # holds it is cleared, or it is orphaned -- it lives in its own axes,
        # so self._axes.clear() does not take it away.
        self._discard_colorbar()
        self._reset_state()
        self._clear_selectors()
        self._start_time = doc.get("time")

        hints = doc.get("hints") or {}
        dimensions = hints.get("dimensions") or []
        self._grid = self._grid_geometry(doc, hints, dimensions)

        if dimensions:
            fields, stream = dimensions[0]
            if stream == PRIMARY and fields:
                self._x_field = fields[0]
        if self._x_field in (None, "time"):
            # count() and friends have no scanned motor.
            self._x_field = "time"
            self._use_elapsed_time = True

        self._axes.clear()
        scan_id = doc.get("scan_id", "?")
        plan = doc.get("plan_name", "scan")
        self._axes.set_title(f"#{scan_id}  {plan}")
        if self._grid:
            # Match BestEffortCallback's LiveGrid: the inner (fast) axis runs
            # horizontally, the outer (slow) axis vertically.
            self._axes.set_xlabel(self._grid["fast"])
            self._axes.set_ylabel(self._grid["slow"])
        else:
            self._axes.set_xlabel(
                "time (s)" if self._use_elapsed_time else self._x_field
            )
            self._axes.grid(True, alpha=0.3)
        self._canvas.draw_idle()
        self._status.setText(f"Scan #{scan_id} ({plan}) running…")
        self._scan_running = True
        self._update_peak()

    @staticmethod
    def _grid_geometry(doc, hints, dimensions):
        """Return mesh geometry for a rectilinear grid scan, else None.

        ``grid_scan``/``rel_grid_scan`` set ``hints["gridding"]`` and carry
        ``shape``/``extents``; ``dimensions`` is ordered outer(slow) first.
        """
        if hints.get("gridding") != "rectilinear" or len(dimensions) < 2:
            return None
        shape = doc.get("shape")
        extents = doc.get("extents")
        if not shape or not extents or len(shape) < 2 or len(extents) < 2:
            return None
        try:
            slow = dimensions[0][0][0]
            fast = dimensions[1][0][0]
        except (IndexError, TypeError):
            return None
        return {
            "slow": slow,
            "fast": fast,
            "shape": (int(shape[0]), int(shape[1])),
            "extents": (list(extents[0]), list(extents[1])),
        }

    def _on_descriptor(self, doc):
        if doc.get("name") != PRIMARY:
            return
        self._descriptor_uid = doc.get("uid")
        if self._series:
            return
        hinted, plottable = self._candidate_fields(doc)
        for field in plottable:
            self._add_series(field, checked=field in hinted)
        if not plottable:
            self._status.setText("No numeric detector field to plot.")
        if self._grid:
            # An image can show one channel at a time, so the check boxes act
            # as a selector here rather than as independent curves.
            self._select_grid_field(
                next((f for f in plottable if f in hinted), None)
                or (plottable[0] if plottable else None)
            )
        self._update_peak()

    def _candidate_fields(self, descriptor):
        """Return (hinted fields, all plottable fields) for this descriptor.

        Hinted fields are what ``counters``/``select_plot`` marked, i.e. the
        user's own choice, so they are the ones ticked by default.
        """
        data_keys = descriptor.get("data_keys") or {}
        hints = descriptor.get("hints") or {}

        # The scanned axes themselves are never a y (or colour) channel.  For a
        # mesh that means both of them, not just the one on the x axis.
        axis_fields = {self._x_field}
        if self._grid:
            axis_fields |= {self._grid["slow"], self._grid["fast"]}

        hinted = []
        for obj_hints in hints.values():
            for field in obj_hints.get("fields", []):
                if field not in axis_fields and field in data_keys:
                    hinted.append(field)

        plottable = []
        for field, spec in data_keys.items():
            if field in axis_fields:
                continue
            # Scalars only: images and arrays have a non-empty shape.
            if spec.get("dtype") == "number" and not spec.get("shape"):
                plottable.append(field)

        # Keep hinted fields first and in hint order, then the rest.
        ordered = hinted + [f for f in sorted(plottable) if f not in hinted]
        return hinted, ordered

    def _add_series(self, field, checked):
        box = QCheckBox(field)
        box.setChecked(checked)
        box.setToolTip(field)
        box.toggled.connect(lambda state, f=field: self._on_toggle(f, state))

        radio = QRadioButton()
        radio.setToolTip(f"Divide the plotted curves by {field}.")
        self._monitor_group.addButton(radio)
        radio.toggled.connect(
            lambda checked, f=field: self._set_monitor(f) if checked else None
        )

        row = self._selector_grid.rowCount()
        self._selector_grid.addWidget(box, row, 0)
        self._selector_grid.addWidget(radio, row, 1, Qt.AlignCenter)
        self._series[field] = {"y": [], "line": None, "box": box, "radio": radio}

    # -- normalisation ------------------------------------------------------

    def _values(self, series):
        """Return the plotted values: raw, or divided by the monitor."""
        if self._monitor is None:
            return series["y"]
        monitor = self._series.get(self._monitor)
        if monitor is None:
            return series["y"]
        out = []
        # strict=False: both lists are appended once per event so they match,
        # and a truncated curve beats an exception mid-scan if they ever do not.
        for value, divisor in zip(series["y"], monitor["y"], strict=False):
            try:
                out.append(value / divisor)
            except (TypeError, ZeroDivisionError):
                # A dropped key or a monitor reading of zero: leave a gap
                # rather than a spike or an exception mid-scan.
                out.append(float("nan"))
        return out

    def _label(self, field):
        """The curve/colourbar label, which says so when it is a ratio."""
        return f"{field} / {self._monitor}" if self._monitor else field

    def _shown(self, series):
        """Whether a field is drawn.  The monitor itself never is -- it would
        be a flat line of ones."""
        return series["box"].isChecked() and series["box"].isEnabled()

    def _set_monitor(self, field):
        """Normalise every curve by *field*, or stop normalising when None."""
        if field == self._monitor:
            return
        self._monitor = field
        for name, series in self._series.items():
            series["box"].setEnabled(name != field)

        if self._grid:
            if self._grid_field == field:
                # The image cannot show the monitor divided by itself.
                self._select_grid_field(
                    next((n for n in self._series if n != field), None)
                )
            self._rebuild_grid()
            self._update_status()
            self._update_peak()
            return

        for name, series in self._series.items():
            line = series["line"]
            if line is None:
                continue
            line.set_data(self._x_data, self._values(series))
            line.set_label(self._label(name))
            line.set_visible(self._shown(series))
        self._rescale()
        self._update_status()
        self._update_peak()

    def _on_toggle(self, field, checked):
        series = self._series.get(field)
        if series is None:
            return
        if self._grid:
            if checked:
                self._select_grid_field(field)
            return
        if checked and series["line"] is None:
            self._create_line(field, series)
        elif series["line"] is not None:
            series["line"].set_visible(self._shown(series))
        self._rescale()
        self._update_status()
        self._update_peak()

    # -- 2D (mesh) rendering ----------------------------------------------

    def _select_grid_field(self, field):
        """Show *field* as the image, unticking the others."""
        if field is None:
            return
        for name, series in self._series.items():
            box = series["box"]
            blocked = box.blockSignals(True)
            box.setChecked(name == field)
            box.blockSignals(blocked)
        if field != self._grid_field:
            self._grid_field = field
            # Rebuild from the points already recorded, so switching channel
            # mid-scan redraws the whole mesh rather than starting it over.
            self._rebuild_grid()

    def _rebuild_grid(self):
        """Recompute the mesh from every point seen so far."""
        if self._grid is None or self._grid_field is None:
            return
        rows, columns = self._grid["shape"]
        self._grid_data = numpy.full((rows, columns), numpy.nan)
        series = self._series.get(self._grid_field)
        if series is not None:
            cells = zip(self._grid_cells, self._values(series), strict=False)
            for cell, value in cells:
                if cell is not None:
                    self._grid_data[cell] = value
        self._redraw_grid()

    def _grid_indices(self, data):
        """Map a point's motor positions to (row, column) in the mesh.

        Derived from the readback values and the run's declared extents rather
        than from the event order, so snaked scans and out-of-order points land
        correctly without having to model the trajectory.
        """
        rows, columns = self._grid["shape"]
        (slow0, slow1), (fast0, fast1) = self._grid["extents"]
        slow = data.get(self._grid["slow"])
        fast = data.get(self._grid["fast"])
        if slow is None or fast is None:
            return None

        def _index(value, low, high, count):
            if count <= 1 or high == low:
                return 0
            position = (float(value) - low) / (high - low) * (count - 1)
            return max(0, min(count - 1, int(round(position))))

        return _index(slow, slow0, slow1, rows), _index(fast, fast0, fast1, columns)

    def _accumulate_grid(self, cell):
        """Fill in the one cell this event landed in."""
        if self._grid_field is None or cell is None:
            return
        series = self._series.get(self._grid_field)
        if series is None or not series["y"]:
            return
        if self._grid_data is None:
            rows, columns = self._grid["shape"]
            self._grid_data = numpy.full((rows, columns), numpy.nan)
        self._grid_data[cell] = self._values(series)[-1]
        self._redraw_grid()

    def _discard_colorbar(self):
        if getattr(self, "_colorbar", None) is None:
            return
        try:
            self._colorbar.remove()
        except Exception:  # noqa: BLE001 - already gone with the axes
            logger.debug("Colorbar removal failed.", exc_info=True)
        self._colorbar = None

    def _redraw_grid(self):
        if self._grid is None or self._grid_data is None:
            return
        finite = numpy.isfinite(self._grid_data)
        if self._image is None and not finite.any():
            # imshow on an all-NaN array has nothing to autoscale to; wait for
            # the first real point.
            return
        (slow0, slow1), (fast0, fast1) = self._grid["extents"]
        if self._image is None:
            self._image = self._axes.imshow(
                self._grid_data,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=[fast0, fast1, slow0, slow1],
            )
            self._colorbar = self._figure.colorbar(self._image, ax=self._axes)
        else:
            self._image.set_data(self._grid_data)
        if finite.any():
            self._image.set_clim(
                float(numpy.nanmin(self._grid_data)),
                float(numpy.nanmax(self._grid_data)),
            )
        if self._colorbar is not None:
            self._colorbar.set_label(self._label(self._grid_field))
        self._canvas.draw_idle()

    def _create_line(self, field, series):
        (line,) = self._axes.plot(
            self._x_data,
            self._values(series),
            marker="o",
            markersize=3,
            label=self._label(field),
        )
        series["line"] = line
        # Colour the check box to match its curve.
        series["box"].setStyleSheet(f"color: {line.get_color()};")

    def _on_event(self, doc):
        if doc.get("descriptor") != self._descriptor_uid:
            return
        data = doc.get("data") or {}

        if self._grid:
            self._x_data.append(doc.get("seq_num", len(self._x_data) + 1))
            for field, series in self._series.items():
                value = data.get(field)
                series["y"].append(
                    value if isinstance(value, (int, float)) else float("nan")
                )
            cell = self._grid_indices(data)
            self._grid_cells.append(cell)
            self._accumulate_grid(cell)
            self._update_status()
            return

        if self._use_elapsed_time:
            x = doc.get("time", 0.0) - (self._start_time or doc.get("time", 0.0))
        elif self._x_field in data:
            x = data[self._x_field]
        else:
            return

        self._x_data.append(x)
        for field, series in self._series.items():
            value = data.get(field)
            # Keep the arrays the same length even if a key is missing.
            series["y"].append(
                value if isinstance(value, (int, float)) else float("nan")
            )
        # The monitor's own point has to be in before any ratio is computed,
        # hence the second pass.
        for field, series in self._series.items():
            if self._shown(series):
                if series["line"] is None:
                    self._create_line(field, series)
                else:
                    series["line"].set_data(self._x_data, self._values(series))

        self._rescale()
        self._update_status()

    def _update_status(self):
        normalised = f", normalised by {self._monitor}" if self._monitor else ""
        if self._grid:
            rows, columns = self._grid["shape"]
            self._status.setText(
                f"{len(self._x_data)} of {rows * columns} points — "
                f"{self._grid_field} vs {self._grid['fast']} / "
                f"{self._grid['slow']}{normalised}"
            )
            return
        shown = sum(1 for s in self._series.values() if self._shown(s))
        self._status.setText(
            f"{len(self._x_data)} points — {shown} of {len(self._series)} "
            f"curves shown{normalised}"
        )

    def _rescale(self):
        # visible_only so hidden curves do not stretch the axes.
        self._axes.relim(visible_only=True)
        self._axes.autoscale_view()
        visible = [
            s["line"]
            for s in self._series.values()
            if s["line"] is not None and self._shown(s)
        ]
        if len(visible) > 1:
            self._axes.legend(fontsize="small", loc="best")
        elif self._axes.get_legend() is not None:
            self._axes.get_legend().remove()
        if len(visible) == 1:
            self._axes.set_ylabel(visible[0].get_label())
        else:
            self._axes.set_ylabel("")
        self._canvas.draw_idle()

    def _on_stop(self, doc):
        # The curves are deliberately left on the axes after the scan.
        reason = doc.get("exit_status", "completed")
        self._status.setText(f"Scan {reason} — {len(self._x_data)} points.")
        self._canvas.draw_idle()
        self._scan_running = False
        self._update_peak()

    # -- peak statistics, markers and the move buttons ---------------------

    def _peak_field(self):
        """Which curve the peak row describes: the combo's choice, if drawn."""
        shown = [name for name, s in self._series.items() if self._shown(s)]
        current = self._peak_combo.currentText()
        if current not in shown:
            current = shown[0] if shown else ""
        # Repopulate only when the list really changed, or the combo fights the
        # user by resetting the choice on every event.
        combo = self._peak_combo
        existing = [combo.itemText(i) for i in range(combo.count())]
        if existing != shown:
            blocked = self._peak_combo.blockSignals(True)
            self._peak_combo.clear()
            self._peak_combo.addItems(shown)
            self._peak_combo.blockSignals(blocked)
        if current:
            blocked = self._peak_combo.blockSignals(True)
            self._peak_combo.setCurrentText(current)
            self._peak_combo.blockSignals(blocked)
        return current or None

    def _peak_axis(self):
        """Dotted path of the scanned axis, or None if it cannot be resolved.

        The documents name the axis by its *hinted field* (``psic_h``); the
        command has to name it by a path that is valid Python in the session
        (``psic.h``).  ``_gui_scan_options()`` supplies the map.
        """
        if not self._x_field or self._use_elapsed_time:
            return None
        return self._axis_fields.get(self._x_field)

    @staticmethod
    def _stat_position(stats, name):
        """x of a statistic, or None.  ``max``/``min`` are ``(x, y)`` pairs."""
        value = (stats or {}).get(name)
        if value is None:
            return None
        return value[0] if name in ("max", "min") else value

    def _update_peak(self):
        """Recompute the peak row and markers, and set the buttons' state."""
        self._peak_stats = None

        # A mesh has two axes and no single peak; count() has no axis at all.
        if self._grid or self._use_elapsed_time or not self._series:
            self._peak_row.setVisible(False)
            self._draw_markers({})
            return
        self._peak_row.setVisible(True)

        field = self._peak_field()
        if field is None:
            self._show_peak("No curve selected.", {})
            self._draw_markers({})
            return

        series = self._series[field]
        stats = peak_statistics(self._x_data, self._values(series))
        self._peak_stats = stats

        positions = {
            name: self._stat_position(stats, name) for name, _colour in MARKER_STYLES
        }
        # The statistic's name carries its marker's colour, so the peak row is
        # the key to the plot.  Rich text collapses runs of spaces, hence the
        # explicit gaps.
        parts = [
            f'<span style="color:{colour}; font-weight:bold">{name}</span> '
            + (_format(positions[name]) if positions[name] is not None else "—")
            for name, colour in MARKER_STYLES
        ]
        parts.append(
            f"fwhm {_format(stats['fwhm'])}" if stats["fwhm"] is not None else "fwhm —"
        )
        self._show_peak("&nbsp;&nbsp;&nbsp;".join(parts), positions)
        self._draw_markers(positions)

    def _show_peak(self, text, positions):
        """Fill in the peak row, disabling each button that cannot act."""
        self._peak_values.setText(text)
        axis = self._peak_axis()

        if self._scan_running:
            reason = "Wait for the scan to finish."
        elif not self._kernel_idle:
            reason = "The kernel is busy."
        elif axis is None:
            reason = (
                f"Cannot work out which device reports '{self._x_field}'."
                if self._x_field
                else "No scanned axis."
            )
        else:
            reason = None

        for name, _colour in MARKER_STYLES:
            button = self._peak_buttons[name]
            position = positions.get(name)
            button.setEnabled(reason is None and position is not None)
            # The value is *not* repeated on the button: four of them, each with
            # a ten-significant-digit position, overran the row and clipped the
            # labels.  The peak row prints the number a couple of centimetres to
            # the left in this button's own colour, and the tooltip carries the
            # exact command, so nothing is lost by keeping the button short.
            if reason is not None:
                button.setToolTip(reason)
            elif position is None:
                button.setToolTip(f"This curve has no {name}.")
            else:
                button.setToolTip(
                    f"{MARKER_TOOLTIPS[name]}\nRE(mv({axis}, {_format(position)}))"
                )

    def _draw_markers(self, positions):
        """One dotted vertical line per statistic, or none if the box is off.

        Called from ``_update_peak`` only, which is not run per event, so the
        markers settle once the scan ends rather than jittering through it.
        """
        wanted = self._markers_box.isChecked()
        for name, colour in MARKER_STYLES:
            position = positions.get(name)
            line = self._marker_lines.get(name)
            if not wanted or position is None:
                if line is not None:
                    self._remove_marker(name)
                continue
            if line is None:
                # "_nolegend_": the legend is the curves' -- a marker is named
                # by the colour of its statistic in the peak row instead.
                self._marker_lines[name] = self._axes.axvline(
                    position,
                    color=colour,
                    linestyle=":",
                    linewidth=1.5,
                    label="_nolegend_",
                )
            else:
                line.set_xdata([position, position])
        self._canvas.draw_idle()

    def _remove_marker(self, name):
        line = self._marker_lines.pop(name, None)
        if line is None:
            return
        try:
            line.remove()
        except (ValueError, NotImplementedError):
            # Already gone with an axes.clear(); nothing left to do.
            pass

    def _go_to(self, statistic):
        """Move the scanned axis onto *statistic* of the displayed curve."""
        axis = self._peak_axis()
        value = self._stat_position(self._peak_stats, statistic)
        if axis is None or value is None:
            return
        # An explicit literal rather than RE(cen()): it is exactly the number
        # on the button, it needs no catalog round-trip, and it reads back in
        # the session history as an ordinary move.
        self.run_in_console(f"RE(mv({axis}, {_format(value)}))")

    # -- kernel exchange ----------------------------------------------------

    def refresh(self):
        """Re-read the axis list, which is how a hinted field becomes a path."""
        self.request({OPTIONS_KEY: OPTIONS_EXPR})

    def on_kernel_values(self, values):
        """Pick up the hinted-field to dotted-path map."""
        options = values.get(OPTIONS_KEY)
        if not isinstance(options, dict):
            return
        fields = options.get("axis_fields")
        if isinstance(fields, dict):
            self._axis_fields = fields
            self._update_peak()

    # -- misc -------------------------------------------------------------

    def _draw_placeholder(self):
        self._axes.set_xticks([])
        self._axes.set_yticks([])
        self._axes.text(
            0.5,
            0.5,
            "The next scan will be plotted here.",
            ha="center",
            va="center",
            transform=self._axes.transAxes,
            alpha=0.5,
        )
        self._canvas.draw_idle()

    def on_kernel_state(self, state):
        """Note when the kernel is gone, so a stalled plot is explicable."""
        self._kernel_idle = state == "idle"
        if state == "idle" and not self._axis_fields:
            self.refresh()
        if state == "dead":
            self._status.setText("Kernel is not running.")
        self._update_peak()
