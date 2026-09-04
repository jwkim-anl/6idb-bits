"""Kernel-side helpers for starting a new SPEC file from the Session tab.

``SPEC_HELPERS_CODE`` is installed in the session by
:meth:`~id6_b.gui.kernel.KernelSession.bootstrap`, next to the HKL, MCP,
attenuation and PVA helper strings, and backs the Session tab's *New data
file* group.

Its own module for the reason ``pva_bridge.py`` is one: this is a single
feature's kernel code with its own validation surface, where
``kernel.HELPERS_CODE`` is the generic set every tab draws on.

Everything goes through
:func:`~id6_b.callbacks.spec_data_file_writer.newSpecFile`, so the GUI is held
to exactly what the console does and there is no second copy of the rules.  It
imports that module directly rather than looking the name up in the session
namespace, because ``startup.py`` only binds ``newSpecFile`` and ``specwriter``
when ``SPEC_DATA_FILES: ENABLE`` is true.

Three facts drive the design, none of them visible at the keyboard:

* **The counter is one behind.**  The next scan is ``RE.md["scan_id"] + 1``
  (``bluesky/run_engine.py:209``), so "the next scan is #1" needs the counter
  at **0**.
* **``scan_id=True`` is how that 0 is reached.**  ``newSpecFile`` computes
  ``scan_id or 1``, and ``True or 1`` is ``True``, which
  ``SpecWriterCallback2.newfile`` maps to ``SCAN_ID_RESET_VALUE`` -- which is
  0.  Passing ``scan_id=1`` would give a first scan numbered 2.
* **An existing file always wins.**  ``newfile`` does
  ``scan_id = max(scan_id or 0, highest)`` before that mapping, so on a file
  that already holds scans the counter follows the file and the reset cannot
  happen.  Forcing it anyway would put duplicate ``#S`` numbers in one file,
  which is corrupt for spec2nexus, so this reports the situation instead.

The base name is set alongside the SPEC file, not separately: it is what
``local_scans`` builds ``<base>_00001_master.hdf`` from, and a counter reset
with a stale base name walks straight into that file.

The sample travels with them for the same reason.  ``experiment_path`` is
``base_experiment_path / sample`` and is the folder the masters go in, so a
preview that described the SPEC file against the typed base name but the
masters against the *old* sample would be answering half the question.  Both
fields default to what is in force -- an empty one means "keep it" -- so
changing only the sample is one field and one button, and the SPEC file it
names is then the one already open.
"""

SPEC_HELPERS_CODE = '''
def _gui_spec_module():
    """The spec_data_file_writer module, or None if it cannot be imported."""
    try:
        from id6_b.callbacks import spec_data_file_writer

        return spec_data_file_writer
    except Exception:
        return None


def _gui_spec_experiment():
    """The experiment singleton, or None."""
    try:
        from id6_b.utils.experiment_utils import experiment

        return experiment
    except Exception:
        return None


def _gui_spec_enabled(module):
    """Whether the SPEC writer is actually subscribed to the RunEngine.

    With SPEC_DATA_FILES: ENABLE false the writer exists but nothing feeds it,
    so a new file would be named and never written.  Better said than found.
    """
    try:
        return bool(module.iconfig.get("SPEC_DATA_FILES", {}).get("ENABLE", False))
    except Exception:
        return False


def _gui_spec_scan_id():
    """RE.md["scan_id"], read live.

    Not from .re_md_dict.yml, which StoredDict writes from a background thread
    and can be some seconds behind -- the preview promises a scan number, so it
    has to be current.
    """
    try:
        from id6_b.utils import run_engine as _re_module

        return int(_re_module.RE.md.get("scan_id", 0))
    except Exception:
        return None


def _gui_spec_highest(path):
    """(number of scans, the counter newfile would set) for an existing file.

    Mirrors SpecWriterCallback2.newfile exactly -- spec2nexus for the scan
    numbers, then int(max(count, biggest) + 0.9999) -- so the preview cannot
    promise a scan number the writer will not use.
    """
    try:
        from spec2nexus.spec import SpecDataFile

        numbers = SpecDataFile(str(path)).getScanNumbers()
    except Exception:
        return None, None
    count = len(numbers)
    biggest = 0 if count == 0 else max(map(float, numbers))
    return count, int(max(count, biggest) + 0.9999)


def _gui_spec_sample_error(sample):
    """Why *sample* cannot be a folder under the base path, or None.

    experiment_path is base_experiment_path / sample, and Path("/a") / "/etc"
    is "/etc" -- an absolute or nested sample silently escapes the base path
    rather than failing, so it is refused by name here.
    """
    import os

    if not sample:
        return None
    if os.path.isabs(sample) or "/" in sample or os.sep in sample:
        return (
            "The sample is one folder under the base path, so its name "
            "cannot contain a path separator."
        )
    if sample in (".", ".."):
        return "'%s' is not a folder name." % sample
    return None


def _gui_spec_exp_path(sample=None):
    """The experiment path *sample* would put in effect, or None.

    Built from base_experiment_path without touching the experiment object:
    the preview has to describe a sample that has not been applied yet.
    """
    import os

    experiment = _gui_spec_experiment()
    if experiment is None:
        return None
    base = getattr(experiment, "base_experiment_path", None)
    name = sample or getattr(experiment, "sample", None)
    if base is None or not name:
        return None
    return os.path.join(str(base), str(name))


def _gui_spec_master(exp_path, base_name, scan_id):
    """The NeXus master local_scans would build for scan *scan_id*, or None.

    local_scans._setup_paths raises FileExistsError on this path, and does so
    whether or not the NeXus writer is enabled, so a counter reset onto an
    occupied number kills the next scan.  Worth knowing before the button.

    *exp_path* is passed in rather than read off the experiment, so this can
    answer for a sample that has been typed but not applied.
    """
    if not exp_path or not base_name or scan_id is None:
        return None
    try:
        from id6_b.plans.local_scans import HDF1_NAME_FORMAT

        path = HDF1_NAME_FORMAT % (str(exp_path), base_name, scan_id)
    except Exception:
        return None
    return path + "_master.hdf"


def _gui_spec_state():
    """The SPEC file, base name and scan counter as they stand now."""
    module = _gui_spec_module()
    if module is None:
        return {
            "available": False,
            "error": "Could not import id6_b.callbacks.spec_data_file_writer.",
        }
    experiment = _gui_spec_experiment()
    try:
        spec_file = str(module.specwriter.spec_filename)
    except Exception:
        spec_file = None
    return {
        "available": True,
        "enabled": _gui_spec_enabled(module),
        "spec_file": spec_file,
        "base_name": getattr(experiment, "file_base_name", None),
        "sample": getattr(experiment, "sample", None),
        "exp_path": _gui_spec_exp_path(),
        "scan_id": _gui_spec_scan_id(),
    }


def _gui_spec_preview(title, sample=None):
    """What _gui_new_spec_file would do.  Reads only; changes nothing.

    Either field may be blank, meaning "keep what is in force" -- so changing
    only the sample needs only the sample typed, and the SPEC file the preview
    then names is the one already open.
    """
    module = _gui_spec_module()
    if module is None:
        return {
            "available": False,
            "error": "Could not import id6_b.callbacks.spec_data_file_writer.",
        }

    import os

    from apstools.utils import cleanupText

    experiment = _gui_spec_experiment()
    title = str(title or "").strip()
    sample = str(sample or "").strip()
    current_sample = getattr(experiment, "sample", None)
    state = {
        "available": True,
        "enabled": _gui_spec_enabled(module),
        "title": title,
        "sample": sample or current_sample,
        "sample_current": current_sample,
        "sample_change": bool(sample) and sample != current_sample,
        "scan_id": _gui_spec_scan_id(),
    }

    problem = _gui_spec_sample_error(sample)
    if problem:
        state["error"] = problem
        return state

    exp_path = _gui_spec_exp_path(sample)
    state["exp_path"] = exp_path
    state["exp_exists"] = bool(exp_path) and os.path.isdir(exp_path)

    if not title:
        # Blank means "keep the current base name", which names the SPEC file
        # already open -- the append branch, and the right answer when the
        # sample is the only thing being changed.
        title = getattr(experiment, "file_base_name", None) or ""
        state["title"] = title
        state["title_default"] = True
    if not title:
        state["error"] = "Enter a base name."
        return state

    clean = cleanupText(title)
    spec_path = module.spec_file_name(title)
    exists = spec_path.exists()

    state["base_name"] = clean
    state["spec_file"] = str(spec_path)
    state["spec_dir"] = os.getcwd()
    state["spec_exists"] = exists

    if exists:
        count, highest = _gui_spec_highest(spec_path)
        state["spec_scans"] = count
        state["next_scan"] = None if highest is None else highest + 1
        # The counter follows the file; the reset cannot be honoured.
        state["reset"] = False
    else:
        state["spec_scans"] = 0
        state["next_scan"] = 1
        state["reset"] = True

    master = _gui_spec_master(exp_path, clean, state["next_scan"])
    state["master_file"] = master
    state["master_exists"] = bool(master) and os.path.exists(master)
    return state


def _gui_new_spec_file(title, sample=None):
    """Start a SPEC file named for *title*, adopting the base name and sample.

    Either argument may be blank, meaning "keep what is in force".
    """
    module = _gui_spec_module()
    if module is None:
        return {
            "available": False,
            "error": "Could not import id6_b.callbacks.spec_data_file_writer.",
        }

    from apstools.utils import cleanupText

    from id6_b.utils import run_engine as _re_module

    experiment = _gui_spec_experiment()
    title = str(title or "").strip()
    sample = str(sample or "").strip()

    problem = _gui_spec_sample_error(sample)
    if problem:
        state = _gui_spec_state()
        state["error"] = problem
        print("! " + problem)
        return state

    if not title:
        title = getattr(experiment, "file_base_name", None) or ""
    if not title:
        state = _gui_spec_state()
        state["error"] = "Enter a base name."
        print("! Enter a base name.")
        return state

    clean = cleanupText(title)
    spec_path = module.spec_file_name(title)
    existed = spec_path.exists()

    sample_changed = False
    if experiment is not None:
        current_sample = getattr(experiment, "sample", None)
        if sample and sample != current_sample:
            sample_changed = True
            try:
                # Sets the sample, adopts the base name, creates
                # <base>/<sample> and chdirs to the base path -- which is
                # where the SPEC file is written, so it has to happen before
                # newSpecFile below.  Both arguments are given, so the
                # input() prompts inside it (stdin is closed in this kernel)
                # are never reached.
                experiment.change_sample(sample=sample, base_name=clean)
            except Exception as exc:
                # No base path set yet, most likely.  Record the sample
                # anyway rather than losing the whole action over a folder
                # that cannot be made.
                experiment.sample = sample
                experiment.file_base_name = clean
                print("! Could not create the sample folder: %s" % exc)
        else:
            # The cleaned form: a typed space would otherwise reach an HDF5
            # file name.  Assigned directly rather than through
            # experiment_change_sample(), which prompts on stdin and also
            # chdirs and mkdirs -- none of it wanted when only the base name
            # is changing.
            experiment.file_base_name = clean

    # scan_id=True, not 1.  See this module's docstring: `True or 1` is True,
    # which newfile maps to SCAN_ID_RESET_VALUE (0), and the next scan is
    # scan_id + 1.  On an existing file newfile's max(...) branch wins first,
    # which is the append behaviour we want.
    module.newSpecFile(title, scan_id=True, RE=_re_module.RE)

    state = _gui_spec_state()
    scan_id = state.get("scan_id")
    # Read back rather than assumed: the route to 0 above is subtle enough
    # that a regression should show in this line, not in the data.
    next_scan = None if scan_id is None else scan_id + 1
    state["created"] = not existed
    state["next_scan"] = next_scan
    state["sample_changed"] = sample_changed

    lines = []
    if sample_changed:
        lines.append("Sample: %s  (%s)" % (sample, state.get("exp_path")))
    if existed:
        count, _highest = _gui_spec_highest(spec_path)
        scans = "" if count is None else " with %d scan(s)" % count
        lines.append(
            "SPEC file: %s (already existed%s -- appending)."
            % (spec_path, scans)
        )
        lines.append(
            "  The scan number was NOT reset: it follows the file. "
            "Next scan will be #%s." % next_scan
        )
        lines.append("  Use a base name that is not in use to start at #1.")
    else:
        lines.append("SPEC file: %s (new)." % spec_path)
        lines.append("  Scan number reset. Next scan will be #%s." % next_scan)
    lines.append("  File base name: %s" % state.get("base_name"))

    master = _gui_spec_master(
        state.get("exp_path"), state.get("base_name"), next_scan
    )
    state["master_file"] = master
    import os

    state["master_exists"] = bool(master) and os.path.exists(master)
    if state["master_exists"]:
        lines.append(
            "  ! %s already exists; the next scan will raise FileExistsError."
            % master
        )
    if not state.get("enabled"):
        lines.append(
            "  ! SPEC data files are disabled in iconfig.yml; "
            "nothing will be written to this file."
        )

    print("\\n".join(lines))
    return state
'''
