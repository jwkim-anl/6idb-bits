"""Session status tab: catalog, user, scan number, output files."""

from qtpy.QtWidgets import QFormLayout
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QVBoxLayout

from .base import PLACEHOLDER
from .base import BaseTab
from .base import value_label

#: Kernel states that mean the RunEngine cannot be queried right now.
_BUSY_STATES = ("busy", "dead")


class StatusTab(BaseTab):
    """Overview of the current Bluesky session."""

    title = "Session"

    def __init__(self, parent=None):
        """Build the Session, Scan and Files field groups."""
        super().__init__(parent)

        self._fields = {}
        self._re_state = None
        self._kernel_state = None

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
        layout.addStretch(1)

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
