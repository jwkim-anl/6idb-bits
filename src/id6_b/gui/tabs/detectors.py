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
from qtpy.QtWidgets import QDialog
from qtpy.QtWidgets import QDialogButtonBox
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

#: Column 0 is the name, column 1 a free-text detail (the PV prefix on a device
#: row, an unrecognised kind on a channel row), then one column per kind.
DETAIL_COLUMN = 1
FIRST_KIND_COLUMN = 2


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

        layout = QVBoxLayout(self)
        layout.addWidget(channels, 1)
        layout.addWidget(self._build_extras_group())

    def _build_extras_group(self):
        """Devices recorded alongside the detectors at every scan point."""
        box = QGroupBox("Also record during scans (extra devices)")
        outer = QVBoxLayout(box)
        outer.addWidget(
            QLabel(
                "Any EPICS device — temperature, capacitance, a source meter — "
                "can be recorded here. Extras skip the count-time setup, so "
                "they need no preset_monitor. Tick them in the Scan plot tab "
                "to plot them."
            )
        )
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
        self._update_apply_enabled()

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
