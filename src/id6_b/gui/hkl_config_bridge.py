"""Kernel-side helpers for saving and loading the hklpy2 configuration.

``HKL_CONFIG_HELPERS_CODE`` is installed in the session by
:meth:`~id6_b.gui.kernel.KernelSession.bootstrap`, next to the HKL, MCP,
attenuation, PVA and SPEC helper strings, and backs the HKL tab's
*Configuration* group.

Its own module for the reason ``spec_bridge.py`` is one: a single feature's
kernel code with its own validation surface.  Deliberately **not** appended to
``hkl_bridge.py``, whose helper string ends ``''' % {...}`` -- every literal
``%`` inside that one has to be doubled, and this code has none to spend.

Everything goes through the three non-interactive functions in
:mod:`id6_b.utils.hkl_utils_pete`
(:func:`~id6_b.utils.hkl_utils_pete.export_diffractometer_config`,
:func:`~id6_b.utils.hkl_utils_pete.apply_diffractometer_config`,
:func:`~id6_b.utils.hkl_utils_pete.scan_diffractometer_configs`), so the GUI
writes the file the console writes and restores it the way the console does.
The console's own three functions cannot be called from here at all: they block
on ``input()`` and the GUI kernel runs with ``allow_stdin=False``.

Three things drive the design:

* **``hkl_utils_pete`` is imported lazily, inside each helper.**  Its module
  scope builds a second ``RunEngine`` and resolves four devices out of
  ``oregistry``, so it must never be imported at GUI-process import time.  In
  the kernel ``startup.py`` has already imported it, so the lazy import is free.

* **The helpers act on the diffractometer they are given**, never on hklpy2's
  process-global ``get_diffractometer()`` -- which is set to the real ``psic``
  when ``hkl_utils_pete`` is imported, while the HKL tab has its own
  ``psic``/``psic_sim`` selector.

* **Scans are identified by uid, not scan number.**  The scan counter is reset
  whenever a new SPEC file is started, so a catalog holds several runs numbered
  1.  A picker keyed on the number would silently resolve to the most recent
  match with nothing on screen saying which run it used.

The two listings are one-off requests, never polled: a directory walk plus a
YAML parse per file, and roughly 40 ms per catalog run, is not something to do
once a second for a group nobody is looking at.

Paths arrive absolute.  A relative one would be resolved against the kernel's
*live* working directory, which is not the directory the GUI was launched in --
``_gui_new_spec_file`` chdirs -- so the saver refuses one rather than writing
somewhere nobody asked for.
"""

HKL_CONFIG_HELPERS_CODE = '''
#: Most rows a configuration listing will return.  A listing is rendered into
#: the reply by IPython's pretty printer, which truncates a sequence past 1000
#: items by writing a literal `...` into it -- which then reaches the GUI as an
#: `Ellipsis` and raises inside Qt.  Nothing here comes near that, but the cap
#: is what makes sure of it.
_GUI_HKLCONF_MAX_ROWS = 200


def _gui_hklconf_dev(name):
    """The named diffractometer from the registry."""
    return oregistry.find(name)


def _gui_hklconf_utils():
    """The hkl_utils_pete module.

    Imported here rather than at module scope: its import builds a RunEngine
    and resolves devices, neither of which belongs in the GUI process.
    """
    from id6_b.utils import hkl_utils_pete

    return hkl_utils_pete


def _gui_hklconf_summary(config):
    """Describe a configuration for one row of the picker.

    Reports the sample the configuration was saved on, how many orienting
    reflections it has and how many samples it carries.  hklpy2 always writes
    a placeholder sample literally named "sample"; it is counted out, the way
    `_prompt_clear_mode` leaves it out of its listing.
    """
    samples = config.get("samples") or {}
    named = [_s for _s in samples if _s != "sample"]
    current = config.get("sample_name") or ""
    entry = samples.get(current) or {}
    order = entry.get("reflections_order") or []
    solver = config.get("solver") or {}
    return {
        "sample": current,
        "samples": len(named),
        "reflections": len(order),
        "geometry": solver.get("geometry") or "",
    }


def _gui_hklconf_files(directory=None):
    """The configuration files in *directory*, newest first.

    *directory* defaults to the kernel's live working directory -- which is
    reported back, since it is not necessarily the one the GUI was started in.
    A file that will not parse is listed with its error rather than dropped:
    it is still on disk, and saying why it cannot be offered beats leaving the
    operator to wonder where it went.
    """
    import os
    import time

    try:
        _hup = _gui_hklconf_utils()
        suffix = _hup.CONFIG_SUFFIX
    except Exception as exc:
        return {"directory": "", "files": [], "error": f"{type(exc).__name__}: {exc}"}

    where = os.path.abspath(directory or os.getcwd())
    try:
        names = [_n for _n in os.listdir(where) if _n.endswith(suffix)]
    except Exception as exc:
        return {
            "directory": where,
            "files": [],
            "error": f"Cannot list {where}: {type(exc).__name__}: {exc}",
        }

    rows = []
    for name in names:
        path = os.path.join(where, name)
        try:
            modified = os.path.getmtime(path)
        except Exception:
            modified = 0.0
        row = {
            "path": path,
            "name": name,
            "modified": modified,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)),
            "sample": "",
            "samples": 0,
            "reflections": 0,
            "geometry": "",
            "error": "",
        }
        try:
            import yaml

            with open(path) as _f:
                config = yaml.safe_load(_f)
            if not isinstance(config, dict):
                raise ValueError("not a configuration mapping")
            row.update(_gui_hklconf_summary(config))
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)

    rows.sort(key=lambda _r: _r["modified"], reverse=True)
    return {"directory": where, "files": rows[:_GUI_HKLCONF_MAX_ROWS]}


def _gui_hklconf_scans(device, limit=15):
    """The newest runs that carry an hklpy2 orientation, newest first.

    Each row is summarised for *device* when the run holds that
    diffractometer's configuration, and for its only one when it holds exactly
    one under a different name -- `psic` and `psic_sim` share the geometry and
    the axis names, so a simulator configuration restores onto the real device
    and the other way round.  Runs with no orientation at all are skipped.

    *limit* runs are inspected, not returned: the walk costs roughly 40 ms a
    run, so this is the knob that bounds how long the Refresh takes.
    """
    try:
        from id6_b.utils import run_engine as _re_module

        cat = getattr(_re_module, "cat", None)
        if cat is None:
            raise RuntimeError(
                "No catalog is available; run the id6_b startup first."
            )
        from hklpy2.run_utils import get_run_orientation
    except Exception as exc:
        return {"scans": [], "error": f"{type(exc).__name__}: {exc}"}

    import time

    try:
        uids = list(cat.v2)[: int(limit)]
    except Exception as exc:
        return {"scans": [], "error": f"Cannot list the catalog: {exc}"}

    rows = []
    for uid in uids:
        try:
            run = cat.v2[uid]
            info = get_run_orientation(run)
            if not info:
                continue
            names = list(info)
            # A run holding the orientation under another name is still
            # offered -- psic and psic_sim are the same geometry -- with
            # `matches` False so the row can say which one it came from.
            chosen = device if device in names else names[0]
            start = run.metadata.get("start") or {}
            when = start.get("time") or 0.0
            row = {
                "uid": str(start.get("uid") or uid),
                "scan_id": start.get("scan_id"),
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(when)),
                "plan": start.get("plan_name") or "",
                "devices": names,
                "device": chosen,
                "matches": device in names,
            }
            row.update(_gui_hklconf_summary(info[chosen] or {}))
            rows.append(row)
        except Exception:
            # One unreadable run must not cost the whole listing.
            continue
    return {"scans": rows[:_GUI_HKLCONF_MAX_ROWS]}


def _gui_hklconf_save(name, path):
    """Write *name*'s configuration to *path*.  Prints its own report.

    The path must be absolute.  A relative one would land in the kernel's
    live working directory, which the GUI cannot see and which anything that
    chdirs can move out from under it.
    """
    import os

    try:
        if not os.path.isabs(str(path)):
            print(f"! Not saved: '{path}' is not an absolute path.")
            return
        dev = _gui_hklconf_dev(name)
        _gui_hklconf_utils().export_diffractometer_config(dev, path)
    except Exception as exc:
        print(f"! Not saved: {type(exc).__name__}: {exc}")
        return
    print(f"Configuration of {name} written to '{path}'.")


def _gui_hklconf_load_file(name, path, clear=True):
    """Restore *path* onto diffractometer *name*.  Prints its own report."""
    try:
        dev = _gui_hklconf_dev(name)
        _hup = _gui_hklconf_utils()
    except Exception as exc:
        print(f"! Not loaded: {type(exc).__name__}: {exc}")
        return
    how = "Replacing" if clear else "Adding to"
    print(f"{how} the configuration of {name} from '{path}'...")
    try:
        _hup.apply_diffractometer_config(dev, str(path), clear=bool(clear))
    except Exception as exc:
        print(f"! Not loaded: {type(exc).__name__}: {exc}")
        return
    print(f"Configuration of {name} restored from '{path}'.")


def _gui_hklconf_load_scan(name, uid, clear=True):
    """Restore the orientation saved in run *uid*.  Prints its own report.

    A run that holds the configuration under a different name is accepted
    when it holds exactly one -- `psic` and `psic_sim` are the same geometry
    on different motors.  A run holding several is refused by name, with the
    available ones listed.
    """
    try:
        dev = _gui_hklconf_dev(name)
        _hup = _gui_hklconf_utils()
        info = _hup.scan_diffractometer_configs(uid)
    except Exception as exc:
        print(f"! Not loaded: {type(exc).__name__}: {exc}")
        return

    names = list(info)
    if name in names:
        chosen = name
    elif len(names) == 1:
        chosen = names[0]
        print(f"The run holds '{chosen}', not '{name}'; restoring it onto {name}.")
    else:
        print(
            f"! Not loaded: the run holds {', '.join(names)}, none of them "
            f"'{name}'. Select that diffractometer and try again."
        )
        return

    how = "Replacing" if clear else "Adding to"
    print(f"{how} the configuration of {name} from scan {uid}...")
    try:
        _hup.apply_diffractometer_config(dev, info[chosen], clear=bool(clear))
    except Exception as exc:
        print(f"! Not loaded: {type(exc).__name__}: {exc}")
        return
    print(f"Configuration of {name} restored from scan {uid}.")
'''
