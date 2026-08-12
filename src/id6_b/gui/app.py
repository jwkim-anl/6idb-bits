"""Main window and entry point for the 6-ID-B Bluesky GUI.

Layout is a vertical splitter: parameter tabs on top, a full IPython console
below.  The console is a real Jupyter front end, so everything that works in
``ipython -i -c "from id6_b.startup import *"`` works here too.

Run with the ``id6b-gui`` console script.
"""

import argparse
import logging
import os
import sys

from qtconsole.rich_jupyter_widget import RichJupyterWidget
from qtpy.QtCore import QSettings
from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QApplication
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QMainWindow
from qtpy.QtWidgets import QMessageBox
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QScrollArea
from qtpy.QtWidgets import QSizePolicy
from qtpy.QtWidgets import QSpinBox
from qtpy.QtWidgets import QSplitter
from qtpy.QtWidgets import QTabWidget
from qtpy.QtWidgets import QToolBar
from qtpy.QtWidgets import QWidget

from .docstream import DocumentStream
from .kernel import KernelSession
from .kernel import StatusPoller
from .tabs.detectors import DetectorsTab
from .tabs.devices import DevicesTab
from .tabs.hkl import HklTab
from .tabs.macro import MacroTab
from .tabs.scan import ScanTab
from .tabs.scanplot import ScanPlotTab
from .tabs.status import StatusTab

logger = logging.getLogger(__name__)

#: Tabs shown in the upper pane, in order.  Add new BaseTab subclasses here.
TABS = [
    StatusTab,
    ScanTab,
    MacroTab,
    ScanPlotTab,
    HklTab,
    DetectorsTab,
    DevicesTab,
]

#: Console font size limits, in points.
MIN_FONT_POINTS = 6
MAX_FONT_POINTS = 32

#: Used only when Qt reports a pixel-sized font, so there is no point size to
#: adopt as the starting value.
FALLBACK_FONT_POINTS = 10

#: Console background choices, mapped to qtconsole ``set_default_style``
#: schemes.  "black" is the default: a dark console is easier on the eyes in a
#: dimmed hutch, and it matches the terminal sessions already in use.
BACKGROUNDS = {"black": "linux", "white": "lightbg"}
DEFAULT_BACKGROUND = "black"

#: Where console appearance choices are remembered between sessions.
SETTINGS_ORG = "APS"
SETTINGS_APP = "id6b-gui"
FONT_SIZE_KEY = "console/font_size"
BACKGROUND_KEY = "console/background"

_STATE_TEXT = {
    "idle": "kernel: idle",
    "busy": "kernel: busy",
    "dead": "kernel: not running",
}


class MainWindow(QMainWindow):
    """Bluesky session window: parameter tabs above, IPython console below."""

    def __init__(self, cwd, parent=None):
        """Start a kernel in *cwd* and build the window around it."""
        super().__init__(parent)
        self.setWindowTitle("6-ID-B Bluesky")
        self.resize(1100, 900)

        self.session = KernelSession(cwd)
        manager, client = self.session.start()

        self.console = self._build_console(manager, client)
        self.tabs, self._tab_widgets = self._build_tabs()

        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.addWidget(self.tabs)
        self._splitter.addWidget(self.console)
        # Equal halves, both resizable; the user can drag the divider.
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([450, 450])
        self._even_split_done = False
        self.setCentralWidget(self._splitter)

        self._build_toolbar()

        # Live plotting happens here, in the GUI process; the kernel only
        # publishes documents.  See docstream.py for why it cannot draw itself.
        self.docstream = DocumentStream(parent=self)
        self.docstream.document.connect(self._on_document)
        self.docstream.start()

        self.poller = StatusPoller(self.session, parent=self)
        # Lets a tab request an occasional one-off expression, and run code
        # visibly in the console for anything with real consequence (see
        # BaseTab).
        for tab in self._tab_widgets:
            tab.poller = self.poller
            tab.console_execute = self.console.execute
            tab.session_cwd = cwd
        self.poller.metadata_changed.connect(self._on_metadata)
        self.poller.kernel_values_changed.connect(self._on_kernel_values)
        self.poller.kernel_state_changed.connect(self._on_kernel_state)

        # No kernel_restarted connection: that signal only fires for an
        # autorestart, which is disabled.  Deliberate restarts drive the
        # bootstrap from _do_restart() via the same ready handshake.
        self.session.ready.connect(self._on_kernel_ready)
        self.session.wait_until_ready()

    def _on_kernel_ready(self):
        """Bootstrap Bluesky and begin polling, once the kernel can answer."""
        self.session.bootstrap(self.console, self.docstream.publisher_code())
        self.poller.start()
        self.restart_button.setEnabled(True)

    def _on_document(self, name, doc):
        """Forward a streamed document to every tab that wants one."""
        for tab in self._tab_widgets:
            handler = getattr(tab, "on_document", None)
            if handler is not None:
                handler(name, doc)

    def _build_console(self, manager, client):
        console = RichJupyterWidget()
        console.kernel_manager = manager
        console.kernel_client = client
        # The toolbar button runs its own confirmation dialog.
        console.confirm_restart = False
        console.clear_on_kernel_restart = True

        # Apply the background before the window is shown, so a dark console
        # never flashes white on startup.
        self._background = self._saved_background()
        console.set_default_style(BACKGROUNDS[self._background])
        return console

    def _build_tabs(self):
        tabs = QTabWidget()
        widgets = []
        for tab_class in TABS:
            tab = tab_class()
            if tab.scrollable:
                # Without this the tallest page sets the tab widget's minimum
                # height (595 px with these tabs) and the splitter can never
                # give the console its half.
                holder = QScrollArea()
                holder.setWidget(tab)
                holder.setWidgetResizable(True)
                holder.setFrameShape(QScrollArea.NoFrame)
                tabs.addTab(holder, tab.title)
            else:
                tabs.addTab(tab, tab.title)
            widgets.append(tab)
        # Let the splitter shrink the whole stack; each page scrolls instead.
        tabs.setMinimumHeight(120)
        return tabs, widgets

    def _build_toolbar(self):
        toolbar = QToolBar("Session")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.restart_button = QPushButton("Restart Bluesky")
        self.restart_button.clicked.connect(self._on_restart_clicked)
        # Enabled by _on_kernel_ready(); restarting a kernel that has not
        # finished starting leaves the session in a confusing half state.
        self.restart_button.setEnabled(False)
        toolbar.addWidget(self.restart_button)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Console font: "))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(MIN_FONT_POINTS, MAX_FONT_POINTS)
        self.font_spin.setSuffix(" pt")
        self.font_spin.setToolTip(
            "Console font size.  Ctrl+= / Ctrl+- inside the console work too."
        )
        self.font_spin.setValue(self._initial_font_points())
        self.font_spin.valueChanged.connect(self._on_font_spin_changed)
        toolbar.addWidget(self.font_spin)
        # Keep the spin box honest when the size is changed from the console
        # itself via Ctrl+= / Ctrl+-.
        self.console.font_changed.connect(self._on_console_font_changed)

        toolbar.addWidget(QLabel("  Background: "))
        self.background_combo = QComboBox()
        self.background_combo.addItem("Black", "black")
        self.background_combo.addItem("White", "white")
        self.background_combo.setToolTip("Console background colour.")
        index = self.background_combo.findData(self._background)
        self.background_combo.setCurrentIndex(max(0, index))
        self.background_combo.currentIndexChanged.connect(self._on_background_changed)
        toolbar.addWidget(self.background_combo)

        # Push the state readout to the right-hand end of the toolbar.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        self.state_label = QLabel("kernel: starting…")
        toolbar.addWidget(self.state_label)

    def _saved_background(self):
        """Return the remembered background name, defaulting to black."""
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        name = settings.value(BACKGROUND_KEY, DEFAULT_BACKGROUND, type=str)
        return name if name in BACKGROUNDS else DEFAULT_BACKGROUND

    def _on_background_changed(self, index):
        """Switch the console background and remember the choice."""
        name = self.background_combo.itemData(index)
        if name not in BACKGROUNDS:
            return
        self._background = name
        # Changing the style sheet does not disturb the font size.
        self.console.set_default_style(BACKGROUNDS[name])
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(BACKGROUND_KEY, name)

    def _initial_font_points(self):
        """Return the remembered console font size, or the current one."""
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        saved = settings.value(FONT_SIZE_KEY, 0, type=int)
        if MIN_FONT_POINTS <= saved <= MAX_FONT_POINTS:
            self._apply_font_points(saved)
            return saved
        current = self.console.font.pointSize()
        if current > 0:
            return current
        # pointSize() is -1 when Qt resolved a pixel-sized desktop font.  Pin a
        # real point size rather than just displaying one, so the spin box and
        # the console cannot disagree about the current size.
        self._apply_font_points(FALLBACK_FONT_POINTS)
        return FALLBACK_FONT_POINTS

    def _apply_font_points(self, points):
        """Set the console font size, leaving the family alone."""
        font = self.console.font
        if font.pointSize() == points:
            return
        font.setPointSize(points)
        self.console.font = font

    def _on_font_spin_changed(self, points):
        """Apply and remember a font size chosen in the toolbar."""
        self._apply_font_points(points)
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(FONT_SIZE_KEY, points)

    def _on_console_font_changed(self):
        """Mirror a console-side font change back into the spin box."""
        points = self.console.font.pointSize()
        if points <= 0 or points == self.font_spin.value():
            return
        # Guard against bouncing back into _on_font_spin_changed.
        blocked = self.font_spin.blockSignals(True)
        self.font_spin.setValue(points)
        self.font_spin.blockSignals(blocked)
        QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(FONT_SIZE_KEY, points)

    def _on_metadata(self, metadata):
        for tab in self._tab_widgets:
            tab.on_metadata(metadata)

    def _on_kernel_values(self, values):
        for tab in self._tab_widgets:
            tab.on_kernel_values(values)

    def _on_kernel_state(self, state):
        self.state_label.setText(_STATE_TEXT.get(state, f"kernel: {state}"))
        for tab in self._tab_widgets:
            tab.on_kernel_state(state)

    def _on_restart_clicked(self):
        answer = QMessageBox.question(
            self,
            "Restart Bluesky",
            "Restart the Bluesky kernel?\n\n"
            "Variables defined in the console will be lost and all EPICS\n"
            "connections will be rebuilt.  This takes roughly 40 seconds.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.restart_button.setEnabled(False)
        self.state_label.setText("kernel: restarting…")
        # Deferred so the disabled button and label repaint before
        # restart_kernel() blocks for a second or two spawning the process.
        QTimer.singleShot(0, self._do_restart)

    def _do_restart(self):
        """Restart the kernel; _on_kernel_ready() re-runs the bootstrap.

        The poller is stopped for the duration so that only the readiness
        handshake is reading the shell channel.
        """
        self.poller.stop()
        self.poller.reset()
        self.session.restart()
        # Autorestart is off, so qtconsole does not clear the console for us.
        self.console.reset(clear=True)
        self.session.wait_until_ready()

    def showEvent(self, event):  # noqa: N802 - Qt naming
        """Split the window evenly once the real height is known.

        setSizes() before the first show is measured against size hints, not
        the final geometry, so the even split has to be (re)applied here.
        """
        super().showEvent(event)
        if not self._even_split_done:
            self._even_split_done = True
            half = max(1, self._splitter.height() // 2)
            self._splitter.setSizes([half, half])

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Stop polling and shut the kernel down so none is left orphaned."""
        self.poller.stop()
        self.docstream.stop()
        for tab in self._tab_widgets:
            shutdown = getattr(getattr(tab, "model3d", None), "shutdown", None)
            if shutdown is not None:
                shutdown()
        try:
            self.console.kernel_client = None
        except Exception:  # noqa: BLE001 - best effort during teardown
            logger.debug("Could not detach console client.", exc_info=True)
        self.session.shutdown()
        super().closeEvent(event)


def main(argv=None):
    """Entry point for the ``id6b-gui`` console script."""
    parser = argparse.ArgumentParser(
        prog="id6b-gui",
        description="Qt interface for the 6-ID-B Bluesky session.",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help=(
            "Working directory for the kernel.  Determines where "
            ".re_md_dict.yml and data files are written (default: current "
            "directory)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("6-ID-B Bluesky")

    window = MainWindow(cwd=args.cwd)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
