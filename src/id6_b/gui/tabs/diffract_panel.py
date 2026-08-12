"""3D diffractometer panel for the right-hand side of the HKL tab.

Shows either the *current* angles or the ones from the last Calculate, with
optional overlays for the scattering plane, the psi reference vector and Q.

Degrades to a message if vtk/pyvista/pyvistaqt are missing, so the GUI still
runs without them.
"""

import logging

import numpy as np
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QRadioButton
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from ..diffract3d import AVAILABLE
from ..diffract3d import INSTALL_HINT
from ..diffract3d import Diffractometer

logger = logging.getLogger(__name__)

CURRENT = "current"
CALCULATED = "calculated"


class Diffract3DPanel(QWidget):
    """The 3D view plus its source selector and overlay toggles."""

    def __init__(self, parent=None):
        """Build the panel, or an explanatory placeholder if pyvista is absent."""
        super().__init__(parent)
        self._model = None
        self._plotter = None
        self._current = dict.fromkeys(("mu", "eta", "chi", "phi", "nu", "delta"), 0.0)
        self._calculated = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not AVAILABLE:
            note = QLabel(INSTALL_HINT)
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignTop)
            note.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(note)
            layout.addStretch(1)
            self._status = QLabel()
            return

        from pyvistaqt import QtInteractor

        self._plotter = QtInteractor(self)
        self._model = Diffractometer()
        self._model.build(self._plotter)
        self._plotter.set_background("#101010")
        self._plotter.view_isometric()
        # Keep the view usable when the tab only has half the window; the
        # splitter still lets it be dragged larger.
        self._plotter.interactor.setMinimumSize(260, 240)
        layout.addWidget(self._plotter.interactor, 1)
        layout.addWidget(self._build_controls())

        self._status = QLabel("Showing current position.")
        layout.addWidget(self._status)

    def _build_controls(self):
        box = QGroupBox("3D view")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._current_radio = QRadioButton("Current")
        self._current_radio.setChecked(True)
        self._current_radio.toggled.connect(lambda _s: self._refresh())
        self._calculated_radio = QRadioButton("Calculated")
        self._calculated_radio.toggled.connect(lambda _s: self._refresh())
        row.addWidget(self._current_radio)
        row.addWidget(self._calculated_radio)
        row.addStretch(1)
        reset = QPushButton("Reset view")
        reset.clicked.connect(self._reset_view)
        row.addWidget(reset)
        outer.addLayout(row)

        toggles = QHBoxLayout()
        self._plane_box = QCheckBox("Scattering plane")
        self._plane_box.toggled.connect(self._toggle_plane)
        toggles.addWidget(self._plane_box)
        self._ref_box = QCheckBox("ψ reference")
        self._ref_box.toggled.connect(self._toggle_ref)
        toggles.addWidget(self._ref_box)
        self._q_box = QCheckBox("Q vector")
        self._q_box.toggled.connect(self._toggle_q)
        toggles.addWidget(self._q_box)
        toggles.addStretch(1)
        outer.addLayout(toggles)
        return box

    # -- inputs from the HKL tab -----------------------------------------

    def set_current(self, reals):
        """Update the live angles."""
        if not reals:
            return
        self._current = {k: float(v) for k, v in reals.items() if v is not None}
        if self._showing_current():
            self._refresh()

    def set_calculated(self, reals):
        """Update the angles from the last successful Calculate."""
        self._calculated = (
            {k: float(v) for k, v in reals.items() if v is not None} if reals else {}
        )
        if not self._showing_current():
            self._refresh()

    def set_orientation(self, ub, reference):
        """Set UB and the psi reference (h2, k2, l2) for the reference arrow."""
        if self._model is None:
            return
        try:
            matrix = np.array(ub, dtype=float)
            if matrix.shape == (3, 3):
                self._model.UB = matrix
        except Exception:  # noqa: BLE001 - a malformed UB must not kill the tab
            logger.debug("Ignoring malformed UB.", exc_info=True)
        if reference:
            self._model.hkl = np.array(
                [float(reference.get(k, 0.0) or 0.0) for k in ("h2", "k2", "l2")]
            )
        self._refresh()

    # -- view -------------------------------------------------------------

    def _showing_current(self):
        return self._current_radio.isChecked() if self._model else True

    def _refresh(self):
        if self._model is None:
            return
        if self._showing_current():
            angles, label = self._current, "current position"
        else:
            angles, label = self._calculated, "calculated position"
        if not angles:
            self._status.setText(f"No {label} yet.")
            return
        for axis in self._model.angles:
            if axis in angles:
                self._model.angles[axis] = angles[axis]
        self._model.update_transforms()
        self._plotter.update()
        self._status.setText(
            f"Showing {label}: "
            + "  ".join(f"{k}={self._model.angles[k]:.3f}" for k in ("delta", "nu"))
        )

    def _toggle_plane(self, checked):
        if self._model is None:
            return
        self._model.scatter_plane_visible = bool(checked)
        self._model.update_scatter_plane()
        self._plotter.update()

    def _toggle_ref(self, checked):
        if self._model is None:
            return
        self._model.ref_vec_visible = bool(checked)
        self._model.update_ref_vec()
        self._plotter.update()
        # UB @ (0,0,0) has no direction, so the arrow stays hidden.  Say so,
        # rather than leaving a ticked box with nothing on screen.
        if checked and not self._model.ref_vec_actor.visibility:
            self._status.setText(
                "ψ reference is 0 0 0 — set h2/k2/l2 below to show the arrow."
            )
        else:
            self._refresh()

    def _toggle_q(self, checked):
        if self._model is None:
            return
        self._model.q_vec_visible = bool(checked)
        self._model.update_q_vec()
        self._plotter.update()

    def _reset_view(self):
        if self._plotter is not None:
            self._plotter.view_isometric()
            self._plotter.reset_camera()

    def shutdown(self):
        """Release the VTK render window; leaking it can hang exit."""
        if self._plotter is not None:
            try:
                self._plotter.close()
            except Exception:  # noqa: BLE001 - best effort during teardown
                logger.debug("Plotter close failed.", exc_info=True)
            self._plotter = None
