"""Kernel-side helpers for the HKL tab.

These are installed in the kernel as part of the bootstrap cell.  They exist
here rather than in the GUI because ``user_expressions`` evaluates expressions
only -- it cannot run a ``try``/``except`` -- and almost every hklpy2 call can
raise on an unoriented or unreachable configuration.

The API mirrors ``id6_b.utils.hkl_utils_pete``, which is the reference for the
call sequences.  That module is deliberately **not imported**: at module scope
it builds its own ``RunEngine(...)``, which would collide with the session's.

Everything returned is plain Python (floats coerced off numpy) so the poller's
``ast.literal_eval`` round trip works.
"""

#: Diffractometers the tab may act on, in selector order.  ``psic_psi`` and
#: ``psic_q`` are used internally for the psi and 2theta readouts only.
DIFFRACTOMETERS = ["psic", "psic_sim"]

#: Names of the helper geometries used for derived quantities.
PSI_GEOMETRY = "psic_psi"
Q_GEOMETRY = "psic_q"

#: Poller keys.
POSITION_KEY = "hkl_position"
STATE_KEY = "hkl_state"

HKL_HELPERS_CODE = '''\
def _gui_hkl_f(value, default=None):
    """Coerce a possibly-numpy value to a plain float."""
    try:
        return float(value)
    except Exception:
        return default


def _gui_hkl_dev(name):
    """Return the named diffractometer from the registry."""
    return oregistry.find(name)


def _gui_hkl_derived(dev):
    """Return (two_theta, psi, psi_reference) using the helper geometries.

    Mirrors ``hkl_utils_pete._wh()``: the psi geometry needs the main
    diffractometer's UB copied onto it before ``inverse()`` means anything.
    """
    import math

    two_theta = psi = None
    reference = {}
    try:
        _q = _gui_hkl_dev("%(q)s")
        _lambda = _gui_hkl_f(dev.beam.wavelength.get())
        _qval = _gui_hkl_f(_q.inverse(0).q)
        if _qval is not None and _lambda:
            _arg = _qval * _lambda / (4.0 * math.pi)
            if -1.0 <= _arg <= 1.0:
                two_theta = math.degrees(2.0 * math.asin(_arg))
    except Exception:
        pass
    try:
        _p = _gui_hkl_dev("%(psi)s")
        _p.sample.UB = dev.sample.UB
        psi = _gui_hkl_f(_p.inverse(0).psi)
        reference = {k: _gui_hkl_f(v) for k, v in _p.core.extras.items()}
    except Exception:
        pass
    return two_theta, psi, reference


def _gui_hkl_position(name):
    """Cheap, frequently-polled readout: angles, hkl, 2theta, psi."""
    dev = _gui_hkl_dev(name)
    out = {"device": name}
    try:
        out["pseudos"] = {
            f: _gui_hkl_f(getattr(dev, f).position)
            for f in dev.pseudo_positioners._fields
        }
        out["reals"] = {
            f: _gui_hkl_f(getattr(dev, f).position)
            for f in dev.real_positioners._fields
        }
    except Exception as exc:
        out["error"] = str(exc)
        return out
    out["wavelength"] = _gui_hkl_f(dev.beam.wavelength.get())
    out["energy"] = _gui_hkl_f(dev.beam.energy.get())
    two_theta, psi, reference = _gui_hkl_derived(dev)
    out["two_theta"] = two_theta
    out["psi"] = psi
    out["psi_reference"] = reference
    out["mode"] = dev.core.mode
    return out


def _gui_hkl_state(name):
    """Full orientation state: samples, reflections, modes, UB."""
    dev = _gui_hkl_dev(name)
    state = {"device": name}
    state["real_fields"] = list(dev.real_positioners._fields)
    state["pseudo_fields"] = list(dev.pseudo_positioners._fields)

    samples = {}
    for sname, sample in dev.samples.items():
        try:
            names = sample.lattice.system_parameter_names(0)
            samples[sname] = {
                p: _gui_hkl_f(getattr(sample.lattice, p)) for p in names
            }
        except Exception:
            samples[sname] = {}
    state["samples"] = samples
    state["sample"] = dev.sample.name
    state["lattice_names"] = list(
        dev.sample.lattice.system_parameter_names(0)
    )

    reflections = []
    try:
        order = list(dev.sample.reflections.order)
        for key, ref in dev.sample.reflections.items():
            tag = ""
            if order and key == order[0]:
                tag = "first"
            elif len(order) > 1 and key == order[1]:
                tag = "second"
            reflections.append({
                "key": str(key),
                "pseudos": {k: _gui_hkl_f(v) for k, v in ref.pseudos.items()},
                "reals": {k: _gui_hkl_f(v) for k, v in ref.reals.items()},
                "tag": tag,
            })
    except Exception:
        order = []
    state["reflections"] = reflections
    state["order"] = [str(k) for k in order]

    # Readback PV per real axis, so the GUI can follow a move over Channel
    # Access while the kernel's shell channel is blocked by the move itself.
    # None for soft axes (psic_sim), which have no PVs to watch.
    real_pvs = {}
    for _f in dev.real_positioners._fields:
        _ax = getattr(dev, _f, None)
        _pv = None
        for _attr in ("user_readback", "readback"):
            _sig = getattr(_ax, _attr, None)
            _pv = getattr(_sig, "pvname", None)
            if _pv:
                break
        real_pvs[_f] = _pv
    state["real_pvs"] = real_pvs

    state["mode"] = dev.core.mode
    state["modes"] = list(dev.core.modes)
    try:
        state["constant_axes"] = list(dev.core.constant_axis_names)
    except Exception:
        state["constant_axes"] = []
    try:
        state["presets"] = {k: _gui_hkl_f(v) for k, v in dev.core.presets.items()}
    except Exception:
        state["presets"] = {}
    try:
        state["extras"] = {k: _gui_hkl_f(v) for k, v in dev.core.extras.items()}
    except Exception:
        state["extras"] = {}
    try:
        state["ub"] = [[_gui_hkl_f(v) for v in row] for row in dev.sample.UB]
    except Exception:
        state["ub"] = []
    two_theta, psi, reference = _gui_hkl_derived(dev)
    state["two_theta"] = two_theta
    state["psi"] = psi
    state["psi_reference"] = reference
    return state


def _gui_hkl_recompute_ub(dev):
    """Recompute UB when two orienting reflections exist.

    ``forward(1, 0, 0)`` afterwards is the workaround documented in
    ``hkl_utils_pete.compute_UB``: without one calculation, a later ``wh()``
    fails.
    """
    order = list(dev.sample.reflections.order)
    if len(order) < 2:
        return "UB not computed: needs two orienting reflections."
    dev.sample.core.calc_UB(order[0], order[1])
    dev.forward(1, 0, 0)
    return f"UB computed from {order[0]} and {order[1]}."


def _gui_hkl_select_sample(name, sample):
    try:
        _gui_hkl_dev(name).sample = sample
        return f"Current sample: {sample}"
    except Exception as exc:
        return f"Could not select sample: {exc}"


def _gui_hkl_add_sample(name, sample, a, b, c, alpha, beta, gamma):
    try:
        _gui_hkl_dev(name).add_sample(
            sample, a, b=b, c=c, alpha=alpha, beta=beta, gamma=gamma,
            replace=True,
        )
        return f"Added sample {sample}."
    except Exception as exc:
        return f"Could not add sample: {exc}"


def _gui_hkl_remove_sample(name, sample):
    dev = _gui_hkl_dev(name)
    if sample == dev.sample.name:
        return "The current sample cannot be removed."
    try:
        dev.core.remove_sample(sample)
        return f"Removed sample {sample}."
    except Exception as exc:
        return f"Could not remove sample: {exc}"


def _gui_hkl_set_lattice(name, values):
    """values: {parameter: number}."""
    dev = _gui_hkl_dev(name)
    try:
        for key, value in values.items():
            setattr(dev.sample.lattice, key, float(value))
    except Exception as exc:
        return f"Could not set lattice: {exc}"
    return "Lattice updated. " + _gui_hkl_recompute_ub(dev)


def _gui_hkl_add_reflection(name, pseudos, reals):
    """pseudos/reals are dicts; reals=None uses the current angles."""
    dev = _gui_hkl_dev(name)
    try:
        if reals is None:
            reals = {
                f: getattr(dev, f).position for f in dev.real_positioners._fields
            }
        ordered = [float(reals[f]) for f in dev.real_positioners._fields]
        hkl = tuple(float(pseudos[f]) for f in dev.pseudo_positioners._fields)
        ref = dev.add_reflection(hkl, ordered)
        return f"Added reflection {ref.name}."
    except Exception as exc:
        return f"Could not add reflection: {exc}"


def _gui_hkl_edit_reflection(name, key, pseudos, reals):
    """Reflection.pseudos/.reals are settable, so this edits in place."""
    dev = _gui_hkl_dev(name)
    try:
        ref = dev.sample.reflections[key]
        if pseudos:
            ref.pseudos = {k: float(v) for k, v in pseudos.items()}
        if reals:
            ref.reals = {k: float(v) for k, v in reals.items()}
    except Exception as exc:
        return f"Could not edit reflection: {exc}"
    message = f"Reflection {key} updated."
    if key in list(dev.sample.reflections.order)[:2]:
        message += " " + _gui_hkl_recompute_ub(dev)
    return message


def _gui_hkl_remove_reflection(name, key):
    dev = _gui_hkl_dev(name)
    try:
        if key in list(dev.sample.reflections.order)[:2]:
            return "Cannot remove an orienting reflection; reassign it first."
        dev.sample.reflections.pop(key)
        return f"Removed reflection {key}."
    except Exception as exc:
        return f"Could not remove reflection: {exc}"


def _gui_hkl_set_orienting(name, first, second):
    dev = _gui_hkl_dev(name)
    try:
        refs = dev.sample.reflections
        order = list(refs.order)
        keys = list(refs.keys())
        if first not in keys or second not in keys:
            return "Unknown reflection key."
        if first == second:
            return "The two orienting reflections must differ."
        order[0:1] = [first]
        if len(order) > 1:
            order[1:2] = [second]
        else:
            order.append(second)
        refs.order = order
    except Exception as exc:
        return f"Could not set orienting reflections: {exc}"
    return f"Orienting: {first}, {second}. " + _gui_hkl_recompute_ub(dev)


def _gui_hkl_compute_ub(name):
    try:
        return _gui_hkl_recompute_ub(_gui_hkl_dev(name))
    except Exception as exc:
        return f"Could not compute UB: {exc}"


def _gui_hkl_presets_text(dev):
    """Render the current mode's presets the way the tab shows them."""
    try:
        items = dict(dev.core.presets)
    except Exception:
        return "unavailable"
    return ", ".join(f"{k}={v:g}" for k, v in items.items()) or "none"


def _gui_hkl_set_presets(name, values):
    """Fix (preset) the angles the current mode holds constant.

    ``values`` is ``{axis: number}``; an axis left out has no preset, so
    ``forward()`` falls back to that motor's live position.  Presets change
    *computed* solutions only -- nothing moves.

    hklpy2's ``presets`` setter silently drops any axis that is not constant
    in the current mode, so the names it would have dropped are checked here
    and reported back rather than disappearing without a word.
    """
    dev = _gui_hkl_dev(name)
    try:
        constant = list(dev.core.constant_axis_names)
    except Exception as exc:
        return f"Could not read the constant axes: {exc}"
    try:
        wanted = {k: float(v) for k, v in (values or {}).items()}
    except Exception as exc:
        return f"Fixed angles must be numbers: {exc}"
    ignored = [k for k in wanted if k not in constant]
    try:
        dev.core.presets = {k: v for k, v in wanted.items() if k in constant}
    except Exception as exc:
        return f"Could not set the fixed angles: {exc}"
    message = f"Fixed angles ({dev.core.mode}): {_gui_hkl_presets_text(dev)}"
    if ignored:
        message += (
            " -- ignored " + ", ".join(sorted(ignored))
            + ", not held constant in this mode"
        )
    return message


def _gui_hkl_set_mode(name, mode):
    """Set the mode and freeze the unused detector angle.

    The preset logic follows ``hkl_utils_pete.setmode``: a 'vertical' mode
    holds the horizontal detector (solver 'gamma') at 0 and vice versa,
    translated from solver axis names to this diffractometer's own.

    The detector angle is only defaulted when it has no preset yet.  hklpy2
    keeps presets *per mode* and restores them when a mode is re-selected, so
    assigning a fresh dict here would throw away an angle the user had fixed
    in this mode earlier -- re-picking the mode in the tab would silently undo
    their setting.
    """
    dev = _gui_hkl_dev(name)
    try:
        dev.core.mode = mode
    except Exception as exc:
        return f"Could not set mode: {exc}"

    solver_det = None
    if "vertical" in mode:
        solver_det = "gamma"
    elif "horizontal" in mode:
        solver_det = "delta"

    try:
        axis = None
        if solver_det is not None:
            mapping = dict(
                zip(dev.core.solver_real_axis_names, list(dev.real_positioners._fields))
            )
            axis = mapping.get(solver_det)
        presets = dict(dev.core.presets)
        if axis is not None and axis not in presets:
            presets[axis] = 0
            dev.core.presets = presets
        return f"Mode: {mode} (fixed: {_gui_hkl_presets_text(dev)})"
    except Exception as exc:
        return f"Mode set to {mode}, but presets failed: {exc}"


def _gui_hkl_set_azimuth(name, h2, k2, l2):
    """Write the psi reference vector.

    ``core.extras`` only exists in a psi_constant mode, so this switches into
    one, writes, mirrors onto the psi geometry, and restores the original mode
    -- the same dance as ``hkl_utils_pete.setaz``.
    """
    dev = _gui_hkl_dev(name)
    original = dev.core.mode
    extras = {"h2": float(h2), "k2": float(k2), "l2": float(l2)}
    try:
        modes = list(dev.core.modes)
        for candidate in ("psi_constant_vertical", "psi_constant_horizontal",
                          "psi_constant"):
            if candidate in modes:
                dev.core.mode = candidate
                dev.core.extras = extras
        try:
            _gui_hkl_dev("%(psi)s").core.extras = extras
        except Exception:
            pass
        return f"Psi reference = {h2} {k2} {l2}"
    except Exception as exc:
        return f"Could not set psi reference: {exc}"
    finally:
        try:
            dev.core.mode = original
        except Exception:
            pass


def _gui_hkl_set_psi(name, psi):
    """Freeze psi for the psi_constant modes (hkl_utils_pete.freeze_psi)."""
    dev = _gui_hkl_dev(name)
    if "psi_constant" not in dev.core.mode:
        return f"Psi can only be fixed in a psi_constant mode (now {dev.core.mode})."
    try:
        dev.core.extras = {"psi": float(psi)}
        return f"Psi fixed at {psi}"
    except Exception as exc:
        return f"Could not fix psi: {exc}"


def _gui_hkl_calc(name, h, k, l, psi=None, presets=None):
    """Compute real angles for an hkl, optionally fixing psi and angles first.

    ``presets`` is re-sent with every calculation rather than relied upon from
    an earlier write: the tab applies fixed angles on a debounce timer, so
    pressing Calculate straight after typing could otherwise solve against the
    previous value.
    """
    dev = _gui_hkl_dev(name)
    out = {"device": name, "h": h, "k": k, "l": l}
    if presets is not None:
        try:
            constant = list(dev.core.constant_axis_names)
            dev.core.presets = {
                _k: float(_v) for _k, _v in presets.items() if _k in constant
            }
            out["presets"] = {_k: _gui_hkl_f(_v) for _k, _v in dev.core.presets.items()}
        except Exception as exc:
            out["error"] = f"Could not fix the angles: {exc}"
            return out
    if psi is not None and "psi_constant" in dev.core.mode:
        try:
            dev.core.extras = {"psi": float(psi)}
            out["psi"] = float(psi)
        except Exception as exc:
            out["error"] = f"Could not fix psi: {exc}"
            return out
    try:
        pos = dev.forward(float(h), float(k), float(l))
        out["reals"] = {
            f: _gui_hkl_f(getattr(pos, f)) for f in dev.real_positioners._fields
        }
    except Exception as exc:
        out["error"] = str(exc)
    return out
''' % {"psi": PSI_GEOMETRY, "q": Q_GEOMETRY}
