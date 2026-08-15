"""Agent tab: approve or reject the moves an LLM asks for.

An MCP client can set up an orientation, ask for a move, ask for a scan -- but
it cannot make any of it happen.  A motion op in ``mcp_server/motion.py``
validates the request and *parks* it; this tab is where a human sees it and
decides.  The invariant the whole feature rests on:

    The MCP layer can only ever request.  The only process that emits motion
    is the GUI, and only from a human click or an operator-armed auto window.

So Approve does not do anything clever: it runs ``_gui_mcp_execute(token)``
through the console, the same path the HKL tab's Move button uses.  The move is
an ordinary console command -- in the history, in the transcript, on the
session RunEngine, interruptible with Ctrl-C.

Three controls beyond Approve/Reject:

``Allow LLM moves without asking``
    Hands over the keys for a stretch of work.  Time-boxed, with a live
    countdown, unchecked at construction and unchecked again on a kernel
    restart, so it can never be inherited from an earlier session.  An
    auto-approval goes through the *same* ``_gui_mcp_execute`` call, so there
    is one code path for motion rather than two.

``Refuse all motion requests``
    The master off switch.  Enforced kernel-side (``_gui_mcp_set_blocked``) so
    a request is declined with a sentence the model can act on, rather than
    sitting unanswered until someone comes back.

History
    The last outcomes, so "what did it do while I was at lunch" has an answer
    that does not need the transcript.

State arrives with the ordinary 1 Hz poll (``mcp_pending`` in
``KERNEL_EXPRESSIONS``), so the tab adds no reader of its own to the shell
channel.
"""

import logging
import time

from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtCore import Signal
from qtpy.QtWidgets import QCheckBox
from qtpy.QtWidgets import QGroupBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QListWidget
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QSpinBox
from qtpy.QtWidgets import QVBoxLayout

from .base import PLACEHOLDER
from .base import BaseTab
from .base import value_label

logger = logging.getLogger(__name__)

#: Key the poller delivers ``_gui_mcp_pending_info()`` under.
PENDING_KEY = "mcp_pending"

#: Auto mode's window, in minutes.  Long enough to cover a run of alignment
#: moves, short enough that walking away ends it.
DEFAULT_AUTO_MINUTES = 30
MIN_AUTO_MINUTES = 1
MAX_AUTO_MINUTES = 240

#: Countdown tick.  Only runs while auto mode is armed.
AUTO_TICK_MS = 1000

#: Shown in the card when nothing is waiting.
IDLE_TEXT = "No request waiting."


class AgentTab(BaseTab):
    """Pending LLM request, the approval controls, and what happened before."""

    title = "Agent"

    #: Emitted when a request the operator has not seen yet turns up, so the
    #: window can raise this tab.  A move waiting behind another tab is a move
    #: nobody approves.
    request_arrived = Signal()

    def __init__(self, parent=None):
        """Build the card, the controls and the history list."""
        super().__init__(parent)

        self._kernel_state = "idle"
        self._pending = None
        #: Tokens an approval has already been sent for.  The console runs
        #: asynchronously, so the next poll can still show a request that is
        #: about to start; without this, auto mode would approve it twice.
        self._acted = set()
        self._auto_until = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_request_group())
        layout.addWidget(self._build_controls_group())
        layout.addWidget(self._build_history_group())
        layout.addStretch(1)

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_TICK_MS)
        self._auto_timer.timeout.connect(self._tick_auto)

        self._show_pending(None)

    # ------------------------------------------------------------------ build

    def _build_request_group(self):
        group = QGroupBox("Pending request")
        box = QVBoxLayout(group)

        self.summary_label = QLabel(IDLE_TEXT)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.summary_label)

        self.details_label = value_label("")
        self.details_label.setWordWrap(True)
        box.addWidget(self.details_label)

        self.command_label = value_label("")
        self.command_label.setWordWrap(True)
        box.addWidget(self.command_label)

        self.origin_label = QLabel("")
        self.origin_label.setWordWrap(True)
        box.addWidget(self.origin_label)

        buttons = QHBoxLayout()
        self.approve_button = QPushButton("Approve")
        self.approve_button.clicked.connect(self._on_approve)
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(self._on_reject)
        buttons.addWidget(self.approve_button)
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        box.addLayout(buttons)

        return group

    def _build_controls_group(self):
        group = QGroupBox("Who may move the instrument")
        box = QVBoxLayout(group)

        row = QHBoxLayout()
        self.auto_check = QCheckBox("Allow LLM moves without asking, for")
        self.auto_check.setToolTip(
            "Approve requests as they arrive, for the window below.\n"
            "Off at startup and after a kernel restart -- it is never inherited."
        )
        self.auto_check.toggled.connect(self._on_auto_toggled)
        row.addWidget(self.auto_check)

        self.auto_minutes = QSpinBox()
        self.auto_minutes.setRange(MIN_AUTO_MINUTES, MAX_AUTO_MINUTES)
        self.auto_minutes.setValue(DEFAULT_AUTO_MINUTES)
        self.auto_minutes.setSuffix(" min")
        row.addWidget(self.auto_minutes)

        self.countdown_label = value_label("")
        row.addWidget(self.countdown_label)
        row.addStretch(1)
        box.addLayout(row)

        self.blocked_check = QCheckBox("Refuse all motion requests")
        self.blocked_check.setToolTip(
            "Requests are declined in the kernel with a sentence the client\n"
            "can act on, rather than waiting here for an answer."
        )
        self.blocked_check.toggled.connect(self._on_blocked_toggled)
        box.addWidget(self.blocked_check)

        caption = QLabel(
            "Setup (sample, lattice, UB, mode) never waits for approval; only "
            "moves and scans do. Approving runs the command in the console, so "
            "Ctrl-C stops it."
        )
        caption.setWordWrap(True)
        box.addWidget(caption)

        return group

    def _build_history_group(self):
        group = QGroupBox("Recent requests")
        box = QVBoxLayout(group)
        self.history_list = QListWidget()
        self.history_list.setMinimumHeight(120)
        box.addWidget(self.history_list)
        return group

    # ------------------------------------------------------------------ hooks

    def on_kernel_values(self, values):
        """Take the parked request, the history and the master switch."""
        info = values.get(PENDING_KEY)
        if not isinstance(info, dict):
            return

        blocked = bool(info.get("blocked"))
        if blocked != self.blocked_check.isChecked():
            # The kernel is the authority: a client cannot change this, but a
            # restart resets it, and the box must not claim otherwise.
            blocked_signals = self.blocked_check.blockSignals(True)
            self.blocked_check.setChecked(blocked)
            self.blocked_check.blockSignals(blocked_signals)

        self._show_history(info.get("history") or [])

        pending = info.get("pending")
        if not isinstance(pending, dict):
            pending = None
        token = pending.get("token") if pending else None
        arrived = token is not None and token not in self._acted
        was = self._pending.get("token") if self._pending else None
        self._show_pending(pending)

        if arrived and token != was:
            self.request_arrived.emit()
        if arrived and self._auto_live():
            self._approve(pending, "auto")

    def on_kernel_state(self, state):
        """Approve/Reject need an idle kernel: a busy one queues the command."""
        self._kernel_state = state
        self._update_buttons()

    # ----------------------------------------------------------------- render

    def _show_pending(self, pending):
        self._pending = pending
        if pending is None:
            self.summary_label.setText(IDLE_TEXT)
            self.details_label.setText("")
            self.command_label.setText("")
            self.origin_label.setText("")
            self._update_buttons()
            return

        device = pending.get("device") or PLACEHOLDER
        self.summary_label.setText(
            f"<b>{pending.get('summary', '')}</b> &nbsp; ({device})"
        )
        self.details_label.setText("\n".join(pending.get("details") or []))
        self.command_label.setText(pending.get("command", ""))

        origin = f"requested at {pending.get('requested', PLACEHOLDER)}"
        if pending.get("allow_large_move"):
            # Worth saying out loud: the travel cap was lifted by the client,
            # so the usual "it cannot be far" reasoning does not apply.
            origin += "  —  the travel limit was overridden for this request"
        self.origin_label.setText(origin)
        self._update_buttons()

    def _show_history(self, history):
        lines = [
            "{}  {}  —  {}".format(
                entry.get("when", ""),
                entry.get("summary", ""),
                entry.get("outcome", ""),
            )
            for entry in reversed(history)
            if isinstance(entry, dict)
        ]
        if lines == [
            self.history_list.item(row).text()
            for row in range(self.history_list.count())
        ]:
            return  # unchanged; redrawing would fight the scroll position
        self.history_list.clear()
        self.history_list.addItems(lines)

    def _update_buttons(self):
        idle = self._kernel_state == "idle"
        waiting = self._pending is not None
        acted = waiting and self._pending.get("token") in self._acted
        enabled = waiting and idle and not acted
        self.approve_button.setEnabled(enabled)
        self.reject_button.setEnabled(waiting and not acted)
        if not waiting:
            reason = "Nothing is waiting for approval."
        elif acted:
            reason = "Already answered; waiting for the kernel to catch up."
        elif not idle:
            reason = f"The kernel is {self._kernel_state}; try again when idle."
        else:
            reason = self._pending.get("command", "")
        self.approve_button.setToolTip(reason)
        self.reject_button.setToolTip(reason)

    # ---------------------------------------------------------------- actions

    def _on_approve(self):
        if self._pending is not None:
            self._approve(self._pending, "operator")

    def _approve(self, pending, who):
        """Run the parked request through the console.

        The command goes out as a trailing comment so the console and the
        transcript say what was approved; the re-validation stays kernel-side,
        where a caller cannot skip it.
        """
        token = pending.get("token")
        if not token:
            return
        command = pending.get("command", "")
        code = f"_gui_mcp_execute({token!r})"
        if command:
            code += f"  # {command}"
        if not self.run_in_console(code):
            logger.warning("No console to approve %s in.", token)
            return
        self._acted.add(token)
        logger.info("Approved %s (%s): %s", token, who, command)
        self._update_buttons()

    def _on_reject(self):
        if self._pending is None:
            return
        token = self._pending.get("token")
        self._acted.add(token)
        self._execute_once(
            f"_gui_mcp_cancel({token!r}, 'rejected by the operator')",
        )
        self._update_buttons()

    def _on_blocked_toggled(self, checked):
        self._execute_once(f"_gui_mcp_set_blocked({bool(checked)!r})")
        if checked and self.auto_check.isChecked():
            # The two together would mean "approve everything, refuse
            # everything"; refusing wins, so say so by dropping auto mode.
            self.auto_check.setChecked(False)

    def _execute_once(self, code):
        if self.poller is None:
            return False
        return self.poller.execute_once(code)

    # -------------------------------------------------------------- auto mode

    def _auto_live(self):
        return self.auto_check.isChecked() and self._auto_until is not None

    def _on_auto_toggled(self, checked):
        if checked:
            self._auto_until = time.monotonic() + 60 * self.auto_minutes.value()
            self.auto_minutes.setEnabled(False)
            self._auto_timer.start()
            self._tick_auto()
        else:
            self._auto_until = None
            self._auto_timer.stop()
            self.auto_minutes.setEnabled(True)
            self.countdown_label.setText("")

    def _tick_auto(self):
        if self._auto_until is None:
            return
        left = int(round(self._auto_until - time.monotonic()))
        if left <= 0:
            self.countdown_label.setText("window closed")
            self.auto_check.setChecked(False)
            return
        self.countdown_label.setText(f"{left // 60:d}:{left % 60:02d} left")

    def clear_auto_mode(self):
        """Drop auto mode and forget what has been answered.

        Called by the window's restart path: the kernel that had the pending
        request is gone, and an auto window armed against the old session must
        not carry over into the new one.
        """
        self.auto_check.setChecked(False)
        self._acted.clear()
        self._show_pending(None)
        self.history_list.clear()
