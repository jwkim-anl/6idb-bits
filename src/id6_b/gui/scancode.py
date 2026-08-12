"""Build a ``local_scans`` call from form values.

Shared by the Scan tab, which runs one scan now, and the Macro tab, which
writes one into a plan.  The rule worth keeping in one place is the argument
order, which differs between the plans and is silent when got wrong::

    ascan(motor, start, stop, ..., num_points, time)     # len % 3 == 2
    grid_scan(motor, start, stop, num, ..., time)        # len % 4 == 1

Every numeric slot is a **string**.  The Scan tab formats its spin-box floats
before calling; the Macro tab passes its free-text fields straight through, so
a loop variable (``start`` = ``centre - 0.1``) survives into the generated code.
"""

#: Plan name → (takes axes, points are per-axis).  ``ascan``/``lup`` share one
#: point count across axes (a trajectory); the grid plans give each axis its
#: own (a mesh).
PLANS = {
    "count": (False, False),
    "ascan": (True, False),
    "lup": (True, False),
    "grid_scan": (True, True),
    "rel_grid_scan": (True, True),
}


#: ``ascan``/``lup`` hand their arguments to bluesky's ``scan()``, which takes
#: any number of motors, so a third axis is a real trajectory.  The grid plans
#: accept more dimensions too, but the Scan plot's live image is 2D
#: (``rows, columns = shape``), so a third grid axis would be silently
#: collapsed onto the first two -- worse than not offering it.
MAX_TRAJECTORY_AXES = 3
MAX_GRID_AXES = 2

#: Label for the checkbox that reveals each axis row after the first.
AXIS_ORDINALS = ("", "Second axis", "Third axis")


def plan_shape(plan):
    """Return ``(takes_axes, per_axis_points)`` for *plan*."""
    return PLANS.get(plan, (True, False))


def axis_limit(plan):
    """Return how many axis rows *plan* may be given in the GUI."""
    takes_axes, per_axis_points = plan_shape(plan)
    if not takes_axes:
        return 0
    return MAX_GRID_AXES if per_axis_points else MAX_TRAJECTORY_AXES


def active_axes(plan, enabled):
    """Return how many axis rows are in play.

    *enabled* is the checked state of the boxes that add rows 2, 3, ...  Each
    extra row needs the one before it, so the count stops at the first
    unchecked box -- and at the plan's own ceiling.
    """
    limit = axis_limit(plan)
    if not limit:
        return 0
    count = 1
    for index, checked in enumerate(enabled, start=1):
        if index >= limit or not checked:
            break
        count += 1
    return count


def format_number(value):
    """Format a float for generated code, without trailing noise."""
    value = round(float(value), 6)
    if value == int(value):
        return str(int(value))
    return repr(value)


def scan_arguments(plan, rows, shared_points, time_text):
    """Return ``(args, problem)`` -- the positional arguments, as strings.

    *rows* is a sequence of ``(axis, start, stop, points)`` string tuples; the
    ``points`` entry is ignored for the plans that share one point count.
    *problem* is a message when the form cannot make a valid call, in which
    case *args* is None.
    """
    takes_axes, per_axis_points = plan_shape(plan)
    time_text = str(time_text).strip()
    if not time_text:
        return None, "Give a time per point."

    if not takes_axes:
        points = str(shared_points).strip()
        if not points:
            return None, "Give a number of readings."
        return [points, time_text], None

    if not rows:
        return None, "Choose an axis."

    args = []
    for axis, start, stop, points in rows:
        axis = str(axis).strip()
        start = str(start).strip()
        stop = str(stop).strip()
        if not axis:
            return None, "Choose an axis."
        if not start or not stop:
            return None, f"{axis}: give a start and a stop."
        if start == stop:
            return None, f"{axis}: start and stop are the same."
        args += [axis, start, stop]
        if per_axis_points:
            points = str(points).strip()
            if not points:
                return None, f"{axis}: give a number of points."
            args.append(points)

    axes = [str(row[0]).strip() for row in rows]
    if len(axes) != len(set(axes)):
        return None, "Each axis must be a different one."

    if not per_axis_points:
        points = str(shared_points).strip()
        if not points:
            return None, "Give a number of points."
        args.append(points)
    args.append(time_text)
    return args, None


def format_scan_call(plan, rows, shared_points, time_text, fixq=False, detectors=None):
    """Return ``(call_text, problem)`` for a bare ``plan(...)`` call.

    The caller decides what to do with it: the Scan tab wraps it in ``RE(...)``,
    the Macro tab prefixes ``yield from``.

    *detectors* is a list of device names, or None to leave the argument out.
    Omitting it matters -- passing an explicit list makes the plan skip
    ``_setup_detectors()``, which is where the negative-time validation lives.
    """
    args, problem = scan_arguments(plan, rows, shared_points, time_text)
    if problem:
        return None, problem

    keywords = []
    if detectors:
        keywords.append(f"detectors=[{', '.join(detectors)}]")
    if fixq and plan_shape(plan)[0]:
        keywords.append("fixq=True")
    return f"{plan}({', '.join(args + keywords)})", None
