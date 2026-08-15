"""Kernel-side dispatcher for the MCP server.

``MCP_HELPERS_CODE`` is installed in the kernel by the GUI's bootstrap, next to
:data:`~id6_b.gui.hkl_bridge.HKL_HELPERS_CODE`, and defines a single entry
point ``_gui_mcp(payload)``.  Everything it can do is one of the ``_gui_hkl_*``
functions that already back the HKL tab, or one of the ``_gui_mcp_*`` request
helpers in :mod:`id6_b.mcp_server.motion`; this module adds no diffractometer
logic of its own.

Motion is *requested*, never performed: the ops below park a validated request
for the GUI's Agent tab to approve.  Nothing reachable from here calls ``RE``.

Three things live here rather than in the MCP server, on purpose:

**The device allow-list.**  ``_GUI_MCP_DEVICES`` and the per-op scope are
checked in the kernel, so a bug in the server -- or a second, unofficial client
that found the connection file -- cannot address an op to a device it was not
written for.  Enforcing it in the client would be a comment, not a control.

**The op allow-list.**  ``op`` indexes an explicit dict of functions.  Nothing
is looked up by name off the payload, and the payload itself is a JSON string,
so the only value interpolated into executed code is one Python string literal.

**The UB backup.**  ``compute_ub`` on two bad reflections replaces a working
orientation with no way back, so the previous UB is stashed before every
mutating call and ``restore_ub`` puts it back.

Mutating calls print one ``[LLM]`` line.  That is what reaches the operator's
console (:meth:`id6_b.gui.app.MainWindow._echo_llm`) and the session
transcript, so an orientation that changed under someone's feet is explicable
afterwards.  Reads print nothing: a model may call ``get_state`` freely and
should not fill the console by doing so.
"""

#: Diffractometers the MCP layer may address.  ``psic_psi`` and ``psic_q`` stay
#: out: they are separate engines on the same motors and nothing here needs
#: them.  The kernel copy below is generated from this set, which is what makes
#: the allow-list a control rather than a comment.
ALLOWED_DEVICES = frozenset({"psic_sim", "psic"})

#: The device a tool acts on when the caller does not name one.  The simulator,
#: always: the real machine has to be asked for by name, every time.
DEFAULT_DEVICE = "psic_sim"

#: Stand-in device for ops that belong to the session rather than to a
#: diffractometer -- moving a slit, running a scan, reading the counters.  An
#: op is scoped to one or the other (see ``_gui_mcp_ops``), so an hkl op cannot
#: arrive addressed to the session and an axis move cannot arrive addressed to
#: ``psic``.
SESSION_DEVICE = "session"

#: Marks the console lines an MCP client's changes produce.  On *every* line,
#: not just the first: :meth:`id6_b.gui.app.MainWindow._echo_llm` selects the
#: lines to echo by this prefix, and a stream message the kernel split would
#: otherwise lose its tail.
ECHO_PREFIX = "[LLM]"

#: Ops that only read.  The session client sends these as a bare
#: ``user_expressions`` request with *empty* code, so they leave no
#: ``execute_input`` on iopub and therefore no line in the transcript.
#: Mutating ops are sent as real code, so they do.  The motion module adds its
#: own reads to this set in :mod:`id6_b.mcp_server.session`, which is where the
#: two are combined -- importing them here would be a cycle.
READ_OPS = frozenset({"get_state", "get_position"})

#: Message prefixes the ``_gui_hkl_*`` helpers use when they decline to do
#: something.  Advisory only -- it sets the ``ok`` flag as a convenience, and
#: the message itself is always returned and is the authoritative answer.
FAILURE_PREFIXES = (
    "Could not",
    "Cannot",
    "Unknown",
    "The current sample cannot",
    "Psi can only",
    "UB not computed",
    "The two orienting",
    "No UB backup",
    "Fixed angles must be",
)

_CODE = '''\
_gui_mcp_ub_backup = {}
_gui_mcp_last = "{}"

#: Filled in from id6_b.mcp_server.bridge, so there is one source of truth.
_GUI_MCP_DEVICES = __DEVICES__
_GUI_MCP_SESSION = __SESSION__
_GUI_MCP_FAILURE_PREFIXES = __FAILURES__


def _gui_mcp_restore_ub(device):
    """Put back the UB from before the last mutating call."""
    backup = _gui_mcp_ub_backup.get(device)
    if backup is None:
        return "No UB backup yet for %s." % device
    try:
        _gui_hkl_dev(device).sample.UB = backup
    except Exception as exc:
        return "Could not restore UB: %s" % exc
    return "UB restored to the value from before the last change."


def _gui_mcp_ops():
    """op name -> (function, argument names, mutating?, scope).

    *scope* is "hkl" for an op that acts on a diffractometer and "session" for
    one that acts on the session as a whole.  Every function takes the device
    as its first argument, whether it needs it or not, so the dispatcher has
    one calling convention.

    The names are resolved here rather than captured, so the motion helpers
    (:mod:`id6_b.mcp_server.motion`) may be installed before or after this
    string -- both run in the same namespace.
    """
    return {
        # -- orientation, on a diffractometer ------------------------------
        "get_state": (_gui_hkl_state, (), False, "hkl"),
        "get_position": (_gui_hkl_position, (), False, "hkl"),
        "add_sample": (
            _gui_hkl_add_sample,
            ("sample", "a", "b", "c", "alpha", "beta", "gamma"),
            True,
            "hkl",
        ),
        "select_sample": (_gui_hkl_select_sample, ("sample",), True, "hkl"),
        "remove_sample": (_gui_hkl_remove_sample, ("sample",), True, "hkl"),
        "set_lattice": (_gui_hkl_set_lattice, ("values",), True, "hkl"),
        "add_reflection": (
            _gui_hkl_add_reflection,
            ("pseudos", "reals"),
            True,
            "hkl",
        ),
        "edit_reflection": (
            _gui_hkl_edit_reflection,
            ("key", "pseudos", "reals"),
            True,
            "hkl",
        ),
        "remove_reflection": (_gui_hkl_remove_reflection, ("key",), True, "hkl"),
        "set_orienting": (_gui_hkl_set_orienting, ("first", "second"), True, "hkl"),
        "compute_ub": (_gui_hkl_compute_ub, (), True, "hkl"),
        "set_mode": (_gui_hkl_set_mode, ("mode",), True, "hkl"),
        "set_fixed_angles": (_gui_hkl_set_presets, ("values",), True, "hkl"),
        "set_psi_reference": (_gui_hkl_set_azimuth, ("h2", "k2", "l2"), True, "hkl"),
        "set_psi": (_gui_hkl_set_psi, ("psi",), True, "hkl"),
        # calc_angles writes presets and extras as a side effect, so it counts
        # as mutating even though it moves nothing and returns a reading.
        "calc_angles": (
            _gui_hkl_calc,
            ("h", "k", "l", "psi", "presets"),
            True,
            "hkl",
        ),
        "restore_ub": (_gui_mcp_restore_ub, (), True, "hkl"),
        # -- motion: these *request*, they never move ----------------------
        "request_hkl": (
            _gui_mcp_request_hkl,
            ("h", "k", "l", "allow_large_move"),
            True,
            "hkl",
        ),
        "request_axes": (
            _gui_mcp_request_axes,
            ("targets", "allow_large_move"),
            True,
            "session",
        ),
        "request_scan": (
            _gui_mcp_request_scan,
            ("plan", "axes", "points", "time", "detectors", "fixq",
             "allow_large_move"),
            True,
            "session",
        ),
        "cancel_request": (_gui_mcp_op_cancel, ("token", "reason"), True, "session"),
        # -- reading the session -------------------------------------------
        "get_request": (_gui_mcp_request_state, (), False, "session"),
        "list_axes": (_gui_mcp_list_axes, (), False, "session"),
        "read_axes": (_gui_mcp_read_axes, ("axes",), False, "session"),
        "get_counters": (_gui_mcp_counters, (), False, "session"),
        "get_last_scan": (_gui_mcp_last_scan, (), False, "session"),
    }


def _gui_mcp_brief(result):
    """A one-line rendering of a dict result, for the console line."""
    reals = result.get("reals")
    if isinstance(reals, dict):
        return ", ".join("%s=%.4f" % (k, v) for k, v in reals.items())
    status = result.get("status")
    summary = result.get("summary")
    if status and summary:
        return "%s: %s" % (status, summary)
    if status:
        return str(status)
    return "ok"


def _gui_mcp_report(op, device, args, outcome):
    """Print what an MCP client just changed, for the operator to read.

    One ``print`` for the whole thing, and ``__PREFIX__`` on *every* line: the
    GUI echoes these into the console by matching that prefix, and a stream
    message the kernel happened to split would otherwise lose its second half.
    """
    parts = []
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, dict):
            inner = ", ".join("%s=%s" % (k, v) for k, v in value.items())
            parts.append("%s(%s)" % (key, inner))
        else:
            parts.append("%s=%s" % (key, value))
    head = "%s %s(%s)%s" % (
        "__PREFIX__",
        op,
        device,
        (": " + " ".join(parts)) if parts else "",
    )
    body = "\\n".join(
        "%s   -> %s" % ("__PREFIX__", line) for line in str(outcome).splitlines()
    )
    print(head + ("\\n" + body if body else ""))


def _gui_mcp(payload):
    """Run one allow-listed HKL operation described by a JSON string.

    Returns a JSON string ``{"ok": bool, "message": str, "data": ...}`` and
    leaves the same text in ``_gui_mcp_last``, which is what the client reads
    back through ``user_expressions``.
    """
    import json

    global _gui_mcp_last

    def _reply(ok, message, data=None):
        global _gui_mcp_last
        _gui_mcp_last = json.dumps(
            {"ok": bool(ok), "message": message, "data": data}, default=str
        )
        return _gui_mcp_last

    try:
        request = json.loads(payload)
    except Exception as exc:
        return _reply(False, "Malformed request: %s" % exc)
    if not isinstance(request, dict):
        return _reply(False, "Request must be a JSON object.")

    op = request.get("op")
    device = request.get("device")
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return _reply(False, "'args' must be a JSON object.")

    table = _gui_mcp_ops()
    if op not in table:
        return _reply(
            False,
            "Unknown operation %r; known: %s."
            % (op, ", ".join(sorted(table))),
        )
    function, names, mutating, scope = table[op]

    # The device check is per op, so an axis move cannot arrive addressed to a
    # diffractometer and an orientation change cannot arrive addressed to the
    # session.  Kernel-side, so a bug in the server cannot skip it.
    if scope == "hkl" and device not in _GUI_MCP_DEVICES:
        return _reply(
            False,
            "Device %r is not available over MCP; allowed: %s. Nothing was "
            "changed." % (device, ", ".join(sorted(_GUI_MCP_DEVICES))),
        )
    if scope == "session" and device != _GUI_MCP_SESSION:
        return _reply(
            False,
            "%s acts on the whole session, so it takes device %r, not %r. "
            "Nothing was changed." % (op, _GUI_MCP_SESSION, device),
        )

    unexpected = [k for k in args if k not in names]
    if unexpected:
        return _reply(
            False,
            "Unexpected argument(s) for %s: %s. Accepts: %s."
            % (op, ", ".join(sorted(unexpected)), ", ".join(names) or "none"),
        )

    if mutating and scope == "hkl" and op != "restore_ub":
        try:
            _gui_mcp_ub_backup[device] = [
                [float(v) for v in row] for row in _gui_hkl_dev(device).sample.UB
            ]
        except Exception:
            pass

    ordered = [args.get(name) for name in names]
    try:
        result = function(device, *ordered)
    except Exception as exc:
        message = "%s failed: %s" % (op, exc)
        if mutating:
            _gui_mcp_report(op, device, args, message)
        return _reply(False, message)

    if isinstance(result, dict):
        error = result.get("error")
        if mutating:
            _gui_mcp_report(op, device, args, error or _gui_mcp_brief(result))
        return _reply(not error, error or "ok", result)

    message = str(result)
    if mutating:
        _gui_mcp_report(op, device, args, message)
    ok = not message.startswith(tuple(_GUI_MCP_FAILURE_PREFIXES))
    return _reply(ok, message)
'''

#: The code the GUI installs in the kernel.  Built by substitution rather than
#: ``%`` formatting: :data:`~id6_b.gui.hkl_bridge.HKL_HELPERS_CODE` uses ``%``
#: and the body below is full of ``%s``.
MCP_HELPERS_CODE = (
    _CODE.replace("__DEVICES__", repr(set(sorted(ALLOWED_DEVICES))))
    .replace("__SESSION__", repr(SESSION_DEVICE))
    .replace("__FAILURES__", repr(tuple(FAILURE_PREFIXES)))
    .replace('"__PREFIX__"', repr(ECHO_PREFIX))
)
