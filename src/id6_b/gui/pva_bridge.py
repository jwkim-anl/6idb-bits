"""Kernel-side helpers for the PVA streaming cache.

``PVA_HELPERS_CODE`` is installed in the session by
:meth:`~id6_b.gui.kernel.KernelSession.bootstrap`, next to the HKL, MCP and
attenuation helper strings, and backs the Detectors tab's *PVA streaming*
group.

It is its own module rather than more lines in ``kernel.HELPERS_CODE`` for the
reason ``MOTION_HELPERS_CODE`` is its own module rather than more lines in
``MCP_HELPERS_CODE``: this is one feature's kernel code, with its own
validation surface, and ``HELPERS_CODE`` is the generic set every tab draws on.
(It differs from ``atten_bridge.py`` in having one caller, not two -- there are
no ``_gui_mcp_pva_*`` wrappers here.  Adding them, should an MCP client ever
want to arm the cache, is the same two functions at the bottom.)

Everything goes through :func:`~id6_b.plans.pva_streaming.pva_streaming_setup`,
so the validation is the one the console gets and there is no second copy of
the rules here.

The state includes a **live readback of the three PVs**, not only the settings.
``6idb1:FilePath:Value`` is a 40-byte Channel Access string and the server
truncates a longer one without saying so, so what the IOC actually kept after
the last scan is the one thing that settles whether a path fitted -- the
character count this package computes is a prediction, the readback is the
result.  ``scan_on`` is there for the other half of the same worry: a cache
left collecting because a scan died in a way the ``finalize_wrapper`` could not
catch shows up as a flag stuck at 1 between scans.
"""

PVA_HELPERS_CODE = '''
def _gui_pva_module():
    """Return the pva_streaming plan module, or None if it is not installed."""
    try:
        from id6_b.plans import pva_streaming

        return pva_streaming
    except Exception:
        return None


def _gui_pva_readback(device):
    """The three PVs as the IOC currently holds them.

    Every read is guarded and reported as None on failure: the streaming
    server is not always running, and a disconnected PV must leave the rest
    of the Detectors tab working.
    """
    values = {"scan_on": None, "pv_file_path": None, "pv_file_name": None}
    if device is None:
        return values
    for key, attr in (
        ("scan_on", "scan_on"),
        ("pv_file_path", "file_path"),
        ("pv_file_name", "file_name"),
    ):
        try:
            values[key] = getattr(device, attr).get()
        except Exception:
            values[key] = None
    return values


def _gui_pva_state():
    """Report the PVA streaming settings, the next file, and the live PVs."""
    module = _gui_pva_module()
    if module is None:
        return {
            "available": False,
            "error": "Could not import id6_b.plans.pva_streaming.",
        }

    settings = module.pva_stream
    device = settings.device

    # The next file needs the experiment (for the sample directory and the
    # base name) and the RunEngine (for the scan id).  Either can be missing
    # in a session that has not been set up, which is a thing to report
    # rather than a failure of this read.
    path = None
    name = None
    try:
        path = settings.file_path()
        name = settings.file_name(module._next_scan_id())
    except Exception as exc:
        next_error = "%s: %s" % (type(exc).__name__, exc)
    else:
        next_error = None

    state = {
        "available": True,
        "enabled": bool(settings.enabled),
        "ready": bool(settings.ready),
        "device": settings.device_name,
        "device_found": device is not None,
        "name_format": settings.name_format,
        "stop_delay": settings.stop_delay,
        "use_tilde": settings.use_tilde,
        "max_string": module.MAX_STRING,
        "file_path": path,
        "file_name": name,
        "next_error": next_error,
        # Computed here rather than in the GUI so the limit has one owner.
        "path_over": path is not None and len(path) > module.MAX_STRING,
        "name_over": name is not None and len(name) > module.MAX_STRING,
    }
    state.update(_gui_pva_readback(device))
    return state


def _gui_pva_set(values):
    """Apply PVA streaming settings.

    *values* maps setting name to value; anything left out keeps its current
    value.  Returns the new state, or a dict with ``error`` explaining the
    refusal -- every rule is checked by ``pva_streaming_setup`` itself, so the
    GUI and the console are held to the same one.
    """
    module = _gui_pva_module()
    if module is None:
        return {"error": "Could not import id6_b.plans.pva_streaming."}
    if not isinstance(values, dict):
        return {"error": "Cannot apply settings: expected a JSON object."}

    known = {"enabled", "device", "name_format", "stop_delay", "use_tilde"}
    unexpected = sorted(set(values) - known)
    if unexpected:
        return {
            "error": "Unknown setting(s): %s. Accepts: %s."
            % (", ".join(unexpected), ", ".join(sorted(known)))
        }

    kwargs = {}

    if "stop_delay" in values and values["stop_delay"] is not None:
        try:
            kwargs["stop_delay"] = float(values["stop_delay"])
        except (TypeError, ValueError):
            return {
                "error": "Cannot apply settings: stop delay must be a number "
                "of seconds, got %r." % (values["stop_delay"],)
            }

    if "name_format" in values and values["name_format"]:
        fmt = str(values["name_format"])
        # Checked here rather than at scan time.  The format is only ever
        # applied inside the scan wrapper, so a bad one would otherwise
        # surface as a TypeError that kills the first scan after it is set --
        # long after the mistake, and with the cache flag already on.
        try:
            fmt % ("scan", 1)
        except (TypeError, ValueError, KeyError) as exc:
            return {
                "error": "Cannot apply settings: the file name format must "
                "take a base name and a scan number, as "
                "'pva_%%s_%%05d.h5' does. Got %r (%s)." % (fmt, exc)
            }
        kwargs["name_format"] = fmt

    if "device" in values and values["device"]:
        kwargs["device"] = str(values["device"])

    if "use_tilde" in values and values["use_tilde"] is not None:
        kwargs["use_tilde"] = bool(values["use_tilde"])

    enabled = values.get("enabled")
    if enabled is None:
        enabled = module.pva_stream.enabled

    try:
        module.pva_streaming_setup(enabled=bool(enabled), **kwargs)
    except ValueError as exc:
        return {"error": "Cannot apply settings: %s" % (exc,)}
    except Exception as exc:
        return {"error": "Cannot apply settings: %s: %s" % (type(exc).__name__, exc)}

    return _gui_pva_state()
'''
