"""Devices tab: everything registered in ``oregistry``.

The listing is fetched on demand rather than on the 1 s status poll -- it is a
comparatively expensive expression and the device set only changes when the
session is (re)started.
"""

import logging

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QAbstractItemView
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QHeaderView
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLineEdit
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QTableWidget
from qtpy.QtWidgets import QTableWidgetItem
from qtpy.QtWidgets import QVBoxLayout

from .base import BaseTab

logger = logging.getLogger(__name__)

#: Key used for the one-off kernel request and its reply.
DEVICES_KEY = "device_table"

#: Calls the helper installed in the kernel by ``kernel.HELPERS_CODE``.  It
#: lists *root* devices only: ``oregistry.all_devices`` also contains every
#: sub-component, which runs to a few thousand entries and is not what "the
#: devices" means to anyone reading this tab.
DEVICES_EXPR = "_gui_device_table()"

COLUMNS = ["Name", "Class", "Prefix", "Labels", "Connected"]


class DevicesTab(BaseTab):
    """Table of every device the session created."""

    title = "Devices"

    def __init__(self, parent=None):
        """Build an empty table with a filter box and a refresh button."""
        super().__init__(parent)
        self._rows = []

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by name, class, prefix or label…")
        self._filter.textChanged.connect(self._apply_filter)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh)

        self._status = QLabel("Waiting for the session…")

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        top = QHBoxLayout()
        top.addWidget(self._filter, 1)
        top.addWidget(self._refresh_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._table)
        layout.addWidget(self._status)

    def refresh(self):
        """Ask the kernel for the device listing on the next idle poll."""
        self._status.setText("Refreshing…")
        self.request({DEVICES_KEY: DEVICES_EXPR})

    def on_kernel_state(self, state):
        """Reload the listing whenever a fresh session becomes usable."""
        if state == "idle" and not self._rows:
            self.refresh()
        elif state == "dead":
            self._status.setText("Kernel is not running.")

    def on_kernel_values(self, values):
        """Populate the table from a device listing reply."""
        rows = values.get(DEVICES_KEY)
        if not isinstance(rows, list):
            return
        self._rows = rows
        self._populate()

    def _populate(self):
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._rows))
        for row, entry in enumerate(self._rows):
            name, cls, prefix, labels, connected = entry
            if connected is True:
                conn = "yes"
            elif connected is False:
                conn = "NO"
            else:
                conn = "—"
            for column, text in enumerate((name, cls, prefix, labels, conn)):
                item = QTableWidgetItem(str(text))
                if column == 4 and connected is False:
                    item.setForeground(Qt.red)
                self._table.setItem(row, column, item)
        self._table.setSortingEnabled(True)
        self._apply_filter(self._filter.text())

    def _apply_filter(self, text):
        needle = text.strip().lower()
        shown = 0
        for row in range(self._table.rowCount()):
            haystack = " ".join(
                self._table.item(row, column).text().lower()
                for column in range(self._table.columnCount())
                if self._table.item(row, column) is not None
            )
            hidden = bool(needle) and needle not in haystack
            self._table.setRowHidden(row, hidden)
            shown += not hidden
        total = self._table.rowCount()
        if needle:
            self._status.setText(f"{shown} of {total} devices match “{text.strip()}”.")
        else:
            self._status.setText(f"{total} devices.")
