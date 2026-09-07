"""Session defaults applied automatically when the GUI starts.

``experiment_setup()`` and ``counters()`` have to be answered before the first
scan of every session, and the answers are nearly always the same ones.  This
module holds a kernel code string that applies them from the ``GUI.AUTO_SETUP``
block of ``configs/iconfig.yml``, appended to the GUI's bootstrap cell next to
the other ``_gui_*`` helper strings.

Only the GUI does this.  The plain IPython workflow (``ipython -i -c "from
id6_b.startup import *"``) is untouched, as is the queue server -- both still
prompt, because neither reads this module.

The configuration is read *in the kernel* rather than in the GUI process:
``iconfig`` is already in the session namespace after ``from id6_b.startup
import *``, so nothing here has to find or parse the file a second time.
"""

#: Installed in the kernel by :meth:`~id6_b.gui.kernel.KernelSession.bootstrap`,
#: which appends it to the startup cell and so runs it once the devices exist.
SESSION_SETUP_CODE = '''\
def _gui_auto_setup():
    """Apply the session defaults from iconfig's ``GUI.AUTO_SETUP`` block.

    Never raises.  A bad configuration is reported and the session continues:
    this runs at the tail of the bootstrap cell, and letting it throw would
    leave a session that is merely un-configured looking like one that failed
    to start.
    """
    _cfg = (globals().get("iconfig") or {}).get("GUI", {}).get("AUTO_SETUP", {})
    if not _cfg.get("ENABLE", False):
        return

    print(
        "\\nApplying GUI.AUTO_SETUP from iconfig.yml -- run experiment_setup() "
        "or counters() to change these for this session."
    )

    _exp = _cfg.get("EXPERIMENT") or {}
    try:
        experiment_setup(
            _exp.get("BASE_PATH", ""),        # "" -> the current directory
            _exp.get("SAMPLE", "test"),
            _exp.get("FILE_BASE_NAME", "scan"),
        )
    except Exception as _exc:
        print(f"AUTO_SETUP: experiment_setup() failed: {_exc!r}")

    _ctr = _cfg.get("COUNTERS") or {}
    if not _ctr.get("DETECTORS"):
        return

    try:
        _options = counters.detectors_plot_options

        def _row(value):
            """Row index in the plot-options table, from a name or an index."""
            if isinstance(value, int) and not isinstance(value, bool):
                if value not in _options.index:
                    raise ValueError(
                        f"row {value} is not one of the "
                        f"{len(_options)} plotting channels"
                    )
                return int(value)
            _hits = _options.index[_options["channels"] == value].tolist()
            if not _hits:
                raise ValueError(
                    f"no plotting channel named {value!r}; available: "
                    + ", ".join(repr(c) for c in _options["channels"])
                )
            return int(_hits[0])

        _dets = [_row(v) for v in _ctr["DETECTORS"]]
        _mon = _row(_ctr.get("MONITOR", "Time"))
        if _mon in _dets:
            raise ValueError(
                f"the monitor {_options.loc[_mon]['channels']!r} is also "
                "selected as a detector"
            )
        # Validated first because plotselect() falls back to input() on a bad
        # argument, and stdin is closed in this kernel.
        counters.plotselect(dets=_dets, mon=_mon)
    except Exception as _exc:
        print(f"AUTO_SETUP: counters selection not applied: {_exc}")


_gui_auto_setup()
'''
