"""Kernel-side motion requests, and the guards that stand in front of them.

``MOTION_HELPERS_CODE`` is a second code string installed by the GUI's
bootstrap next to :data:`~id6_b.mcp_server.bridge.MCP_HELPERS_CODE`.  It exists
separately only so each stays readable; both run in the same namespace, and
``_gui_mcp_ops()`` resolves the names below at call time, so their order does
not matter.

The invariant
-------------
**The MCP layer can only ever *request*.  The only process that emits motion is
the GUI, from a human click or an operator-armed auto window.**

Nothing here calls ``RE(...)`` on behalf of a client.  A motion or scan op
validates its targets and *parks* the request in ``_gui_mcp_pending``; the
Agent tab picks it up on the next 1 Hz poll and, if the operator approves,
calls ``_gui_mcp_execute(token)`` **through the console**, so an
LLM-originated move is an ordinary console command -- in the history, in the
transcript, on the session RunEngine, and interruptible with Ctrl-C.

It also keeps the client honest about time.  A running plan holds the kernel's
shell channel and :meth:`id6_b.mcp_server.session.HklSession._probe_idle`
refuses rather than queueing, so a *blocking* move would blow the call timeout
and leave every later call reporting "busy".  Requests return in milliseconds.

Nothing a client sends is ever executed as text
-----------------------------------------------
A parked request stores *structured* arguments -- an allow-listed dotted path
and a float, or a plan name and its numbers.  ``_gui_mcp_execute`` resolves
those to objects and calls ``RE(bps.mv(obj, value, ...))`` directly.  The
readable command string travels with the request for the banner, the console
comment and the ``[LLM]`` line, and is never evaluated.

Two allow-lists, not one
------------------------
An **axis** is whatever ``_gui_scan_options()`` offers -- moved with
``move_axes``, scanned by the Scan and Macro tabs, one definition shared by
all three.  A **settable signal** is anything else underneath a device that
reports ``write_access``: a filter transmission, a source-meter voltage, an
enum that picks a mode.  Those are ``_gui_mcp_settable_paths``, set with
``request_signals``, and they are kept separate deliberately -- widening
``_movable()`` to reach ``filters.transmission`` would also have put a few
hundred ``velocity`` and ``user_offset`` entries into the Scan tab's axis
combo, and made a motor's coordinate system settable by accident.

The guards
----------
``_gui_mcp_check`` -- ``_gui_mcp_check_targets`` for axes,
``_gui_mcp_check_settables`` for signals, both ending in the same
``_gui_mcp_check_number`` -- runs at request time *and* again inside
``_gui_mcp_execute``, because minutes may pass while the banner waits:

* **Soft limits** -- ``check_value()`` where the axis has one (ophyd raises
  ``LimitError``, which is the canonical check and covers pseudo axes), else
  ``.limits``.  Every axis is checked before anything is parked, and one
  failure refuses the whole request.  No partial move, ever.
* **Maximum travel** -- :data:`MAX_TRAVEL_DEG` for diffractometer axes, plus a
  unit-free :data:`MAX_TRAVEL_FRACTION` of the soft-limit span for any axis
  that has finite limits.  A single absolute cap is meaningless across degrees,
  mm, keV and kelvin.  ``allow_large_move`` lifts both -- a visible, logged,
  still-human-approved act.
* **The choices of an enum** -- a signal set by name is checked against
  ``enum_strs``, and refused *with the list*, so a client that guessed wrong
  can correct itself rather than write an index it did not mean.

Neither guard knows anything about geometry: they are per-axis, and they say
nothing about the detector arm meeting the cryostat.  The angles on the
approval banner are what protects against that.
"""

from .bridge import ALLOWED_DEVICES
from .bridge import ECHO_PREFIX

#: Cap on a single axis's travel, in degrees, for the diffractometer axes.
#:
#: Set to catch a decimal point in the wrong place, not to stop a legitimate
#: long move: driving to a reflection from the home position is routinely 40 to
#: 60 degrees on eta, so a tighter cap would refuse the *first* move of nearly
#: every experiment and teach a client to pass ``allow_large_move`` by habit --
#: which is how a guard stops being one.  Half a circle in a single request is
#: the thing worth questioning.
MAX_TRAVEL_DEG = 90.0

#: Cap on a single axis's travel as a fraction of its soft-limit span, for any
#: axis whose limits are finite.  Unit-free on purpose -- a single absolute cap
#: cannot mean the same thing in degrees, mm, keV and kelvin.  Half its travel
#: in one move is a big move on any axis.
MAX_TRAVEL_FRACTION = 0.5

#: How many past requests the Agent tab is shown.
HISTORY_LIMIT = 20

#: Plans a client may ask to run.  The names are looked up in the session
#: namespace at execution time, so this is an allow-list, not an import.
ALLOWED_PLANS = ("count", "ascan", "lup", "grid_scan", "rel_grid_scan")

#: Ops that read and never park anything.  Sent as empty code by the client, so
#: a model polling them leaves nothing in the console or the transcript.
MOTION_READ_OPS = frozenset(
    {
        "list_axes",
        "read_axes",
        "list_signals",
        "get_counters",
        "get_last_scan",
        "get_request",
    }
)

_CODE = '''\
_gui_mcp_pending = None
_gui_mcp_history = []
_gui_mcp_motion_blocked = False

_GUI_MCP_MAX_TRAVEL_DEG = __MAX_TRAVEL_DEG__
_GUI_MCP_MAX_TRAVEL_FRACTION = __MAX_TRAVEL_FRACTION__
_GUI_MCP_HISTORY_LIMIT = __HISTORY_LIMIT__
_GUI_MCP_PLANS = __PLANS__


def _gui_mcp_clock():
    """Wall-clock time, for the banner and the history list."""
    import time

    return time.strftime("%H:%M:%S")


def _gui_mcp_resolve(path):
    """Return the object a dotted path names, or None.

    Only paths that appear in ``_gui_scan_options()['axes']`` reach here, so
    this is a lookup, not a parser: it walks named attributes and never
    evaluates anything.
    """
    parts = str(path).split(".")
    obj = None
    for _d in oregistry.root_devices:
        if _d.name == parts[0]:
            obj = _d
            break
    for _attr in parts[1:]:
        if obj is None:
            return None
        obj = getattr(obj, _attr, None)
    return obj


def _gui_mcp_axis_paths():
    """The dotted paths a client may move: whatever the Scan tab would offer.

    ``_gui_scan_options`` finds movables by capability rather than by class and
    skips containers, so this is one list with one definition of "an axis" --
    the Scan tab, the Macro tab and an MCP client cannot disagree about it.
    """
    try:
        return [_p for _p, _cls in _gui_scan_options()["axes"]]
    except Exception:
        return []


def _gui_mcp_limits(obj):
    """An axis's soft limits as a (low, high) pair, or None when it has none."""
    try:
        low, high = obj.limits
        low = float(low)
        high = float(high)
    except Exception:
        return None
    # ophyd spells "no limits" as an empty interval.
    return None if low == high else (low, high)


def _gui_mcp_where(obj):
    """An axis's current value, whatever kind of object it is."""
    for _attr in ("position", "get"):
        _value = getattr(obj, _attr, None)
        try:
            return float(_value() if _attr == "get" else _value)
        except Exception:
            continue
    return None


def _gui_mcp_read(obj):
    """A signal's current value in its own type -- a number, or an enum's name."""
    try:
        if not getattr(obj, "connected", True):
            return None
        return obj.get()
    except Exception:
        return None


def _gui_mcp_value(value):
    """Keep a value in the type it will be written in.

    Numbers become floats, as they always have.  A string stays a string,
    which is what an EPICS enum needs -- ``filters.energy_select`` is set to
    ``"Local"``, and ``float()`` would have destroyed that.
    """
    if isinstance(value, str):
        return value
    try:
        return float(value)
    except Exception:
        return value


def _gui_mcp_root(name):
    """The root device called *name*, by exact name, or None.

    Not ``oregistry.find``, whose positional argument is a fuzzy match that
    raises ``MultipleComponentsFound`` as often as it succeeds.
    """
    for _d in oregistry.root_devices:
        if _d.name == str(name):
            return _d
    return None


def _gui_mcp_settable_paths(root):
    """The dotted paths of *root*'s writable signals: the set-value allow-list.

    Same rule as the Macro tab's ``_gui_macro_targets`` -- every non-lazy
    signal underneath the device that reports ``write_access`` -- with two
    deliberate subtractions:

    * **Axes are not here.**  They are ``list_axes``' business, and a client
      that names one is told so rather than handed a second way to move a
      motor.
    * **Nor is anything living underneath an axis.**  That is what keeps
      ``sl1.top.velocity`` and ``sl1.top.user_offset`` off the list: if it is
      an axis, move it with ``move_axes``; redefining its motor record is not
      something to reach through here.

    What is left is the things that are *set* rather than moved -- a filter
    transmission, a source-meter voltage, a temperature setpoint, an enum that
    picks a mode.
    """
    dev = _gui_mcp_root(root)
    if dev is None:
        return []
    axes = set(_gui_mcp_axis_paths())
    under = tuple(_a + "." for _a in axes)
    try:
        walk = [("%s.%s" % (dev.name, _w.dotted_name), _w.item)
                for _w in dev.walk_signals(include_lazy=False)]
    except Exception:
        # A root device that is itself a Signal has no walk_signals.
        walk = [(dev.name, dev)]
    out = []
    for _path, _sig in walk:
        try:
            if not getattr(_sig, "write_access", False):
                continue
        except Exception:
            continue        # a disconnected signal can raise on write_access
        if _path in axes or _path.startswith(under):
            continue
        out.append(_path)
    return sorted(set(out))


def _gui_mcp_is_angle(path):
    """Whether *path* is an axis of a diffractometer, i.e. measured in degrees."""
    return str(path).split(".")[0] in _GUI_MCP_DEVICES


def _gui_mcp_check_number(path, obj, value, allow_large_move, angle):
    """The numeric guard -- soft limits, then travel -- or None if it passes.

    Shared by the axis check and the signal check so the two cannot drift
    apart.  *angle* switches the degree cap on; a signal is not an angle, so
    for one the only cap is the fraction of its soft-limit span, and a signal
    with no limits has none at all.  The approval banner is what protects
    those, which is the same bargain the axes with no limits already make.
    """
    span = _gui_mcp_limits(obj)
    checker = getattr(obj, "check_value", None)
    if callable(checker):
        try:
            checker(value)
        except Exception as exc:
            # ophyd's LimitError names the *setpoint signal* and says
            # "outside of range", which reads as an internal error rather
            # than as an instruction.  Lead with the axis and its soft
            # limits, and keep the original text after it.
            _why = (
                "its soft limits are %g to %g" % (span[0], span[1])
                if span is not None
                else "the axis refused the value"
            )
            return "%s cannot go to %g: %s (%s)" % (path, value, _why, exc)
    if span is not None and not (span[0] <= value <= span[1]):
        return (
            "%s cannot go to %g: soft limits are %g to %g."
            % (path, value, span[0], span[1])
        )

    if allow_large_move:
        return None
    current = _gui_mcp_where(obj)
    if current is None:
        return None
    travel = abs(value - current)
    caps = []
    if angle:
        caps.append((_GUI_MCP_MAX_TRAVEL_DEG, "%g deg" % _GUI_MCP_MAX_TRAVEL_DEG))
    if span is not None:
        _cap = abs(span[1] - span[0]) * _GUI_MCP_MAX_TRAVEL_FRACTION
        caps.append(
            (
                _cap,
                "%g (%g%% of its soft-limit span)"
                % (_cap, _GUI_MCP_MAX_TRAVEL_FRACTION * 100.0),
            )
        )
    for _cap, _text in caps:
        if travel > _cap:
            return (
                "%s would travel %g, from %g to %g; the limit for one move "
                "is %s. Ask the operator, or repeat with "
                "allow_large_move=true if that is really intended."
                % (path, travel, current, value, _text)
            )
    return None


def _gui_mcp_check_targets(targets, allow_large_move=False):
    """Return the reasons *targets* may not be moved to; empty means go.

    *targets* is a list of ``(path, value)``.  Every axis is checked before
    anything is parked or moved, and the caller refuses the request as a whole,
    so a bad axis can never leave the others half-moved.
    """
    problems = []
    allowed = set(_gui_mcp_axis_paths())
    for path, value in targets:
        if path not in allowed:
            problems.append("%s is not a movable axis in this session." % path)
            continue
        obj = _gui_mcp_resolve(path)
        if obj is None:
            problems.append("%s could not be resolved to a device." % path)
            continue
        try:
            value = float(value)
        except Exception:
            problems.append("%s: %r is not a number." % (path, value))
            continue
        problem = _gui_mcp_check_number(
            path, obj, value, allow_large_move, _gui_mcp_is_angle(path)
        )
        if problem:
            problems.append(problem)
    return problems


def _gui_mcp_check_settables(targets, allow_large_move=False):
    """Return the reasons *targets* may not be written; empty means go.

    The numeric half is ``_gui_mcp_check_number``, the same guard the axes get.
    What a signal adds is the string half: an EPICS enum is set by name
    (``filters.energy_select`` is ``"Mono"`` or ``"Local"``), and a name that
    is not one of its choices is refused *with the choices*, so a client can
    correct itself instead of guessing.
    """
    problems = []
    axes = set(_gui_mcp_axis_paths())
    for path, value in targets:
        root = str(path).split(".")[0]
        if path in axes:
            problems.append(
                "%s is a movable axis, not a signal; use move_axes for it."
                % path
            )
            continue
        if path not in _gui_mcp_settable_paths(root):
            problems.append(
                "%s is not a settable signal in this session; list_signals "
                "with device_name=%r says what is." % (path, root)
            )
            continue
        obj = _gui_mcp_resolve(path)
        if obj is None:
            problems.append("%s could not be resolved to a signal." % path)
            continue

        choices = list(getattr(obj, "enum_strs", None) or [])
        if isinstance(value, str):
            if not choices:
                problems.append(
                    "%s takes a number, not the string %r." % (path, value)
                )
            elif value not in choices:
                problems.append(
                    "%s has no setting %r; its choices are %s."
                    % (path, value, ", ".join(choices))
                )
            continue
        try:
            value = float(value)
        except Exception:
            problems.append("%s: %r is not a number." % (path, value))
            continue
        if choices:
            # An enum may also be set by index, but only to one that exists.
            if value != int(value) or not 0 <= int(value) < len(choices):
                problems.append(
                    "%s is an enum; give one of %s, or an index from 0 to %d."
                    % (path, ", ".join(choices), len(choices) - 1)
                )
            continue
        problem = _gui_mcp_check_number(path, obj, value, allow_large_move, False)
        if problem:
            problems.append(problem)
    return problems


def _gui_mcp_check(kind, targets, allow_large_move=False):
    """Run the guard that belongs to *kind*.

    One dispatcher, used at request time *and* at approval time, so the two
    checks cannot come to disagree about what is allowed.
    """
    if kind == "signals":
        return _gui_mcp_check_settables(targets, allow_large_move)
    return _gui_mcp_check_targets(targets, allow_large_move)


def _gui_mcp_note(request, outcome):
    """Record what became of a request, for the Agent tab's history."""
    _gui_mcp_history.append(
        {
            "token": request.get("token", ""),
            "summary": request.get("summary", ""),
            "command": request.get("command", ""),
            "outcome": outcome,
            "when": _gui_mcp_clock(),
        }
    )
    del _gui_mcp_history[:-_GUI_MCP_HISTORY_LIMIT]
    return _gui_mcp_history[-1]


def _gui_mcp_park(kind, device, summary, command, moves, details, allow_large_move,
                  plan=None, args=None, kwargs=None):
    """Validate a request and, if it passes, make it the pending one.

    Returns the dict a client sees.  One request at a time: a second while one
    is waiting is refused, which is also what stops a model that has not
    noticed the answer from queueing up a row of moves.
    """
    global _gui_mcp_pending

    if _gui_mcp_motion_blocked:
        return {
            "error": (
                "Motion requests are switched off in the GUI's Agent tab. The "
                "operator has to turn them back on; nothing was requested."
            )
        }
    if _gui_mcp_pending is not None:
        return {
            "error": (
                "A request is already waiting for the operator: %s. Wait for it "
                "or cancel it before asking for another."
                % _gui_mcp_pending.get("summary", "")
            )
        }

    problems = _gui_mcp_check(kind, moves, allow_large_move)
    if problems:
        _gui_mcp_note(
            {"summary": summary, "command": command}, "refused by a guard"
        )
        return {"error": "Refused, and nothing was requested. " + " ".join(problems)}

    import uuid

    request = {
        "token": uuid.uuid4().hex[:8],
        "kind": kind,
        "device": device,
        "summary": summary,
        "command": command,
        "details": list(details or []),
        # Not float(): a signal may be set to an enum's name, and the value
        # stored here is the one that will be written.
        "moves": [[_p, _gui_mcp_value(_v)] for _p, _v in moves],
        "allow_large_move": bool(allow_large_move),
        "requested": _gui_mcp_clock(),
    }
    if plan is not None:
        request["plan"] = plan
        request["args"] = list(args or [])
        request["kwargs"] = dict(kwargs or {})
    _gui_mcp_pending = request
    return {
        "status": "awaiting approval",
        "token": request["token"],
        "summary": summary,
        "command": command,
        "details": request["details"],
        "message": (
            "Requested, and waiting for the operator to approve it in the "
            "GUI's Agent tab. Nothing has moved. Poll get_request to see what "
            "happened; do not send it again."
        ),
    }


def _gui_mcp_request_hkl(device, h, k, l, allow_large_move=False):
    """Ask to move a diffractometer to an hkl.

    Solved first, with the same ``_gui_hkl_calc`` the HKL tab's Calculate
    button uses, so the six angles are known *before* anyone is asked to
    approve -- and so the guards act on the angles that will really move
    rather than on the Miller indices, which have no limits.
    """
    solution = _gui_hkl_calc(device, h, k, l)
    if solution.get("error"):
        return {
            "error": "Could not solve %s for h=%g k=%g l=%g: %s"
            % (device, h, k, l, solution["error"])
        }
    reals = solution.get("reals") or {}
    moves = [["%s.%s" % (device, _f), _v] for _f, _v in reals.items()]
    details = ["%s %.4f" % (_f, _v) for _f, _v in reals.items()]
    # The angles, not h/k/l: what is approved has to be what runs.  Moving the
    # pseudo axes would solve again at execution time, against whatever the
    # presets and the wavelength are by then -- which is not what the operator
    # saw on the banner.  The Miller indices are in the summary.
    command = "RE(bps.mv(%s))" % ", ".join(
        "%s, %r" % (_p, float(_v)) for _p, _v in moves
    )
    result = _gui_mcp_park(
        "hkl",
        device,
        "%s to H K L = %g %g %g" % (device, h, k, l),
        command,
        moves,
        details,
        allow_large_move,
    )
    if "error" not in result:
        result["angles"] = {_f: float(_v) for _f, _v in reals.items()}
    return result


def _gui_mcp_request_axes(device, targets, allow_large_move=False):
    """Ask to move one or more axes by dotted path."""
    if not isinstance(targets, dict) or not targets:
        return {"error": "Give targets as {axis path: value}, e.g. {'sl1.top': 2.0}."}
    moves = []
    for path in sorted(targets):
        moves.append([str(path), targets[path]])
    summary = ", ".join("%s to %g" % (_p, float(_v)) for _p, _v in moves)
    command = "RE(bps.mv(%s))" % ", ".join(
        "%s, %r" % (_p, float(_v)) for _p, _v in moves
    )
    return _gui_mcp_park(
        "axes", device, summary, command, moves, [], allow_large_move
    )


def _gui_mcp_request_signals(device, targets, allow_large_move=False):
    """Ask to set one or more writable signals.

    The same gate as a move, and for the same reason: a filter that goes in
    changes what the next scan measures as surely as a motor does.  Validated,
    parked, approved by a human, then run in the console as an ordinary
    ``RE(bps.mv(...))`` -- ``mv`` drives anything with a ``set()``, which is
    every ophyd signal, so a transmission and a motor take one path through
    the RunEngine and land in one set of documents.
    """
    if not isinstance(targets, dict) or not targets:
        return {
            "error": (
                "Give targets as {signal path: value}, e.g. "
                "{'filters.transmission': 0.1}."
            )
        }
    writes = []
    for path in sorted(targets):
        writes.append([str(path), _gui_mcp_value(targets[path])])
    summary = ", ".join("%s to %s" % (_p, _v) for _p, _v in writes)
    command = "RE(bps.mv(%s))" % ", ".join(
        "%s, %r" % (_p, _v) for _p, _v in writes
    )
    # What it is now, next to what it would become: the operator is approving a
    # change, and a change is only readable against where it starts.
    details = []
    for _p, _v in writes:
        _now = _gui_mcp_read(_gui_mcp_resolve(_p))
        details.append("%s: %s -> %s" % (_p, "?" if _now is None else _now, _v))
    return _gui_mcp_park(
        "signals", device, summary, command, writes, details, allow_large_move
    )


def _gui_mcp_request_scan(device, plan, axes, points, time, detectors=None,
                          fixq=False, allow_large_move=False):
    """Ask to run a scan.

    The call is built by ``id6_b.gui.scancode.format_scan_call`` -- the same
    function the Scan tab and the Macro tab use -- so the argument-order rule
    that differs between the trajectory and grid plans lives in one place and
    a client cannot get it wrong in a new way.

    Both ends of every axis are checked against the soft limits, since a scan
    that starts inside them and ends outside is a scan that stops half way.
    """
    from id6_b.gui.scancode import format_number
    from id6_b.gui.scancode import format_scan_call
    from id6_b.gui.scancode import plan_shape

    if plan not in _GUI_MCP_PLANS:
        return {
            "error": "Unknown plan %r; known: %s."
            % (plan, ", ".join(_GUI_MCP_PLANS))
        }
    takes_axes, per_axis_points = plan_shape(plan)
    axes = list(axes or [])
    if takes_axes and not axes:
        return {"error": "%s needs at least one axis." % plan}
    if not takes_axes and axes:
        return {"error": "count takes no axes; give points and time only."}
    try:
        time = float(time)
    except Exception:
        return {"error": "Give the time per point in seconds, e.g. 1.0."}
    if not per_axis_points:
        try:
            points = int(points)
        except Exception:
            return {
                "error": "%s needs a number of points shared by every axis."
                % plan
            }

    known = set(_gui_mcp_axis_paths())
    rows, moves = [], []
    for entry in axes:
        try:
            name = str(entry["axis"])
            start = float(entry["start"])
            stop = float(entry["stop"])
        except Exception:
            return {
                "error": (
                    "Each axis needs 'axis', 'start' and 'stop' (and 'points' "
                    "for a grid plan)."
                )
            }
        if name not in known:
            return {"error": "%s is not a movable axis in this session." % name}
        per_points = entry.get("points", points)
        rows.append(
            (
                name,
                format_number(start),
                format_number(stop),
                str(int(per_points)) if per_points else "",
            )
        )
        if plan.startswith("rel_") or plan == "lup":
            # Relative plans move about wherever the axis is now.
            here = _gui_mcp_where(_gui_mcp_resolve(name)) or 0.0
            moves += [[name, here + start], [name, here + stop]]
        else:
            moves += [[name, start], [name, stop]]

    detector_names = []
    if detectors:
        try:
            candidates = set(_gui_scan_options()["detector_candidates"])
        except Exception:
            candidates = set()
        for _d in detectors:
            if _d not in candidates:
                return {
                    "error": "%s is not a detector; known: %s."
                    % (_d, ", ".join(sorted(candidates)) or "none")
                }
            detector_names.append(_d)

    call, problem = format_scan_call(
        plan,
        rows,
        str(int(points)) if points else "",
        format_number(time),
        fixq=fixq,
        detectors=detector_names or None,
    )
    if problem:
        return {"error": problem}

    args = []
    for name, start, stop, per_points in rows:
        args.append(["axis", name])
        args.append(["value", float(start)])
        args.append(["value", float(stop)])
        if per_axis_points:
            args.append(["int", int(per_points)])
    if takes_axes and not per_axis_points:
        args.append(["int", int(points)])
    if not takes_axes:
        args.append(["int", int(points)])
    args.append(["value", float(time)])

    return _gui_mcp_park(
        "scan",
        device,
        call,
        "RE(%s)" % call,
        moves,
        [],
        allow_large_move,
        plan=plan,
        args=args,
        kwargs={"fixq": bool(fixq), "detectors": detector_names},
    )


def _gui_mcp_pending_info():
    """What the Agent tab polls: the pending request, history, master switch."""
    return {
        "pending": _gui_mcp_pending,
        "history": list(_gui_mcp_history[-_GUI_MCP_HISTORY_LIMIT:]),
        "blocked": bool(_gui_mcp_motion_blocked),
    }


def _gui_mcp_request_state(device=None):
    """What a client polls: its request, and what became of the last one."""
    out = _gui_mcp_pending_info()
    out["last"] = _gui_mcp_history[-1] if _gui_mcp_history else None
    if out["pending"] is None:
        out["status"] = "nothing pending"
    else:
        out["status"] = "awaiting approval"
    return out


def _gui_mcp_set_blocked(flag):
    """The Agent tab's master switch."""
    global _gui_mcp_motion_blocked

    _gui_mcp_motion_blocked = bool(flag)
    return _gui_mcp_motion_blocked


def _gui_mcp_cancel(token="", reason="cancelled"):
    """Drop the pending request.  An empty *token* means whichever is pending."""
    global _gui_mcp_pending

    request = _gui_mcp_pending
    if request is None:
        return {"status": "nothing pending"}
    if token and request["token"] != token:
        return {"error": "No pending request with token %s." % token}
    _gui_mcp_pending = None
    entry = _gui_mcp_note(request, str(reason))
    print(
        "__PREFIX__ %s: %s\\n__PREFIX__   -> %s"
        % (request["kind"], request["summary"], reason)
    )
    return {"status": str(reason), "request": entry}


def _gui_mcp_op_cancel(device=None, token=None, reason=None):
    """Op-table form of the cancel: every dispatched op takes device first."""
    return _gui_mcp_cancel(token or "", reason or "withdrawn by the client")


def _gui_mcp_execute(token):
    """Approve and run the pending request.  Called *by the GUI*, never by a client.

    Re-validates first: minutes may have passed at the banner, and the machine
    may have moved since.  Prints the ``[LLM]`` lines before starting, so the
    console says what is happening while it happens.
    """
    global _gui_mcp_pending

    request = _gui_mcp_pending
    if request is None or (token and request["token"] != token):
        print("__PREFIX__ approval ignored: no pending request %s." % token)
        return None

    problems = _gui_mcp_check(
        request["kind"], request["moves"], request.get("allow_large_move", False)
    )
    if problems:
        _gui_mcp_pending = None
        _gui_mcp_note(request, "refused on re-check")
        print(
            "__PREFIX__ %s: %s\\n__PREFIX__   -> refused on re-check: %s"
            % (request["kind"], request["summary"], " ".join(problems))
        )
        return None

    _gui_mcp_pending = None
    entry = _gui_mcp_note(request, "running")
    print(
        "__PREFIX__ approved: %s\\n__PREFIX__   -> %s"
        % (request["summary"], request["command"])
    )

    try:
        if request["kind"] == "scan":
            plan = globals().get(request["plan"])
            if plan is None:
                raise NameError("%s is not defined in this session" % request["plan"])
            args = []
            for kind, value in request["args"]:
                if kind == "axis":
                    args.append(_gui_mcp_resolve(value))
                elif kind == "int":
                    args.append(int(value))
                else:
                    args.append(float(value))
            kwargs = {}
            if request["kwargs"].get("fixq"):
                kwargs["fixq"] = True
            if request["kwargs"].get("detectors"):
                kwargs["detectors"] = [
                    oregistry.find(_n) for _n in request["kwargs"]["detectors"]
                ]
            result = RE(plan(*args, **kwargs))
        else:
            pairs = []
            for path, value in request["moves"]:
                pairs.append(_gui_mcp_resolve(path))
                # Already coerced by _gui_mcp_value when the request was
                # parked, and float() here would break an enum's name.
                pairs.append(value)
            result = RE(bps.mv(*pairs))
    except BaseException as exc:
        entry["outcome"] = "failed: %s" % exc
        print("__PREFIX__   -> failed: %s" % exc)
        raise
    entry["outcome"] = "done"
    print("__PREFIX__   -> done: %s" % request["summary"])
    return result


def _gui_mcp_list_axes(device=None):
    """Every axis a client may move, with where it is and how far it may go."""
    out = []
    for path in _gui_mcp_axis_paths():
        obj = _gui_mcp_resolve(path)
        if obj is None:
            continue
        span = _gui_mcp_limits(obj)
        out.append(
            {
                "axis": path,
                "class": type(obj).__name__,
                "position": _gui_mcp_where(obj),
                "low": None if span is None else span[0],
                "high": None if span is None else span[1],
            }
        )
    return {"axes": out}


def _gui_mcp_signal_info(path):
    """One row of ``list_signals``: what it is, where it is, what it accepts."""
    obj = _gui_mcp_resolve(path)
    row = {
        "signal": path,
        "class": None if obj is None else type(obj).__name__,
        "value": _gui_mcp_read(obj),
    }
    span = _gui_mcp_limits(obj)
    row["low"] = None if span is None else span[0]
    row["high"] = None if span is None else span[1]
    choices = list(getattr(obj, "enum_strs", None) or [])
    if choices:
        row["choices"] = choices
    try:
        units = obj.metadata.get("units")
    except Exception:
        units = None
    if units:
        row["units"] = units
    return row


def _gui_mcp_list_signals(device=None, name=None):
    """The writable signals a client may set -- a device at a time.

    With no *name*, the root devices that have any, and how many.  With one,
    that device's signals in full.  Per device on purpose: reading a value is
    a channel-access round trip each, and the session has several hundred
    signals that nobody is about to set.
    """
    if not name:
        rows = []
        for _d in sorted(oregistry.root_devices, key=lambda _x: _x.name):
            _n = len(_gui_mcp_settable_paths(_d.name))
            if _n:
                rows.append(
                    {"device": _d.name, "class": type(_d).__name__, "signals": _n}
                )
        return {
            "devices": rows,
            "message": (
                "Call again with device_name=<device> for its settable "
                "signals. Motor-like axes are not listed here; they are in "
                "list_axes and are moved with move_axes."
            ),
        }
    if _gui_mcp_root(name) is None:
        return {"error": "No device named %r in this session." % name}
    paths = _gui_mcp_settable_paths(name)
    if not paths:
        return {
            "signals": [],
            "message": (
                "%s has no settable signals of its own. Its movable axes, if "
                "it has any, are in list_axes." % name
            ),
        }
    return {"signals": [_gui_mcp_signal_info(_p) for _p in paths]}


def _gui_mcp_read_axes(device=None, axes=None):
    """Read named axes, or every diffractometer axis when none are named."""
    if not axes:
        axes = [_p for _p in _gui_mcp_axis_paths() if _gui_mcp_is_angle(_p)]
    values, unknown = {}, []
    known = set(_gui_mcp_axis_paths())
    for path in axes:
        if path not in known:
            unknown.append(path)
            continue
        values[path] = _gui_mcp_where(_gui_mcp_resolve(path))
    out = {"values": values}
    if unknown:
        out["unknown"] = unknown
    return out


def _gui_mcp_counters(device=None):
    """What the next scan will count: detectors, channels and the monitor."""
    out = {}
    try:
        out["detectors"] = [_d.name for _d in counters.detectors]
        out["monitor"] = counters.monitor
        out["extra_devices"] = [_d.name for _d in counters.extra_devices]
    except Exception as exc:
        return {"error": "Could not read the counters selection: %s" % exc}
    channels = {}
    for _d in counters.detectors:
        try:
            channels[_d.name] = list(_d.plot_options)
        except Exception:
            pass
    out["channels"] = channels
    return out


def _gui_mcp_last_scan(device=None):
    """The last run: what it scanned, and the peak of every hinted detector.

    Read out of the catalog through ``plans.center_maximum``, the same route
    ``cen()`` takes, so a peak reported here is a peak ``cen()`` would move to
    -- and not ``bec.peaks``, which is filled in asynchronously under Qt.
    """
    from id6_b.plans import center_maximum as _cm
    from id6_b.utils.peak_statistics import peak_statistics

    try:
        run = _cm._last_run()
    except Exception as exc:
        return {"error": "No scan to read: %s" % exc}
    start = run.metadata.get("start", {})
    out = {
        "scan_id": start.get("scan_id"),
        "plan": start.get("plan_name"),
        "motors": list(start.get("motors") or []),
        "points": start.get("num_points"),
        "uid": start.get("uid"),
    }
    try:
        field = _cm._scanned_field(run)
    except Exception:
        return out
    out["axis"] = field
    try:
        x = _cm._column(run, field)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    peaks = {}
    for detector in _cm._hinted_detectors(run):
        try:
            stats = peak_statistics(x, _cm._column(run, detector))
        except Exception:
            continue
        peaks[detector] = {
            _k: (None if _v is None else (list(_v) if isinstance(_v, tuple) else _v))
            for _k, _v in stats.items()
        }
    out["peaks"] = peaks
    return out
'''

#: The code the GUI installs in the kernel, next to ``MCP_HELPERS_CODE``.
#: Substituted rather than ``%``-formatted: the body is full of ``%s``.
MOTION_HELPERS_CODE = (
    _CODE.replace("__MAX_TRAVEL_DEG__", repr(MAX_TRAVEL_DEG))
    .replace("__MAX_TRAVEL_FRACTION__", repr(MAX_TRAVEL_FRACTION))
    .replace("__HISTORY_LIMIT__", repr(HISTORY_LIMIT))
    .replace("__PLANS__", repr(tuple(ALLOWED_PLANS)))
    # Plain textual substitution: every occurrence is already inside a string
    # literal, and some follow an escape rather than a quote.
    .replace("__PREFIX__", ECHO_PREFIX)
)

#: Kept so a reader of this module sees what it is guarding.
DIFFRACTOMETERS = tuple(sorted(ALLOWED_DEVICES))
