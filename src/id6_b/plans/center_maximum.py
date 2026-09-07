"""Plans that move a positioner onto a feature of the last scan.

``cen`` / ``com`` / ``maxi`` / ``mini`` each move one positioner to a statistic
of the most recent scan, so the standard alignment loop is expressible as a
plan::

    RE(lup(sl1.top, -0.5, 0.5, 41, 1.0))
    RE(cen())                                   # onto the peak
    RE(lup(sl1.top, -0.05, 0.05, 41, 1.0))      # rescan, finer
    RE(cen())

and, more usefully, as a single macro that Ctrl-C interrupts as a whole::

    def align():
        for step in [0.5, 0.05]:
            yield from lup(sl1.top, -step, step, 41, 1.0)
            yield from cen()

With no arguments both the positioner and the detector are inferred from the
last run; an ambiguous inference raises rather than guessing, so a macro fails
loudly at that step instead of moving the wrong motor.

**The data comes from the catalog, never from BestEffortCallback's ``peaks``.**
That is not a preference -- ``peaks`` cannot be used here.  ``BestEffortCallback``
is a ``QtAwareCallback``: under a Qt matplotlib backend (which is what the GUI
and any ``%matplotlib qt`` session have) it does not process documents inline
but emits them to the Qt main thread through a queued signal
(``bluesky/callbacks/mpl_plotting.py``).  BEC's ``stop()`` -- where ``peaks`` is
filled in -- therefore runs *after* the plan has already moved on, so inside a
macro ``peaks`` is still empty at the moment ``cen()`` executes.  Verified by
bisection: the same plan gives ``peaks == {'det': 0.12...}`` mid-plan under the
Agg backend and ``peaks == {}`` under qtAgg.  ``cat.v1.insert`` is a plain
callback, so the run is in the catalog synchronously and is always readable.

Columns are pulled one field at a time via ``to_dask()``; a whole-stream
``read()`` would drag in every area-detector image.  The statistics come from
:func:`~id6_b.utils.peak_statistics.peak_statistics`, which is both the function
the GUI's Scan plot tab uses -- so the button and the plan agree -- and a
faithful reimplementation of ``bluesky.callbacks.fitting.PeakStats``, so the
values also match the ``cen`` table printed in the console.
"""

from logging import getLogger

import numpy as np
from apsbits.core.instrument_init import oregistry

from ..utils import run_engine as _re_module
from ..utils.peak_statistics import peak_statistics
from .local_scans import mv

logger = getLogger(__name__)

__all__ = [
    "cen",
    "cen2",
    "com",
    "maxi",
    "maxi2",
    "mini",
    "mini2",
]

#: Statistics whose value is an ``(x, y)`` pair rather than a bare position.
_PAIR_STATISTICS = ("max", "min")


def _last_run():
    """Return the most recent run, or explain why there is not one."""
    cat = _re_module.cat
    if cat is None:
        raise RuntimeError(
            "No databroker catalog.  Run 'from id6_b.startup import *' first."
        )
    if len(cat) == 0:
        raise RuntimeError("The catalog is empty -- run a scan first.")
    return cat[-1]


def _resolve_field(field):
    """Return the device that reports ``field`` in its hints, or ``None``.

    A hinted field name is *not* a device name -- ``psic.h`` reports
    ``psic_h`` -- so ``oregistry.find()`` is the wrong lookup here (its
    positional argument is a fuzzy ``any_of`` match, which raises
    ``MultipleComponentsFound`` as often as it succeeds).

    A field is reported both by the leaf that owns it and by every container
    above it, so ``psic_h`` matches ``psic`` as well as ``psic.h``.  The device
    hinting the *fewest* fields is the leaf; a longer name breaks a tie.
    """
    best = None
    for device in oregistry.all_devices:
        try:
            fields = device.hints.get("fields", [])
        except Exception:  # noqa: BLE001 - a broken device must not stop the search
            continue
        if field not in fields:
            continue
        rank = (len(fields), -len(device.name))
        if best is None or rank < best[0]:
            best = (rank, device)
    return None if best is None else best[1]


def _scanned_field(run):
    """Return the single hinted x field of *run*."""
    dimensions = run.metadata["start"].get("hints", {}).get("dimensions")
    if not dimensions:
        raise ValueError(
            "The last run has no hinted scan dimension -- give the positioner "
            "explicitly, e.g. cen(sl1.top)."
        )
    if len(dimensions) > 1:
        raise ValueError(
            f"The last run scanned {len(dimensions)} dimensions -- give the "
            "positioner explicitly, e.g. cen(sl1.top)."
        )
    return dimensions[0][0][0]


def _get_positioner(run):
    """Infer the positioner from *run*'s hinted scan dimension."""
    field = _scanned_field(run)
    positioner = _resolve_field(field)
    if positioner is None:
        raise ValueError(
            f"Cannot work out which device reports '{field}' -- give the "
            "positioner explicitly, e.g. cen(sl1.top)."
        )
    return positioner


def _hinted_detectors(run):
    """Return *run*'s hinted scalar detector fields, minus the scanned axes.

    The same selection ``BestEffortCallback`` plots, read out of the primary
    descriptor rather than out of ``peaks``: every field a device hinted, less
    the scan dimensions and anything that is not a scalar -- an area detector
    hints its image too, and an image has no peak.  A non-empty ``shape`` in
    ``data_keys`` is what marks those; a scalar's shape is ``[]``.
    """
    dimensions = run.metadata["start"].get("hints", {}).get("dimensions") or []
    scanned = {field for fields, _stream in dimensions for field in fields}

    try:
        descriptors = run.primary.metadata["descriptors"]
    except (AttributeError, KeyError):
        raise ValueError("The last run has no primary stream to read.") from None

    fields = []
    for descriptor in descriptors:
        data_keys = descriptor.get("data_keys", {})
        for hints in descriptor.get("hints", {}).values():
            for field in hints.get("fields", []):
                if field in scanned or field in fields:
                    continue
                if data_keys.get(field, {}).get("shape"):
                    continue
                fields.append(field)
    return fields


def _get_detector(run):
    """Infer the detector field when *run* hinted exactly one."""
    names = sorted(_hinted_detectors(run))
    if not names:
        raise ValueError(
            "The last scan hinted no scalar detector -- name one with "
            "detector=, e.g. cen(detector='scaler_It')."
        )
    if len(names) > 1:
        raise ValueError(
            "The last scan had more than one hinted detector -- pass one of "
            f"{names} as detector=."
        )
    return names[0]


def _get_current_pos(positioner):
    """Read a positioner's current value, whatever kind of object it is."""
    if hasattr(positioner, "position"):
        return positioner.position
    if hasattr(positioner, "readback"):
        return positioner.readback.get()
    return positioner.get()


def _column(run, field):
    """Read one column of *run*'s primary stream, lazily.

    ``to_dask()[field]`` pulls back a single field.  ``primary.read()`` takes
    no arguments and would load every field, including area-detector images.
    """
    try:
        return np.asarray(run.primary.to_dask()[field].compute(), dtype=float)
    except KeyError:
        raise ValueError(
            f"'{field}' is not a field of the last run's primary stream.  "
            f"Detectors it hinted: {sorted(_hinted_detectors(run))}."
        ) from None


def _position(run, statistic, detector, monitor):
    """Return the position of *statistic* on *detector*, optionally normalised."""
    x = _column(run, _scanned_field(run))
    y = _column(run, detector)
    if monitor is not None:
        reference = _column(run, monitor)
        with np.errstate(divide="ignore", invalid="ignore"):
            y = np.where(reference == 0, np.nan, y / reference)

    value = peak_statistics(x, y)[statistic]
    if value is None:
        label = detector if monitor is None else f"{detector}/{monitor}"
        raise ValueError(f"'{statistic}' is not defined for {label} in the last scan.")
    return value[0] if statistic in _PAIR_STATISTICS else value


def _move_to_pos(statistic, positioner=None, detector=None, monitor=None):
    """Move ``positioner`` to ``statistic`` of the last scan."""
    run = _last_run()
    if positioner is None:
        positioner = _get_positioner(run)
    if detector is None:
        detector = _get_detector(run)

    new_pos = _position(run, statistic, detector, monitor)

    label = detector if monitor is None else f"{detector} / {monitor}"
    message = (
        f"Moving {positioner.name} from {_get_current_pos(positioner)} to "
        f"{new_pos} ({statistic} of {label})."
    )
    logger.info(message)
    print(message)

    yield from mv(positioner, new_pos)


def cen(positioner=None, detector=None, monitor=None):
    """Move to the center of the peak of the last scan.

    The center is the mid-point between the half-maximum crossings, the same
    ``cen`` that ``BestEffortCallback`` prints after each scan.

    Parameters
    ----------
    positioner : ophyd object, optional
        Device to move.  Inferred from the last scan's hinted axis when
        omitted.
    detector : str, optional
        Hinted detector *field* name, e.g. ``"scaler_It"``.  Only needed when
        the scan had more than one hinted detector.
    monitor : str, optional
        Field to divide the detector by before finding the peak, e.g.
        ``"scaler_I0"``.  Off by default.
    """
    yield from _move_to_pos("cen", positioner, detector, monitor)


def com(positioner=None, detector=None, monitor=None):
    """Move to the center of mass of the last scan.

    Parameters
    ----------
    positioner : ophyd object, optional
        Device to move.  Inferred from the last scan's hinted axis when
        omitted.
    detector : str, optional
        Hinted detector field name.  Only needed when the scan had more than
        one hinted detector.
    monitor : str, optional
        Field to divide the detector by before finding the peak.
    """
    yield from _move_to_pos("com", positioner, detector, monitor)


def maxi(positioner=None, detector=None, monitor=None):
    """Move to the maximum of the last scan.

    Parameters
    ----------
    positioner : ophyd object, optional
        Device to move.  Inferred from the last scan's hinted axis when
        omitted.
    detector : str, optional
        Hinted detector field name.  Only needed when the scan had more than
        one hinted detector.
    monitor : str, optional
        Field to divide the detector by before finding the maximum.
    """
    yield from _move_to_pos("max", positioner, detector, monitor)


def mini(positioner=None, detector=None, monitor=None):
    """Move to the minimum of the last scan.

    Parameters
    ----------
    positioner : ophyd object, optional
        Device to move.  Inferred from the last scan's hinted axis when
        omitted.
    detector : str, optional
        Hinted detector field name.  Only needed when the scan had more than
        one hinted detector.
    monitor : str, optional
        Field to divide the detector by before finding the minimum.
    """
    yield from _move_to_pos("min", positioner, detector, monitor)


#: POLAR spelt these ``cen2``/``maxi2``/``mini2``; kept for muscle memory.
cen2 = cen
maxi2 = maxi
mini2 = mini
