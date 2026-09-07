"""Kernel-side helpers for automatic attenuation.

``ATTEN_HELPERS_CODE`` is installed in the session by
:meth:`~id6_b.gui.kernel.KernelSession.bootstrap`, next to the HKL and MCP
helper strings.  It serves **two** callers, which is why it lives here rather
than in either of them:

* the GUI's Detectors tab, which reads ``_gui_atten_state()`` on the 1 Hz poll
  and writes through ``_gui_atten_set()``;
* the MCP dispatcher, through the ``_gui_mcp_atten_*`` wrappers at the bottom,
  which exist only to take the device argument that
  :func:`~id6_b.mcp_server.bridge._gui_mcp_ops` gives every op.

Both go through :func:`~id6_b.plans.auto_attenuation.attenuation_setup`, so the
validation is the same one the console gets and there is no second copy of the
rules here.

Unlike the motion ops, ``set_attenuation`` is **not** parked for approval. It
moves nothing by itself: it is a configuration change of the same kind as
``set_mode`` or ``set_fixed_angles``, which decide where a later approved move
will actually go and are likewise ungated. The filters do move once it is
armed, but only inside a scan the operator has already approved, the setting is
visible in the Detectors tab and in every run's metadata, and each adjustment
prints to the console.
"""

ATTEN_HELPERS_CODE = '''
def _gui_atten_module():
    """Return the auto_attenuation module, or None if it is not installed."""
    try:
        from id6_b.plans import auto_attenuation

        return auto_attenuation
    except Exception:
        return None


#: Hard cap on any name list this module returns.  A reply comes back through
#: ``user_expressions`` as a *repr* rendered by IPython's pretty printer,
#: which truncates a sequence past ``MAX_SEQ_LENGTH`` (1000) by writing a
#: literal ``...`` into it -- and ``ast.literal_eval`` then hands the GUI a
#: list with an ``Ellipsis`` in the middle, which is not a string and blew up
#: ``QComboBox.addItems``.  A real area detector walks to well over 1000
#: signals, so this is a backstop, not a theoretical limit.  The filters below
#: should keep the lists to a few dozen on their own.
_GUI_ATTEN_MAX_NAMES = 300


def _gui_atten_walk(detector):
    """Every signal on *detector*, or an empty list if it cannot be walked."""
    try:
        return [
            walk.item
            for walk in detector.walk_signals(include_lazy=False)
            if getattr(walk, "item", None) is not None
        ]
    except Exception:
        return []


def _gui_atten_detectors():
    """The selected detectors, or an empty list."""
    try:
        from id6_b.utils.counters_class import counters

        return list(counters.detectors)
    except Exception:
        return []


def _gui_atten_channels():
    """Data keys that can be *watched*, from the selected detectors.

    Only signals read at every point qualify -- ``hinted`` or ``normal``.  A
    ``config`` signal is recorded once per descriptor rather than per event,
    so it can never be the thing a per-point threshold looks at, and on an
    area detector the config signals are the overwhelming majority: the
    unfiltered walk of ``lambda250k`` runs to thousands.

    ``kind & Kind.normal`` is the test rather than a set membership, because
    ``Kind`` is a flag and ``hinted`` includes ``normal``.
    """
    from ophyd import Kind

    names = []
    for det in _gui_atten_detectors():
        for signal in _gui_atten_walk(det):
            name = getattr(signal, "name", None)
            if not name:
                continue
            try:
                if not signal.kind & Kind.normal:
                    continue
            except Exception:
                continue
            names.append(str(name))
    return sorted(set(names))[:_GUI_ATTEN_MAX_NAMES]


def _gui_atten_counter_channels():
    """Array counters that could confirm a frame was processed.

    Deliberately *not* the same list as :func:`_gui_atten_channels`: a plugin
    counter is usually ``config`` kind, so it is not read per event and would
    be filtered out there -- but the retake loop reads it directly with
    ``rd()``, for which kind is irrelevant.
    """
    names = []
    for det in _gui_atten_detectors():
        for signal in _gui_atten_walk(det):
            name = getattr(signal, "name", None)
            if name and str(name).endswith("array_counter"):
                names.append(str(name))
    return sorted(set(names))[:_GUI_ATTEN_MAX_NAMES]


def _gui_atten_find_signal(name):
    """Resolve a data key back to its signal object, or None.

    Searches *every* signal, not just the readable ones, for the reason
    :func:`_gui_atten_counter_channels` gives: the counter is read directly
    and its kind does not matter.
    """
    if not name:
        return None
    for det in _gui_atten_detectors():
        for signal in _gui_atten_walk(det):
            if getattr(signal, "name", None) == name:
                return signal
    return None


def _gui_atten_state():
    """Report the automatic-attenuation settings and what it can watch."""
    module = _gui_atten_module()
    if module is None:
        return {
            "available": False,
            "error": "Could not import id6_b.plans.auto_attenuation.",
        }

    settings = module.auto_atten
    filters = settings.filters
    transmission = None
    if filters is not None:
        try:
            transmission = float(filters.transmission.get())
        except Exception:
            transmission = None

    counter = settings.counter_signal
    return {
        "available": True,
        "enabled": bool(settings.enabled),
        "ready": bool(settings.ready),
        "signal": settings.signal,
        "low": settings.low,
        "high": settings.high,
        "factor": str(settings.factor),
        "min_transmission": settings.min_transmission,
        "max_transmission": settings.max_transmission,
        "settle": settings.settle,
        "move_timeout": settings.move_timeout,
        "max_tries": settings.max_tries,
        "counter_signal": getattr(counter, "name", None),
        "filters": getattr(filters, "name", None),
        "transmission": transmission,
        "channels": _gui_atten_channels(),
        "counter_channels": _gui_atten_counter_channels(),
        "summary": repr(settings),
    }


#: Settings that are plain numbers, and the type each is read back as.
_GUI_ATTEN_NUMBERS = {
    "low": float,
    "high": float,
    "min_transmission": float,
    "max_transmission": float,
    "settle": float,
    "move_timeout": float,
    "max_tries": int,
}


def _gui_atten_set(values):
    """Apply automatic-attenuation settings.

    *values* maps setting name to value; anything left out keeps its current
    value.  ``factor`` takes a number or ``"auto"``.  ``signal`` and
    ``counter_signal`` take data keys -- the latter is resolved back to its
    signal object here, because the retake loop reads it directly.

    Returns the new state, or a dict with ``error`` explaining the refusal.
    Every rule is checked by ``attenuation_setup`` itself, so the GUI, the
    console and an MCP client are all held to the same one.
    """
    module = _gui_atten_module()
    if module is None:
        return {"error": "Could not import id6_b.plans.auto_attenuation."}
    if not isinstance(values, dict):
        return {"error": "Cannot apply settings: expected a JSON object."}

    known = (
        {"signal", "counter_signal", "factor", "enabled"}
        | set(_GUI_ATTEN_NUMBERS)
    )
    unexpected = sorted(set(values) - known)
    if unexpected:
        return {
            "error": "Unknown setting(s): %s. Accepts: %s."
            % (", ".join(unexpected), ", ".join(sorted(known)))
        }

    kwargs = {}
    for name, cast in _GUI_ATTEN_NUMBERS.items():
        if name in values and values[name] is not None:
            try:
                kwargs[name] = cast(values[name])
            except (TypeError, ValueError):
                return {
                    "error": "Cannot apply settings: %s must be a number, "
                    "got %r." % (name, values[name])
                }

    if "signal" in values and values["signal"]:
        kwargs["signal"] = str(values["signal"])

    if "factor" in values and values["factor"] is not None:
        factor = values["factor"]
        if isinstance(factor, str) and factor.strip().lower() in ("auto", ""):
            kwargs["factor"] = module.AUTO
        else:
            try:
                kwargs["factor"] = float(factor)
            except (TypeError, ValueError):
                return {
                    "error": "Cannot apply settings: factor must be a number "
                    "greater than 1, or 'auto'. Got %r." % (factor,)
                }

    if "counter_signal" in values:
        wanted = values["counter_signal"]
        if not wanted:
            kwargs["counter_signal"] = None
        else:
            found = _gui_atten_find_signal(str(wanted))
            if found is None:
                return {
                    "error": "Cannot apply settings: no signal named %r among "
                    "the selected detectors. Pick one of the channels "
                    "get_attenuation reports." % (str(wanted),)
                }
            kwargs["counter_signal"] = found

    enabled = values.get("enabled")
    if enabled is None:
        enabled = module.auto_atten.enabled

    try:
        module.attenuation_setup(enabled=bool(enabled), **kwargs)
    except ValueError as exc:
        return {"error": "Cannot apply settings: %s" % (exc,)}
    except Exception as exc:
        return {"error": "Cannot apply settings: %s: %s" % (type(exc).__name__, exc)}

    return _gui_atten_state()


# -- the MCP dispatcher's calling convention ----------------------------
# Every op is called as function(device, *args), whether it needs the device
# or not.  These two adapt to that and add nothing else.


def _gui_mcp_atten_state(device):
    """MCP: report the automatic-attenuation settings."""
    return _gui_atten_state()


def _gui_mcp_atten_set(device, values):
    """MCP: apply automatic-attenuation settings."""
    return _gui_atten_set(values)
'''
