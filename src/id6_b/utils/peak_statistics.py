"""Peak statistics for a single 1D curve.

One implementation, used by two callers that must not disagree:

* :mod:`id6_b.plans.center_maximum` -- the ``cen``/``maxi``/``mini``/``com``
  plans that actually move a positioner, running in the Bluesky session.
* :mod:`id6_b.gui.tabs.scanplot` -- the peak readout, the dotted marker lines
  and the four "Go to" buttons, running in the *GUI* process.

The definitions are copied from :class:`bluesky.callbacks.fitting.PeakStats`
(with its default ``edge_count=None``, i.e. no background subtraction), so the
numbers reported here match the ``cen``/``com``/``max`` table that
``BestEffortCallback`` prints in the console after every scan.

The module is deliberately dependency-light -- only :mod:`numpy` -- so the GUI
process can import it without pulling in ophyd, bluesky or EPICS.
"""

import math

import numpy as np

__all__ = ["STATISTICS", "peak_statistics"]

#: Statistics returned by :func:`peak_statistics`, in display order.
STATISTICS = ("cen", "com", "max", "min", "fwhm")


def _finite_pairs(x, y):
    """Return ``x``/``y`` as float arrays with non-finite pairs dropped.

    The GUI divides a detector by a monitor and yields ``NaN`` wherever the
    monitor read zero, so holes in the middle of a curve are normal here.
    """
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    size = min(x.size, y.size)
    x, y = x[:size], y[:size]
    keep = np.isfinite(x) & np.isfinite(y)
    return x[keep], y[keep]


def _center_of_mass(x, y):
    """Index-space centre of mass mapped onto ``x``.

    Matches ``PeakStats``, which takes the centre of mass over *sample
    indices* and then interpolates that fractional index onto ``x``.  For
    evenly spaced ``x`` this equals ``sum(x * y) / sum(y)``; for uneven
    spacing it does not, and matching the console table is what matters.
    """
    total = float(np.sum(y))
    if not math.isfinite(total) or total == 0.0:
        return None
    index = float(np.sum(y * np.arange(y.size, dtype=float)) / total)
    if not math.isfinite(index):
        return None
    value = float(np.interp(index, np.arange(x.size, dtype=float), x))
    return value if math.isfinite(value) else None


def _crossings(x, y):
    """Interpolated x positions where ``y`` crosses its own half maximum."""
    mid = (float(np.max(y)) + float(np.min(y))) / 2
    above = (y > mid).astype(int)
    found = []
    for index in np.where(np.diff(above))[0]:
        span_x = x[index : index + 2]
        span_y = y[index : index + 2] - mid
        dx = span_x[1] - span_x[0]
        dy = span_y[1] - span_y[0]
        if dx == 0 or dy == 0:
            continue
        found.append(float(-span_y[0] / (dy / dx) + span_x[0]))
    return [value for value in found if math.isfinite(value)]


def peak_statistics(x, y):
    """Compute peak statistics for one curve.

    Parameters
    ----------
    x, y : sequence of float
        The curve, in acquisition order.  ``x`` is *not* sorted, so that the
        result matches ``PeakStats``, which also works in event order.
        Non-finite pairs are dropped.

    Returns
    -------
    dict
        Keys ``"cen"``, ``"com"``, ``"max"``, ``"min"``, ``"fwhm"``,
        ``"crossings"`` and ``"npoints"``.  ``cen``, ``com`` and ``fwhm`` are
        floats; ``max`` and ``min`` are ``(x, y)`` tuples, as in
        ``PeakStats``.  A statistic that is not defined for this curve is
        ``None`` rather than an error -- a monotonic ramp has no half-maximum
        crossing, so it has no ``cen`` and no ``fwhm``, but it still has a
        ``max``.
    """
    x, y = _finite_pairs(x, y)
    result = dict.fromkeys(STATISTICS)
    result["crossings"] = []
    result["npoints"] = int(x.size)
    if x.size == 0:
        return result

    argmin = int(np.argmin(y))
    argmax = int(np.argmax(y))
    result["min"] = (float(x[argmin]), float(y[argmin]))
    result["max"] = (float(x[argmax]), float(y[argmax]))
    result["com"] = _center_of_mass(x, y)

    if x.size < 2:
        return result

    crossings = _crossings(x, y)
    result["crossings"] = crossings
    if crossings:
        result["cen"] = float(np.mean(crossings))
        if len(crossings) >= 2:
            result["fwhm"] = float(abs(crossings[-1] - crossings[0]))
    return result
