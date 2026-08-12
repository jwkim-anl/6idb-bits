"""Jupyter kernel management and status polling for the Bluesky GUI.

Two independent clients talk to one kernel:

* the *console* client, owned by the ``RichJupyterWidget``;
* a *poll* client used only to fill the tabs.

They are kept separate so that polling never appears in the console.
``RichJupyterWidget.include_other_output`` defaults to ``False``, so messages
originating from the poll client's session are filtered out of the display.

Status comes from two sources, because the kernel is not always reachable:

``.re_md_dict.yml``
    Written by ``apstools.utils.StoredDict`` from a *background* thread, so it
    keeps updating while the RunEngine is busy.  This is the scan-safe source.

``user_expressions``
    Evaluated by the kernel, so only usable while the kernel is idle.  Supplies
    the values that never reach the metadata file (SPEC filename, experiment
    paths).
"""

import ast
import logging
import queue
import time
from pathlib import Path

import yaml
from jupyter_client import BlockingKernelClient
from qtconsole.manager import QtKernelManager
from qtpy.QtCore import QObject
from qtpy.QtCore import QTimer
from qtpy.QtCore import Signal

from .hkl_bridge import HKL_HELPERS_CODE

logger = logging.getLogger(__name__)

#: Executed in the kernel to build the session.  Same line the IPython
#: workflow uses, so both front ends share one code path.
BOOTSTRAP_CODE = "from id6_b.startup import *"

#: Helpers defined in the kernel for the GUI to call.  ``user_expressions``
#: evaluates expressions only -- it cannot run a ``try``/``except`` -- so
#: anything that needs error handling per device has to live here.
HELPERS_CODE = '''\
def _gui_device_table():
    """Return (name, class, prefix, labels, connected) for each root device."""
    rows = []
    for _d in sorted(oregistry.root_devices, key=lambda d: d.name):
        try:
            _conn = bool(_d.connected)
        except Exception:
            _conn = None          # lazy/area-detector devices can raise
        rows.append((
            _d.name,
            type(_d).__name__,
            str(getattr(_d, "prefix", "")),
            ",".join(sorted(getattr(_d, "_ophyd_labels_", None) or [])),
            _conn,
        ))
    return rows


def _gui_detector_kinds():
    """Return (device, prefix, channel, kind) for every detector channel.

    Uses the ``plot_signals`` mapping, so any detector implementing the
    CountersClass interface is picked up without changes here.
    """
    rows = []
    for _d in oregistry.findall("detectors", allow_none=True) or []:
        try:
            _signals = getattr(_d, "plot_signals", None) or {}
        except Exception:
            continue          # lazy components can raise when instantiated
        for _name, _sig in _signals.items():
            try:
                _kind = _sig.kind.name
            except Exception:
                _kind = None
            rows.append((_d.name, str(getattr(_d, "prefix", "")), _name, _kind))
    return rows


def _gui_extra_devices():
    """Return the names of devices recorded alongside the detectors."""
    try:
        return [d.name for d in counters.extra_devices]
    except Exception:
        return []


def _gui_addable_devices():
    """Root devices that are neither detectors nor already extras.

    Anything here can be recorded during a scan.  Extras skip
    ``configure_counts_wrapper``, so unlike a real detector they do not need a
    ``preset_monitor`` -- which is why a thermometer or capacitance bridge
    works as an extra but crashes as a detector.
    """
    taken = set()
    try:
        taken |= {d.name for d in counters.detectors}
        taken |= {d.name for d in counters.extra_devices}
    except Exception:
        pass
    rows = []
    for _d in sorted(oregistry.root_devices, key=lambda d: d.name):
        if _d.name in taken:
            continue
        rows.append((_d.name, type(_d).__name__, str(getattr(_d, "prefix", ""))))
    return rows


def _gui_add_extra(name):
    """Record *name* at every scan point (counters.extra_devices)."""
    try:
        current = list(counters.extra_devices)
        if any(d.name == name for d in current):
            return f"{name} is already an extra device."
        counters.extra_devices = current + [oregistry.find(name)]
        return f"Recording {name} during scans."
    except Exception as exc:
        return f"Could not add {name}: {exc}"


def _gui_remove_extra(name):
    """Stop recording *name*."""
    try:
        counters.extra_devices = [
            d for d in counters.extra_devices if d.name != name
        ]
        return f"No longer recording {name}."
    except Exception as exc:
        return f"Could not remove {name}: {exc}"


def _gui_set_kinds(changes):
    """Apply [(device, channel, kind_name), ...]; return what actually stuck."""
    from ophyd import Kind as _Kind

    applied = []
    for _dev, _chan, _kind in changes:
        try:
            _sig = oregistry.find(_dev).plot_signals[_chan]
            _sig.kind = getattr(_Kind, _kind)
            applied.append((_dev, _chan, _sig.kind.name))
        except Exception as _exc:
            print(f"Could not set {_dev}.{_chan} to {_kind}: {_exc}")
    return applied


def _gui_extra_kinds(include_omitted=False):
    """Return (device, prefix, dotted_name, kind) for each extra device signal.

    Extras are arbitrary devices with no ``plot_signals`` mapping, so their
    signals are enumerated with ``walk_signals``.  Omitted signals are skipped
    by default: they are not recorded anyway, and on a motor bundle they are
    the majority (52 of sl1's 100) of motor-record internals.
    """
    rows = []
    try:
        extras = list(counters.extra_devices)
    except Exception:
        return rows
    for _d in extras:
        try:
            walk = list(_d.walk_signals(include_lazy=False))
        except Exception:
            continue
        for _w in walk:
            try:
                _kind = _w.item.kind.name
            except Exception:
                continue
            if _kind == "omitted" and not include_omitted:
                continue
            rows.append((
                _d.name,
                str(getattr(_d, "prefix", "")),
                _w.dotted_name,
                _kind,
            ))
    return rows


def _gui_scan_options():
    """Return the pick lists the Scan tab needs.

    Axes are found by capability, not class, because the classes disagree:
    ``EpicsMotor`` and the hklpy2 pseudo axes are ``PositionerBase``, but
    ``sim_motor`` is a ``SynAxis`` (not a positioner, yet has ``set`` and
    ``position``) and ``energy`` is an ``EnergySignal`` (``set`` but no
    ``position``).  All three are scannable.  Identifiers are dotted paths,
    which are valid Python in this namespace.

    Detectors: only those with ``preset_monitor``.  ``configure_counts_wrapper``
    calls ``rd(det.preset_monitor)`` on every detector, so anything else raises
    ``AttributeError`` when scanned.
    """
    from ophyd import Signal

    def _movable(obj):
        try:
            return callable(getattr(obj, "set", None)) and hasattr(obj, "position")
        except Exception:
            return False

    axes, seen = [], set()

    def _add(obj, path):
        if path not in seen:
            seen.add(path)
            axes.append((path, type(obj).__name__))

    for _d in sorted(oregistry.root_devices, key=lambda d: d.name):
        _has_axis_children = False
        for _attr in getattr(_d, "component_names", ()):
            try:
                _c = getattr(_d, _attr)
            except Exception:
                continue
            if _movable(_c):
                _add(_c, f"{_d.name}.{_attr}")
                _has_axis_children = True
        # A movable with movable children (mono, psic, sl1) is a container:
        # scan mono.energy or psic.h, not the container itself.
        if _movable(_d) and not _has_axis_children:
            _add(_d, _d.name)
        elif isinstance(_d, Signal) and getattr(_d, "write_access", False):
            _add(_d, _d.name)

    candidates = []
    for _d in sorted(oregistry.root_devices, key=lambda d: d.name):
        if hasattr(_d, "preset_monitor"):
            candidates.append(_d.name)

    try:
        selected = [d.name for d in counters.detectors]
        monitor = counters.monitor
    except Exception:
        selected, monitor = [], "Time"

    return {
        "axes": axes,
        "detector_candidates": candidates,
        "detectors_selected": selected,
        "monitor": monitor,
    }


def _gui_macro_targets(name):
    """Return [(dotted_path, class_name, kind)] that ``mv()`` can drive on *name*.

    Two kinds, positioners first because they are what a macro usually moves:

    ``positioner``
        The device itself when it is movable, and its movable children.
        ``walk_signals`` alone is wrong here -- ``mv(sl1.top, 3)`` wants the
        ``EpicsMotor``, not ``sl1.top.user_setpoint``.
    ``signal``
        Every writable signal underneath, for the things that are set rather
        than moved: a temperature setpoint, a source voltage, a filter
        transmission.

    Per device rather than all at once, so the tab never ships the several
    hundred signal names it will not use.
    """
    from ophyd import Signal as _Signal

    def _movable(obj):
        try:
            return callable(getattr(obj, "set", None)) and hasattr(obj, "position")
        except Exception:
            return False

    try:
        _dev = oregistry.find(name)
    except Exception:
        return []

    rows, seen = [], set()

    def _add(path, obj, kind):
        if path not in seen:
            seen.add(path)
            rows.append((path, type(obj).__name__, kind))

    if _movable(_dev):
        _add(_dev.name, _dev, "positioner")
    elif isinstance(_dev, _Signal) and getattr(_dev, "write_access", False):
        _add(_dev.name, _dev, "signal")

    for _attr in getattr(_dev, "component_names", ()):
        try:
            _c = getattr(_dev, _attr)
        except Exception:
            continue
        if _movable(_c):
            _add(f"{_dev.name}.{_attr}", _c, "positioner")

    signals = []
    try:
        walk = list(_dev.walk_signals(include_lazy=False))
    except Exception:
        walk = []
    for _w in walk:
        try:
            if not getattr(_w.item, "write_access", False):
                continue
        except Exception:
            continue        # a disconnected signal can raise on write_access
        signals.append((f"{_dev.name}.{_w.dotted_name}", _w.item))
    for _path, _sig in sorted(signals, key=lambda _row: _row[0]):
        _add(_path, _sig, "signal")
    return rows


def _gui_set_extra_kinds(changes):
    """Apply [(device, dotted_name, kind_name), ...] to extra-device signals."""
    import operator

    from ophyd import Kind as _Kind

    applied = []
    for _dev, _dotted, _kind in changes:
        try:
            _sig = operator.attrgetter(_dotted)(oregistry.find(_dev))
            _sig.kind = getattr(_Kind, _kind)
            applied.append((_dev, _dotted, _sig.kind.name))
        except Exception as _exc:
            print(f"Could not set {_dev}.{_dotted} to {_kind}: {_exc}")
    return applied
'''

#: RunEngine metadata autosave, resolved against the kernel's working
#: directory (``RUN_ENGINE.MD_PATH`` in ``iconfig.yml``).
MD_FILENAME = ".re_md_dict.yml"

POLL_INTERVAL_MS = 1000

#: Evaluated in the kernel when it is idle.  Each entry reports its own
#: ``status``, so one failing expression never blanks the others --
#: ``experiment.experiment_path`` raises until ``experiment_setup()`` has run,
#: which is the normal state at session start.
KERNEL_EXPRESSIONS = {
    "re_state": "RE.state",
    "spec_file": "str(specwriter.spec_filename)",
    "sample": "experiment.sample",
    "exp_path": "str(experiment.experiment_path)",
    "n_runs": "len(cat)",
    "cwd": "__import__('os').getcwd()",
}


def _from_repr(text):
    """Convert a ``user_expressions`` repr back into a Python value."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


class KernelSession(QObject):
    """Own the kernel process and the two clients that talk to it."""

    #: Emitted once the kernel answers ``kernel_info``, i.e. it is safe to
    #: execute code in it.
    ready = Signal()

    def __init__(self, cwd, parent=None):
        """Prepare a session that will run its kernel in *cwd*."""
        super().__init__(parent)
        self.cwd = str(cwd)
        self.manager = None
        self.client = None
        self.poll_client = None
        self._ready_timer = None
        self._ready_deadline = 0.0
        self._ready_last_request = 0.0

    def start(self):
        """Start the kernel and both clients.

        Returns the ``(manager, client)`` pair for the console widget.
        """
        self.manager = QtKernelManager(kernel_name="python3")
        # Autorestart off, deliberately.  A silent restart would drop every
        # EPICS connection and device mid-experiment with only a log line to
        # show for it; at a beamline a visibly dead kernel the user restarts on
        # purpose is safer.  It also stops qtconsole's restart handler from
        # resetting the console on a spurious "kernel died" poll at startup.
        self.manager.autorestart = False
        # cwd matters: the RunEngine writes .re_md_dict.yml relative to it,
        # and the poller reads that same path back.
        self.manager.start_kernel(cwd=self.cwd)

        self.client = self.manager.client()
        self.client.start_channels()

        # Deliberately does NOT wait_for_ready(): the bootstrap takes ~40 s and
        # blocking here would freeze the GUI before it is even shown.  The
        # poller tolerates a kernel that is not answering yet.
        self.poll_client = BlockingKernelClient()
        self.poll_client.load_connection_file(self.manager.connection_file)
        self.poll_client.start_channels()

        return self.manager, self.client

    def wait_until_ready(self, timeout_s=180.0):
        """Emit :attr:`ready` once the kernel answers, without blocking.

        Executing before the kernel has announced itself is what makes
        qtconsole misread the kernel's own ``status: starting`` message as a
        crash-restart (``_handle_status`` calls ``_handle_kernel_restarted``
        whenever that arrives while the widget is executing).  Waiting for a
        ``kernel_info_reply`` first avoids the race entirely.

        Uses the poll client, so call this before the status poller starts to
        keep one reader on the shell channel.
        """
        self._ready_deadline = time.monotonic() + timeout_s
        self._ready_last_request = 0.0
        if self._ready_timer is None:
            self._ready_timer = QTimer(self)
            self._ready_timer.setInterval(200)
            self._ready_timer.timeout.connect(self._check_ready)
        self._ready_timer.start()

    def _check_ready(self):
        client = self.poll_client
        if client is None:
            return
        now = time.monotonic()
        # Re-ask periodically: a request sent while the kernel was still
        # binding its sockets is simply lost.
        if now - self._ready_last_request > 2.0:
            self._ready_last_request = now
            try:
                client.kernel_info()
            except Exception:  # noqa: BLE001 - kernel not up yet
                logger.debug("kernel_info request failed.", exc_info=True)
        while True:
            try:
                msg = client.get_shell_msg(timeout=0)
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001 - kernel not up yet
                break
            if msg.get("msg_type") == "kernel_info_reply":
                self._ready_timer.stop()
                self.ready.emit()
                return
        if now > self._ready_deadline:
            self._ready_timer.stop()
            logger.warning("Kernel never reported ready; continuing anyway.")
            self.ready.emit()

    def bootstrap(self, console, follow_up_code=None):
        """Run the Bluesky startup in the kernel, its output shown in *console*.

        Sent on the console's *own* client rather than through
        ``console.execute()``.  The cell is several hundred lines of GUI helper
        definitions, and ``execute()`` echoes all of it into the console and
        spends prompt ``In [1]`` on it, so the user's first command starts at
        ``[2]``.  ``silent=True`` suppresses the ``execute_input`` broadcast and
        leaves the execution counter alone: nothing is echoed and the session
        opens at ``In [1]``.

        This is *not* qtconsole's hidden execute -- ``console.execute(source,
        hidden=True)`` sets ``_hidden``, which also swallows every ``stream``
        and ``error`` message for the request.  Sending on the client directly
        means the widget never registers the request, so ``_hidden`` stays
        False and the device-loading log and any traceback still appear,
        inserted above the prompt the way background output is.

        It must be the console's client and not the poll client: separate
        sockets give no ordering guarantee, so *follow_up_code* (the live-plot
        publisher subscription) could arrive before the import and fail on a
        missing ``RE``.  It is appended to this same cell for that reason.
        """
        parts = [BOOTSTRAP_CODE, HELPERS_CODE, HKL_HELPERS_CODE]
        if follow_up_code:
            parts.append(follow_up_code)

        # The prompt is live while this runs, where ``console.execute()`` used
        # to block it, so say what is happening: a command typed now is queued
        # by the kernel and runs once the startup finishes -- which for the
        # first ~40 s means before the devices exist.
        notice = getattr(console, "append_stream", None)
        if notice is not None:
            notice(
                "Starting the Bluesky session. Anything typed before it "
                "finishes will run afterwards.\n"
            )

        console.kernel_client.execute(
            "\n".join(parts),
            silent=True,
            store_history=False,
            allow_stdin=False,
        )

    def restart(self):
        """Restart the kernel process, reusing the same ports.

        Ports are preserved so both clients stay valid across the restart.

        Note this does *not* emit ``QtKernelManager.kernel_restarted`` -- that
        signal fires only from the autorestarter, which is disabled.  Callers
        must run :meth:`bootstrap` themselves afterwards.
        """
        self.manager.restart_kernel(now=False)

    def is_alive(self):
        """Return whether the kernel process is running."""
        try:
            return self.manager is not None and self.manager.is_alive()
        except Exception:  # noqa: BLE001 - liveness must never raise
            return False

    def shutdown(self):
        """Stop the readiness timer and both clients, then kill the kernel."""
        if self._ready_timer is not None:
            self._ready_timer.stop()
        for client in (self.poll_client, self.client):
            if client is None:
                continue
            try:
                client.stop_channels()
            except Exception:  # noqa: BLE001 - best effort during teardown
                logger.debug("Failed to stop channels.", exc_info=True)
        if self.manager is not None:
            try:
                self.manager.shutdown_kernel(now=True)
            except Exception:  # noqa: BLE001 - best effort during teardown
                logger.debug("Failed to shut down kernel.", exc_info=True)


class StatusPoller(QObject):
    """Poll session state and publish it to the tabs."""

    metadata_changed = Signal(dict)
    kernel_values_changed = Signal(dict)
    kernel_state_changed = Signal(str)

    def __init__(self, session, parent=None):
        """Poll state for *session*, a started :class:`KernelSession`."""
        super().__init__(parent)
        self._session = session
        self._md_path = Path(session.cwd) / MD_FILENAME
        self._md_mtime = None
        self._busy = False
        self._state = None
        self._pending = None
        self._once = {}

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def kernel_state(self):
        """Last known kernel state: ``idle``, ``busy``, ``dead`` or None.

        ``kernel_state_changed`` only fires on transitions, so anything that
        needs the *current* state -- rather than a change in it -- must read
        this.  A short operation can start and finish between two polls without
        ever producing a transition.
        """
        return self._state

    def start(self):
        """Begin polling."""
        self._timer.start()
        self._tick()

    def stop(self):
        """Stop polling."""
        self._timer.stop()

    def _tick(self):
        self._drain_iopub()
        self._read_metadata_file()
        self._poll_kernel()
        self._emit_state()

    def _drain_iopub(self):
        """Consume broadcast messages to track kernel busy/idle.

        iopub is broadcast to every client, so this sees execution driven from
        the console even though it runs on the poll client.
        """
        client = self._session.poll_client
        if client is None:
            return
        while True:
            try:
                msg = client.get_iopub_msg(timeout=0)
            except queue.Empty:
                return
            except Exception:  # noqa: BLE001 - a dead channel is not fatal
                return
            if msg.get("msg_type") == "status":
                state = msg.get("content", {}).get("execution_state")
                if state in ("busy", "idle"):
                    self._busy = state == "busy"

    def _read_metadata_file(self):
        """Re-read the RunEngine metadata file when it changes on disk."""
        try:
            mtime = self._md_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._md_mtime:
            return
        try:
            with open(self._md_path) as stream:
                metadata = yaml.safe_load(stream)
        except Exception:  # noqa: BLE001 - a torn read just retries next tick
            logger.debug("Could not read %s.", self._md_path, exc_info=True)
            return
        if isinstance(metadata, dict):
            self._md_mtime = mtime
            self.metadata_changed.emit(metadata)

    def execute_once(self, code):
        """Run a statement in the kernel, invisibly to the console.

        Goes out on the poll client, so it is filtered out of the console
        display.  Returns True if the request was sent.
        """
        client = self._session.poll_client
        if client is None:
            return False
        try:
            client.execute(code, silent=True, store_history=False, allow_stdin=False)
        except Exception:  # noqa: BLE001 - caller decides how to report
            logger.exception("Could not execute %r in the kernel.", code[:80])
            return False
        return True

    def request_once(self, expressions):
        """Evaluate *expressions* on the next poll, then forget them.

        Lets a tab ask an occasional expensive question (a device listing, say)
        without paying for it every second, and without opening a second reader
        on the shell channel.  Results arrive on ``kernel_values_changed``
        alongside the usual keys.
        """
        self._once.update(expressions)

    def _poll_kernel(self):
        """Request kernel-only values, at most one request in flight.

        Holding back while a request is outstanding is what stops a burst of
        queued polls from firing all at once when a long scan finishes.
        """
        client = self._session.poll_client
        if client is None:
            return
        if self._pending is not None:
            self._collect_reply(client)
            return
        if self._busy:
            return
        expressions = dict(KERNEL_EXPRESSIONS)
        expressions.update(self._once)
        try:
            self._pending = client.execute(
                "",
                silent=False,  # silent=True suppresses user_expressions
                store_history=False,  # keeps the console prompt number intact
                allow_stdin=False,
                user_expressions=expressions,
            )
        except Exception:  # noqa: BLE001 - kernel may be restarting
            self._pending = None
        else:
            self._once.clear()

    def _collect_reply(self, client):
        """Non-blocking check for the outstanding poll reply."""
        while True:
            try:
                reply = client.get_shell_msg(timeout=0)
            except queue.Empty:
                return
            except Exception:  # noqa: BLE001 - kernel may be restarting
                self._pending = None
                return
            if reply.get("parent_header", {}).get("msg_id") != self._pending:
                continue  # a stale reply from before a restart
            self._pending = None
            expressions = reply.get("content", {}).get("user_expressions") or {}
            values = {
                key: _from_repr(result["data"]["text/plain"])
                for key, result in expressions.items()
                if result.get("status") == "ok"
            }
            self.kernel_values_changed.emit(values)
            return

    def _emit_state(self):
        if not self._session.is_alive():
            state = "dead"
        else:
            state = "busy" if self._busy else "idle"
        if state != self._state:
            self._state = state
            self.kernel_state_changed.emit(state)

    def reset(self):
        """Forget cached state so a restarted kernel is re-read from scratch."""
        self._md_mtime = None
        self._pending = None
        self._busy = False
