"""Detectors tab: set the ophyd ``Kind`` of each detector channel.

Unlike the check boxes on the Scan plot tab -- which only hide and show curves
that are already being recorded -- this changes the kernel-side configuration
and therefore what future scans read:

``hinted``
    read into the event stream and plotted.
``normal``
    read into the event stream, not plotted.
``config``
    recorded once per descriptor rather than per event.
``omitted``
    not read at all; the channel does not appear in the data.

The four are radio buttons in a column each, so the choice for a row is one
click and the whole tree can be read down a column.

Channels come from each detector's ``plot_signals`` mapping, so any detector
implementing the CountersClass interface appears here without changes.
"""

import logging

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QButtonGroup
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QDialog
from qtpy.QtWidgets import QDialogButtonBox
from qtpy.QtWidgets import QGridLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLineEdit
from qtpy.QtWidgets import QListWidget
from qtpy.QtWidgets import QListWidgetItem
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QRadioButton
from qtpy.QtWidgets import QTreeWidget
from qtpy.QtWidgets import QTreeWidgetItem
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from .base import BaseTab
from .base import value_label

logger = logging.getLogger(__name__)

#: Key for the one-off kernel request and its reply.
KINDS_KEY = "detector_kinds"
KINDS_EXPR = "_gui_detector_kinds()"

#: Devices recorded alongside the detectors (``counters.extra_devices``).
EXTRAS_KEY = "detector_extras"
EXTRAS_EXPR = "_gui_extra_devices()"
ADDABLE_KEY = "detector_addable"
ADDABLE_EXPR = "_gui_addable_devices()"
#: Kinds of the signals belonging to the extra devices.
EXTRA_KINDS_KEY = "detector_extra_kinds"

#: Automatic attenuation settings (see ``gui/atten_bridge.py``).
ATTEN_KEY = "detector_attenuation"
ATTEN_EXPR = "_gui_atten_state()"

#: PVA streaming cache settings (see ``gui/pva_bridge.py``).
PVA_KEY = "detector_pva"
PVA_EXPR = "_gui_pva_state()"

#: Caps on the attenuation entry boxes.  Uncapped, a ``QGridLayout`` column
#: with a stretch factor hands its widget the whole surplus, so a box holding
#: ``3000`` (39 px of glyphs) was measured at 830 px and the channel combos at
#: 1771 -- the trap ``tabs/scan.py`` documents.  The numeric cap matches
#: ``LATTICE_WIDTH`` in the HKL tab so the two read alike; the combo cap fits
#: the longest real channel name, ``lambda250k_stats5_array_counter`` at
#: 272 px of glyphs, since the point of the combo is to show which one is
#: selected.
ATTEN_VALUE_WIDTH = 80
ATTEN_NAME_WIDTH = 300

#: Ceiling on the width the channel tree may demand (see
#: ``DetectorsTab._fit_tree_width``).  Past this the tab would be pushed wider
#: than the window, which moves the horizontal scrollbar out to the tab rather
#: than getting rid of it.
TREE_MAX_MINIMUM = 1200

#: Offered in the order a user thinks about them, one tree column each.
KIND_CHOICES = ["hinted", "normal", "config", "omitted"]

#: What each kind does, on the column header and on every radio.
KIND_TOOLTIPS = {
    "hinted": "Read at every point and plotted.",
    "normal": "Read at every point, not plotted.",
    "config": "Recorded once per descriptor rather than at every point.",
    "omitted": (
        "Not read at all — the channel does not appear in the data. "
        "This is how an unnamed scaler channel is kept out of the "
        "descriptor; omitting a named one loses its readings."
    ),
}

#: Why an Apply button is disabled, on every Apply that waits for idle.
_WAITING_FOR_IDLE = "Waiting for the kernel to go idle — a scan is running."

#: Column 0 is the name, column 1 a free-text detail (the PV prefix on a device
#: row, an unrecognised kind on a channel row), then one column per kind.
DETAIL_COLUMN = 1
FIRST_KIND_COLUMN = 2


def _half_width(widget):
    """Give *widget* the left half of its row and leave the right half empty.

    A share of the width rather than a pixel cap, so it tracks the window
    instead of going wrong on the next monitor.

    Half is the *preference*, not a limit: a widget whose minimum width is
    larger wins, and ``DetectorsTab._fit_tree_width`` sets exactly such a
    minimum from the populated columns.  So the group is half the window when
    half is enough to show everything, and grows past half instead of
    scrolling when it is not.
    """
    row = QHBoxLayout()
    row.addWidget(widget, 1)
    row.addStretch(1)
    return row


def _centered(widget):
    """Wrap *widget* so it sits in the middle of its tree cell.

    ``setItemWidget`` takes no alignment, and a bare radio indicator would
    otherwise hug the left edge of a column headed by a centred name.
    """
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 0, Qt.AlignCenter)
    return holder


class _KindRow:
    """The Kind radios for one channel, one per column, exclusive within a row.

    The ``QButtonGroup`` is deliberately parentless and owned by this object:
    parented to the tree it would outlive ``clear()`` and accumulate one dead
    group per channel on every refresh.
    """

    def __init__(self, tree, item, kind, on_change):
        """Fill *item*'s kind columns with radios, *kind* preselected."""
        self.original = kind
        self._group = QButtonGroup()
        self._buttons = {}
        for offset, choice in enumerate(KIND_CHOICES):
            button = QRadioButton()
            button.setToolTip(KIND_TOOLTIPS[choice])
            button.setChecked(choice == kind)
            self._group.addButton(button)
            self._buttons[choice] = button
            tree.setItemWidget(item, FIRST_KIND_COLUMN + offset, _centered(button))
        # Connected only after the initial state is set, so building the tree
        # does not register as an edit.
        for button in self._buttons.values():
            button.toggled.connect(on_change)

    def kind(self):
        """The chosen kind, or None when the kernel reported an unknown one.

        ``Kind`` is a flag, so a channel can report a composite like
        ``normal|config`` that matches no radio.  Nothing is checked then, and
        the row simply contributes no change until the user picks a kind.
        """
        for choice, button in self._buttons.items():
            if button.isChecked():
                return choice
        return None


class DetectorsTab(BaseTab):
    """Per-channel Kind selection for every detector."""

    title = "Detectors"

    def __init__(self, parent=None):
        """Build the (initially empty) channel tree and its buttons."""
        super().__init__(parent)
        self._rows = []
        self._extra_rows = []
        self._editors = {}  # (device, channel) -> _KindRow
        self._extra_editors = {}  # same, for extra-device signals
        self._kernel_idle = False
        # The attenuation fields are filled from the kernel once and then left
        # alone.  They are on the 1 Hz poll like everything else here, and
        # rewriting them on every reply would pull the text out from under
        # whoever is typing in them.
        self._atten_loaded = False
        self._pva_loaded = False  # same, for the PVA streaming fields

        self._tree = QTreeWidget()
        self._tree.setColumnCount(FIRST_KIND_COLUMN + len(KIND_CHOICES))
        self._tree.setHeaderLabels(["Detector / channel", "", *KIND_CHOICES])
        header = self._tree.headerItem()
        for offset, choice in enumerate(KIND_CHOICES):
            column = FIRST_KIND_COLUMN + offset
            header.setToolTip(column, KIND_TOOLTIPS[choice])
            header.setTextAlignment(column, Qt.AlignCenter)
        # Otherwise the last section absorbs the whole window width and the
        # "omitted" radio ends up an inch away from the other three.  With it
        # off, the columns keep their resized-to-contents widths and the slack
        # is left blank at the right.
        self._tree.header().setStretchLastSection(False)
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)

        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply)
        self._apply_button.setEnabled(False)
        self._reload_button = QPushButton("Reload")
        self._reload_button.clicked.connect(self.refresh)

        self._status = QLabel("Waiting for the session…")

        buttons = QHBoxLayout()
        buttons.addWidget(self._status, 1)
        buttons.addWidget(self._reload_button)
        buttons.addWidget(self._apply_button)

        channels = QGroupBox("Detector channels")
        channels_layout = QVBoxLayout(channels)
        channels_layout.addWidget(self._tree)
        channels_layout.addLayout(buttons)

        # Kept so _fit_tree_width can hold all three to one width: the tree
        # needs the most, and a stack of group boxes with ragged right edges
        # reads as a mistake.
        self._groups = [
            channels,
            self._build_extras_group(),
            self._build_attenuation_group(),
            self._build_pva_group(),
        ]
        layout = QVBoxLayout(self)
        layout.addLayout(_half_width(self._groups[0]), 1)
        for group in self._groups[1:]:
            layout.addLayout(_half_width(group))

    def _build_attenuation_group(self):
        """Threshold-driven filter transmission control during scans.

        Belongs here rather than on the Scan tab: like the ``Kind`` tree above
        it is kernel-side configuration that changes what *future* scans do,
        and the channel it watches is one of the detector channels listed
        above it.
        """
        box = QGroupBox("Automatic attenuation")
        outer = QVBoxLayout(box)
        # Wrapped, or the label's one-line minimumSizeHint becomes the group's
        # minimum width and no share-of-the-width layout can shrink it.
        caption = QLabel(
            "Retakes a scan point at a different filter transmission "
            "until the watched channel is inside the window. Rejected "
            "readings are discarded, so only the accepted one is "
            "recorded. A point that cannot be brought into range is "
            "accepted as it stands."
        )
        caption.setWordWrap(True)
        outer.addWidget(caption)

        self._atten_enabled = QCheckBox("Arm automatic attenuation")
        self._atten_enabled.setToolTip(
            "While this is off the scans behave exactly as they did before "
            "the feature existed. Turning it off does not put the filters "
            "back — use filters.allout() for that."
        )
        outer.addWidget(self._atten_enabled)

        grid = QGridLayout()
        # Every field is capped (see ATTEN_VALUE_WIDTH) and the surplus goes
        # to an empty column on the right.  Giving the *field* columns the
        # stretch instead, as this first did, handed a box holding "3000" the
        # whole 830 px the row had spare.
        for column in range(4):
            grid.setColumnStretch(column, 0)
        grid.setColumnStretch(4, 1)

        self._atten_signal = QComboBox()
        self._atten_signal.setEditable(True)
        self._atten_signal.setToolTip(
            "Data key to watch. For the Lambda 250K, stats5 is fed the whole "
            "frame, so lambda250k_stats5_max_value is the brightest pixel on "
            "the detector; stats1–4 are per-ROI."
        )
        self._atten_signal.setMaximumWidth(ATTEN_NAME_WIDTH)
        grid.addWidget(QLabel("Watch channel"), 0, 0)
        # Spans into the trailing stretch column, so ATTEN_NAME_WIDTH is what
        # limits it.  Spanning only the field columns pinned it to their
        # combined 270 px and clipped the longest channel name.
        grid.addWidget(self._atten_signal, 0, 1, 1, 4)

        self._atten_low = QLineEdit()
        self._atten_high = QLineEdit()
        grid.addWidget(QLabel("Accept above"), 1, 0)
        grid.addWidget(self._atten_low, 1, 1)
        grid.addWidget(QLabel("and below"), 1, 2)
        grid.addWidget(self._atten_high, 1, 3)

        self._atten_factor = QLineEdit()
        self._atten_factor.setToolTip(
            "Transmission step. A number steps by exactly that, so 10 walks "
            "1 → 0.1 → 0.01. 'auto' works each step out from how far off the "
            "reading is, usually reaching the window in one step."
        )
        self._atten_tries = QLineEdit()
        self._atten_tries.setToolTip(
            "Adjustments allowed per point before the point is accepted as it stands."
        )
        grid.addWidget(QLabel("Step factor"), 2, 0)
        grid.addWidget(self._atten_factor, 2, 1)
        grid.addWidget(QLabel("Max retakes"), 2, 2)
        grid.addWidget(self._atten_tries, 2, 3)

        for field in (
            self._atten_low,
            self._atten_high,
            self._atten_factor,
            self._atten_tries,
        ):
            field.setMaximumWidth(ATTEN_VALUE_WIDTH)

        self._atten_counter = QComboBox()
        self._atten_counter.setEditable(True)
        self._atten_counter.setToolTip(
            "Array counter confirming the detector plugin processed the "
            "frame just taken, e.g. lambda250k_stats5_array_counter. Set it "
            "for an area detector: plugin callbacks are asynchronous and a "
            "stale reading steers the adjustment the wrong way."
        )
        self._atten_counter.setMaximumWidth(ATTEN_NAME_WIDTH)
        grid.addWidget(QLabel("Frame counter"), 3, 0)
        grid.addWidget(self._atten_counter, 3, 1, 1, 4)

        outer.addLayout(grid)

        row = QHBoxLayout()
        self._atten_status = QLabel("Waiting for the session…")
        row.addWidget(self._atten_status, 1)
        self._atten_apply = QPushButton("Apply")
        self._atten_apply.clicked.connect(self._apply_attenuation)
        self._atten_apply.setEnabled(False)
        row.addWidget(self._atten_apply)
        outer.addLayout(row)
        return box

    def _build_pva_group(self):
        """The PVA streaming cache, started and stopped around each scan.

        Here for the same reason as the attenuation group: it is kernel-side
        configuration that changes what *future* scans do, and what it caches
        is the detector stream listed above.
        """
        box = QGroupBox("PVA streaming cache")
        outer = QVBoxLayout(box)
        caption = QLabel(
            "Writes the directory and file name to the streaming server and "
            "raises its ScanOn flag before each scan, clearing it a moment "
            "after. The flag is cleared even by a scan that is interrupted "
            "or fails."
        )
        caption.setWordWrap(True)
        outer.addWidget(caption)

        self._pva_enabled = QCheckBox("Arm PVA streaming")
        self._pva_enabled.setToolTip(
            "While this is off the scans behave exactly as they did before "
            "the feature existed."
        )
        outer.addWidget(self._pva_enabled)

        grid = QGridLayout()
        # Same column arrangement as the attenuation grid: the fields are
        # capped and an empty right-hand column takes the surplus.
        for column in range(4):
            grid.setColumnStretch(column, 0)
        grid.setColumnStretch(4, 1)

        self._pva_format = QLineEdit()
        self._pva_format.setToolTip(
            "printf format applied to (experiment base name, scan number), "
            "as 'pva_%s_%05d.h5' is."
        )
        self._pva_format.setMaximumWidth(ATTEN_NAME_WIDTH)
        grid.addWidget(QLabel("File name format"), 0, 0)
        grid.addWidget(self._pva_format, 0, 1, 1, 4)

        self._pva_delay = QLineEdit()
        self._pva_delay.setToolTip(
            "Seconds between the end of the scan and ScanOn → 0, so the cache "
            "can finish writing."
        )
        self._pva_delay.setMaximumWidth(ATTEN_VALUE_WIDTH)
        grid.addWidget(QLabel("Stop delay"), 1, 0)
        grid.addWidget(self._pva_delay, 1, 1)
        grid.addWidget(QLabel("s"), 1, 2)

        self._pva_tilde = QCheckBox("Abbreviate the home directory to ~")
        self._pva_tilde.setToolTip(
            "The directory PV holds 39 characters and /home/beams18/USER6IDB "
            "alone is 22 of them, so a full path is usually truncated. "
            "Whatever reads the PV has to expand the ~ itself."
        )
        grid.addWidget(self._pva_tilde, 2, 0, 1, 5)

        self._pva_next = value_label()
        # Wrapped for the reason the caption is: an unwrapped path sets the
        # group's minimum width from its own one-line hint.
        self._pva_next.setWordWrap(True)
        grid.addWidget(QLabel("Next file"), 3, 0)
        grid.addWidget(self._pva_next, 3, 1, 1, 4)

        self._pva_readback = value_label()
        self._pva_readback.setWordWrap(True)
        self._pva_readback.setToolTip(
            "What the three PVs hold now. The directory PV is the one to "
            "check: an over-long path is truncated by the server without "
            "anything being reported to the client."
        )
        grid.addWidget(QLabel("PVs now"), 4, 0)
        grid.addWidget(self._pva_readback, 4, 1, 1, 4)

        outer.addLayout(grid)

        self._pva_warning = QLabel()
        self._pva_warning.setWordWrap(True)
        self._pva_warning.setVisible(False)
        outer.addWidget(self._pva_warning)

        row = QHBoxLayout()
        self._pva_status = QLabel("Waiting for the session…")
        row.addWidget(self._pva_status, 1)
        self._pva_apply = QPushButton("Apply")
        self._pva_apply.clicked.connect(self._apply_pva)
        self._pva_apply.setEnabled(False)
        row.addWidget(self._pva_apply)
        outer.addLayout(row)
        return box

    def _build_extras_group(self):
        """Devices recorded alongside the detectors at every scan point."""
        box = QGroupBox("Also record during scans (extra devices)")
        outer = QVBoxLayout(box)
        caption = QLabel(
            "Any EPICS device — temperature, capacitance, a source meter — "
            "can be recorded here. Extras skip the count-time setup, so "
            "they need no preset_monitor. Tick them in the Scan plot tab "
            "to plot them."
        )
        caption.setWordWrap(True)
        outer.addWidget(caption)
        self._extras_list = QListWidget()
        self._extras_list.setMaximumHeight(110)
        outer.addWidget(self._extras_list)

        row = QHBoxLayout()
        self._show_omitted = QCheckBox("Show unrecorded (omitted) channels")
        self._show_omitted.setToolTip(
            "Omitted signals are not recorded.  On a motor bundle they are "
            "most of the list (52 of sl1's 100), so they are hidden by default."
        )
        self._show_omitted.toggled.connect(lambda _s: self.refresh())
        row.addWidget(self._show_omitted)
        row.addStretch(1)
        self._add_extra_button = QPushButton("Add…")
        self._add_extra_button.clicked.connect(self._add_extra)
        row.addWidget(self._add_extra_button)
        self._remove_extra_button = QPushButton("Remove")
        self._remove_extra_button.clicked.connect(self._remove_extra)
        row.addWidget(self._remove_extra_button)
        outer.addLayout(row)
        return box

    # -- kernel exchange --------------------------------------------------

    def refresh(self):
        """Re-read detector kinds, extra-device kinds, and the extras list."""
        show_omitted = self._show_omitted.isChecked()
        self.request(
            {
                KINDS_KEY: KINDS_EXPR,
                EXTRAS_KEY: EXTRAS_EXPR,
                EXTRA_KINDS_KEY: f"_gui_extra_kinds({show_omitted!r})",
                ATTEN_KEY: ATTEN_EXPR,
                PVA_KEY: PVA_EXPR,
            }
        )

    def on_kernel_state(self, state):
        """Track idleness; Kind must not be changed during a scan."""
        self._kernel_idle = state == "idle"
        if state == "idle" and not self._rows:
            self.refresh()
        if state == "dead":
            self._status.setText("Kernel is not running.")
        self._update_apply_enabled()

    def on_kernel_values(self, values):
        """Consume kind, extras, addable-device and action replies."""
        rows = values.get(KINDS_KEY)
        extra_rows = values.get(EXTRA_KINDS_KEY)
        if isinstance(rows, list):
            self._rows = rows
        if isinstance(extra_rows, list):
            self._extra_rows = extra_rows
        if isinstance(rows, list) or isinstance(extra_rows, list):
            self._populate()
        extras = values.get(EXTRAS_KEY)
        if isinstance(extras, list):
            self._extras_list.clear()
            self._extras_list.addItems([str(name) for name in extras])
        addable = values.get(ADDABLE_KEY)
        if isinstance(addable, list):
            self._show_add_dialog(addable)
        attenuation = values.get(ATTEN_KEY)
        if isinstance(attenuation, dict):
            self._show_attenuation(attenuation)
        streaming = values.get(PVA_KEY)
        if isinstance(streaming, dict):
            self._show_pva(streaming)

    # -- automatic attenuation ---------------------------------------------

    def _show_attenuation(self, state):
        """Render the attenuation settings, or the kernel's refusal."""
        if state.get("available") is False:
            # The module itself is missing.  Distinct from a refused apply,
            # and it must not latch _atten_loaded: this can arrive before the
            # session is up, and the fields would then never be filled in.
            self._atten_status.setText(
                str(state.get("error") or "Automatic attenuation is not available.")
            )
            self._atten_apply.setEnabled(False)
            return

        error = state.get("error")
        if error:
            # A refused apply.  Leave the fields as typed so the mistake can
            # be corrected in place instead of reverting to the kernel's
            # values, which is what _apply_attenuation cleared the flag for.
            self._atten_loaded = True
            self._atten_status.setText(str(error))
            self._update_apply_enabled()
            return

        # Filter to str before it reaches addItems.  A reply arrives as a repr
        # rendered by IPython's pretty printer, which truncates a sequence
        # past 1000 items by writing a literal `...` into it -- literal_eval
        # then yields a list with an Ellipsis in the middle, and addItems
        # raises TypeError.  That exception used to abort the whole process
        # (see MainWindow._on_kernel_values), taking the live session with it,
        # so this stays defensive even though the kernel side now caps the
        # lists well below the limit.
        def _names(key):
            return [name for name in (state.get(key) or []) if isinstance(name, str)]

        for combo, key in (
            (self._atten_signal, "channels"),
            (self._atten_counter, "counter_channels"),
        ):
            names = _names(key)
            # Refresh the offered names without disturbing what is typed:
            # setCurrentText on an editable combo only writes the line edit.
            typed = combo.currentText()
            if [combo.itemText(i) for i in range(combo.count())] != names:
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(names)
                combo.setCurrentText(typed)
                combo.blockSignals(False)

        if not self._atten_loaded:
            self._atten_loaded = True
            self._atten_enabled.setChecked(bool(state.get("enabled")))
            self._atten_signal.setCurrentText(state.get("signal") or "")
            self._atten_counter.setCurrentText(state.get("counter_signal") or "")
            self._atten_factor.setText(str(state.get("factor") or "auto"))
            for widget, key in (
                (self._atten_low, "low"),
                (self._atten_high, "high"),
                (self._atten_tries, "max_tries"),
            ):
                value = state.get(key)
                widget.setText("" if value is None else f"{value:g}")

        transmission = state.get("transmission")
        where = "" if transmission is None else f"  Transmission {transmission:g}."
        if state.get("ready"):
            self._atten_status.setText(f"Armed, watching {state['signal']}.{where}")
        elif state.get("enabled"):
            self._atten_status.setText(f"Enabled but not configured.{where}")
        else:
            self._atten_status.setText(f"Off.{where}")
        self._update_apply_enabled()

    def _apply_attenuation(self):
        """Send the on-screen settings to the kernel, then re-read them."""
        values = {
            "enabled": self._atten_enabled.isChecked(),
            "signal": self._atten_signal.currentText().strip(),
            "counter_signal": self._atten_counter.currentText().strip(),
            "factor": self._atten_factor.text().strip() or "auto",
        }
        for key, widget in (
            ("low", self._atten_low),
            ("high", self._atten_high),
            ("max_tries", self._atten_tries),
        ):
            text = widget.text().strip()
            if text:
                values[key] = text

        # Sent as an *expression* rather than through execute_once, because
        # _gui_atten_set returns the new state -- or a dict with `error` when
        # it refuses.  execute_once discards the value, which would make a
        # rejected setting look as though it had been applied.  The reply
        # lands under the same key as the ordinary read, so one code path
        # renders both.
        if self.poller is None:
            self._atten_status.setText("Could not reach the kernel.")
            return
        self._atten_loaded = False
        self.request({ATTEN_KEY: f"_gui_atten_set({values!r})"})
        self._atten_status.setText("Applying…")

    # -- PVA streaming ------------------------------------------------------

    @staticmethod
    def _pva_flag(value):
        """Interpret a ScanOn readback as a flag, or None when unreadable.

        Read through ``float`` first: the PV comes back as a number, and a
        bare ``bool`` on the string ``"0"`` -- which is what a differently
        configured signal would give -- is True.
        """
        if value is None:
            return None
        try:
            return bool(float(value))
        except (TypeError, ValueError):
            return bool(value)

    def _show_pva(self, state):
        """Render the PVA streaming settings, or the kernel's refusal."""
        if state.get("available") is False:
            # The module is missing.  As with attenuation this must not latch
            # _pva_loaded: it can arrive before the session is up, and the
            # fields would then never be filled in.
            self._pva_status.setText(
                str(state.get("error") or "PVA streaming is not available.")
            )
            self._pva_apply.setEnabled(False)
            return

        error = state.get("error")
        if error:
            # A refused apply.  Leave the fields as typed so the mistake can be
            # corrected in place.
            self._pva_loaded = True
            self._pva_status.setText(str(error))
            self._update_apply_enabled()
            return

        if not self._pva_loaded:
            self._pva_loaded = True
            self._pva_enabled.setChecked(bool(state.get("enabled")))
            self._pva_tilde.setChecked(bool(state.get("use_tilde")))
            self._pva_format.setText(str(state.get("name_format") or ""))
            delay = state.get("stop_delay")
            self._pva_delay.setText("" if delay is None else f"{delay:g}")

        path = state.get("file_path")
        name = state.get("file_name")
        if path and name:
            self._pva_next.setText(f"{path}/{name}")
        else:
            self._pva_next.setText(
                str(state.get("next_error") or "(experiment not set up)")
            )

        flag = self._pva_flag(state.get("scan_on"))
        pv_path = state.get("pv_file_path")
        pv_name = state.get("pv_file_name")
        if flag is None and pv_path is None and pv_name is None:
            self._pva_readback.setText("(not connected)")
        else:
            caching = {None: "?", True: "1 (caching)", False: "0"}[flag]
            self._pva_readback.setText(
                f"ScanOn {caching}   {pv_path or '?'}/{pv_name or '?'}"
            )

        limit = state.get("max_string")
        warnings = []
        for label, value, key in (
            ("directory", path, "path_over"),
            ("file name", name, "name_over"),
        ):
            if state.get(key):
                warnings.append(
                    f"! The {label} is {len(value)} characters; the PV holds "
                    f"{limit} and the server truncates the rest without "
                    f"reporting it."
                )
        if flag:
            # Any reply we receive was evaluated while the kernel was idle, so
            # no scan is running -- a raised flag here is a cache left
            # collecting, not one in normal use.
            warnings.append(
                "! ScanOn is set with no scan running; the cache is still "
                "collecting. Clear it with pva_stream.stop_caching()."
            )
        self._pva_warning.setText("\n".join(warnings))
        self._pva_warning.setVisible(bool(warnings))

        if state.get("ready"):
            self._pva_status.setText("Armed.")
        elif state.get("enabled"):
            self._pva_status.setText(
                f"Enabled, but no device named '{state.get('device')}' was found."
            )
        else:
            self._pva_status.setText("Off.")
        self._update_apply_enabled()

    def _apply_pva(self):
        """Send the on-screen streaming settings to the kernel, then re-read."""
        values = {
            "enabled": self._pva_enabled.isChecked(),
            "use_tilde": self._pva_tilde.isChecked(),
        }
        # An empty box means "leave that setting alone", as it does in the
        # attenuation group -- sending "" would be refused as not a number.
        for key, widget in (
            ("name_format", self._pva_format),
            ("stop_delay", self._pva_delay),
        ):
            text = widget.text().strip()
            if text:
                values[key] = text
        # Sent as an expression, not through execute_once, for the reason
        # _apply_attenuation documents: _gui_pva_set returns the new state, or
        # a dict with `error` when it refuses, and execute_once discards it.
        if self.poller is None:
            self._pva_status.setText("Could not reach the kernel.")
            return
        self._pva_loaded = False
        self.request({PVA_KEY: f"_gui_pva_set({values!r})"})
        self._pva_status.setText("Applying…")

    # -- view -------------------------------------------------------------

    def _populate(self):
        self._tree.clear()
        self._editors.clear()
        self._extra_editors.clear()

        self._add_device_nodes(self._rows, self._editors, suffix="")
        self._add_device_nodes(
            self._extra_rows, self._extra_editors, suffix="   (extra)"
        )

        # expandAll() rather than per-item setExpanded(): the latter is ignored
        # when the tree is populated while its tab is not the visible one.
        self._tree.expandAll()
        self._tree.resizeColumnToContents(0)
        self._tree.resizeColumnToContents(DETAIL_COLUMN)
        for offset in range(len(KIND_CHOICES)):
            # Keep the radio columns as narrow as their headers, so the channel
            # names get the width.
            self._tree.resizeColumnToContents(FIRST_KIND_COLUMN + offset)
        self._fit_tree_width()
        self._update_apply_enabled()

    def _fit_tree_width(self):
        """Ask for at least the width the columns actually need.

        The group is laid out as a share of the window (see
        :func:`_half_width`), which on its own let the tree fall below its
        content and scroll sideways.  A minimum computed from the *populated*
        columns is what stops that: it follows the channel names actually
        present rather than a guess, and it still lets the group take only
        half when half is more than enough.

        Capped at :data:`TREE_MAX_MINIMUM` so an unusually long name cannot
        push the tab wider than the window and move the scrollbar rather than
        remove it.
        """
        header = self._tree.header()
        columns = sum(
            header.sectionSize(index) for index in range(self._tree.columnCount())
        )
        if not columns:
            return
        # Frame, and the vertical scrollbar that appears once the list is
        # long -- without its width the last column is what gets clipped.
        chrome = (
            2 * self._tree.frameWidth()
            + self._tree.verticalScrollBar().sizeHint().width()
        )
        wanted = min(columns + chrome, TREE_MAX_MINIMUM)
        self._tree.setMinimumWidth(wanted)
        # Match the other two to the tree's group.  Ask Qt for that group's
        # own minimum rather than adding up margins: the group box contributes
        # a frame as well as its layout's margins, and hand-summing them left
        # the edges 6 px out of line.
        group_width = self._groups[0].minimumSizeHint().width()
        for group in self._groups[1:]:
            group.setMinimumWidth(group_width)

    def _add_device_nodes(self, rows, editors, suffix):
        """Add one tree node per device, with Kind radios for each channel."""
        by_device = {}
        for device, prefix, channel, kind in rows:
            by_device.setdefault((device, prefix), []).append((channel, kind))

        for (device, prefix), channels in by_device.items():
            parent = QTreeWidgetItem(self._tree, [f"{device}{suffix}", prefix])
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            for channel, kind in channels:
                child = QTreeWidgetItem(parent, [channel, ""])
                if kind not in KIND_CHOICES:
                    # A composite flag such as normal|config matches no radio,
                    # so show what the kernel reported rather than nothing.
                    child.setText(DETAIL_COLUMN, str(kind))
                editors[(device, channel)] = _KindRow(
                    self._tree, child, kind, self._on_edit
                )

    @staticmethod
    def _collect_changes(editors):
        changes = []
        for (device, channel), row in editors.items():
            chosen = row.kind()
            if chosen is not None and chosen != row.original:
                changes.append((device, channel, chosen))
        return changes

    def _pending_changes(self):
        """Return every pending change, detector and extra alike."""
        return self._collect_changes(self._editors) + self._collect_changes(
            self._extra_editors
        )

    def _on_edit(self, _checked):
        # Each pick toggles two radios (the old one off, the new one on), so
        # this runs twice per click; it only recomputes, so that is harmless.
        self._update_apply_enabled()

    def _update_apply_enabled(self):
        changes = self._pending_changes()
        self._apply_button.setEnabled(bool(changes) and self._kernel_idle)
        self._add_extra_button.setEnabled(self._kernel_idle)
        self._remove_extra_button.setEnabled(self._kernel_idle)
        waiting = "" if self._kernel_idle else _WAITING_FOR_IDLE
        # Arming mid-scan would change the retake behaviour of a scan already
        # under way, so this waits for idle like everything else here.
        self._atten_apply.setEnabled(self._kernel_idle)
        self._atten_apply.setToolTip(waiting)
        # Arming mid-scan would mean the cache file was named for a scan that
        # is already under way, so this waits for idle too.
        self._pva_apply.setEnabled(self._kernel_idle)
        self._pva_apply.setToolTip(waiting)
        if not self._rows:
            return
        if not changes:
            self._status.setText(f"{len(self._rows)} channels.")
        elif not self._kernel_idle:
            self._status.setText(
                f"{len(changes)} change(s) pending — waiting for the kernel to go idle."
            )
        else:
            self._status.setText(f"{len(changes)} change(s) ready to apply.")

    # -- apply ------------------------------------------------------------

    def _apply(self):
        # Detector channels resolve through plot_signals; extra-device signals
        # resolve by dotted attribute path, so they need separate helpers.
        detector_changes = self._collect_changes(self._editors)
        extra_changes = self._collect_changes(self._extra_editors)
        total = len(detector_changes) + len(extra_changes)
        if not total or self.poller is None:
            return
        ok = True
        if detector_changes:
            ok &= self.poller.execute_once(f"_gui_set_kinds({detector_changes!r})")
        if extra_changes:
            ok &= self.poller.execute_once(f"_gui_set_extra_kinds({extra_changes!r})")
        if not ok:
            self._status.setText("Could not apply changes; see the log.")
            return
        self._status.setText(f"Applied {total} change(s).")
        if any(kind == "omitted" for *_, kind in extra_changes):
            # Omitted extras are filtered out of the listing, so the row the
            # user just changed would vanish and could not be undone.  Signals
            # are blocked because refresh() follows anyway.
            self._show_omitted.blockSignals(True)
            self._show_omitted.setChecked(True)
            self._show_omitted.blockSignals(False)
        # Read back so the tree shows what the kernel actually accepted.
        self.refresh()

    def on_document(self, name, doc):
        """Refresh after a run, since plans may re-hint channels themselves."""
        if name == "stop":
            self.refresh()

    # -- extra devices ----------------------------------------------------

    def _add_extra(self):
        """Ask the kernel which devices are available, then offer them."""
        self._status.setText("Looking up available devices…")
        self.request({ADDABLE_KEY: ADDABLE_EXPR})

    def _show_add_dialog(self, addable):
        """Present the picker once the device list arrives."""
        if not addable:
            self._status.setText("No further devices available to record.")
            return
        dialog = _AddExtraDialog(addable, self)
        if dialog.exec_() != QDialog.Accepted:
            self._status.setText("Cancelled.")
            return
        name = dialog.selected_name()
        if not name:
            return
        self._mutate(f"_gui_add_extra({name!r})", f"Recording {name}.")

    def _remove_extra(self):
        item = self._extras_list.currentItem()
        if item is None:
            self._status.setText("Select an extra device to remove.")
            return
        name = item.text()
        self._mutate(f"_gui_remove_extra({name!r})", f"No longer recording {name}.")

    def _mutate(self, call, message):
        """Change the extras list, then re-read it.

        Sent with ``execute_once`` rather than as a polled expression: the
        poller batches expressions into one request, so a mutation and a read
        of the same state can land in the same batch and the read may be
        evaluated first, reporting stale values.  A statement goes out on the
        shell channel immediately and is therefore queued ahead of the next
        poll's read.
        """
        if self.poller is None or not self.poller.execute_once(call):
            self._status.setText("Could not reach the kernel.")
            return
        self._status.setText(message)
        self.refresh()


class _AddExtraDialog(QDialog):
    """Filterable picker of devices that can be recorded during scans."""

    def __init__(self, addable, parent=None):
        """Populate from ``[(name, class, prefix), ...]``."""
        super().__init__(parent)
        self.setWindowTitle("Add device to record")
        self.resize(460, 380)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter…")
        self._filter.textChanged.connect(self._apply_filter)

        self._list = QListWidget()
        for name, class_name, prefix in addable:
            label = f"{name}   ({class_name}{'  ' + prefix if prefix else ''})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(lambda _i: self.accept())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._filter)
        layout.addWidget(self._list)
        layout.addWidget(buttons)

    def _apply_filter(self, text):
        needle = text.strip().lower()
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def selected_name(self):
        """Return the registry name of the chosen device, if any."""
        item = self._list.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(Qt.UserRole)
