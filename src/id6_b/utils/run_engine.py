"""RunEngine reference for use by plans and utilities.

This module holds references to the RunEngine (RE), BestEffortCallback (bec),
its peak results (peaks) and the databroker catalog (cat) that are populated by
startup.py after initialization.  Plans and utilities import the module, not
the names, so that they see the updated references at call time::

    from ..utils import run_engine as _re_module
    _re_module.RE.md["scan_id"]
"""

RE = None
bec = None
peaks = None
cat = None
