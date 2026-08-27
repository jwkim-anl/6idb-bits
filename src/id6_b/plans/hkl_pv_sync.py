"""Keep the HKL-conversion PVs matching the live session.

The beamline's analysis converts detector images using
:class:`~id6_b.devices.hkl_pvs.HklConversionPVs` -- the UB matrix, the
detector's centre channel and the axis direction convention.  This module is
what writes them, from two places:

* a **watcher**, a one-second background thread that pushes whatever has
  changed, so an orientation computed in the console, in the GUI's HKL tab or
  through an MCP client all reach the PVs alike; and
* the **start of every scan**, so the values are right for the data being
  taken even if the watcher has been stopped.

Usage
-----
::

    hkl_pv                       # settings, and what the PVs hold now
    hkl_pv_setup(diffractometer="psic_sim")   # publish the simulator instead
    hkl_pv_setup(convention="spec")           # the ad_hoc geometry's directions
    hkl_pv.enabled = False       # stop writing, settings kept
    hkl_pv.push()                # write once, by hand

Which way each value flows
--------------------------
The session is the source for the UB matrix and for the centre channel:
``psic.sample.UB`` and ``lambda250k.xcenter``/``ycenter`` are pushed out, and a
stray ``caput`` into either PV is corrected on the next tick.

``DetectorSetup:Distance`` is **not** synced -- nothing in the session sources
it.  Set it directly (``hkl_pvs.distance.put(400.644)``); ``repr(hkl_pv)``
reports what it holds.

Note ``xcenter``/``ycenter`` are plain Python attributes on the detector class
(``devices/lambda_detector.py``), not signals, so they default to 256 in a
fresh session regardless of what the PV held.  Use
``lambda250k.image_cen(x, y)`` to set them.  Any future area detector carrying
the same two attribute names works by passing ``detector=`` to
:func:`hkl_pv_setup`.
"""

__all__ = [
    "hkl_pv",
    "hkl_pv_setup",
    "hkl_pv_decorator",
    "hkl_pv_wrapper",
]

import threading
from logging import getLogger

from apsbits.core.instrument_init import oregistry
from bluesky.preprocessors import make_decorator

from ..devices.hkl_pvs import CONVENTIONS
from ..devices.hkl_pvs import DIRECTION_KEYS

logger = getLogger(__name__)

#: Tolerance for deciding a floating-point PV already holds the wanted value.
TOLERANCE = 1e-12


def _close(current, wanted):
    """True when the array *current* already holds *wanted*.

    A PV that is disconnected, empty or the wrong length counts as different,
    so the first push after the IOC comes back writes rather than skips.
    """
    if current is None:
        return False
    try:
        values = [float(v) for v in current]
    except TypeError:
        # A scalar comes back bare rather than as a one-element sequence.
        try:
            values = [float(current)]
        except (TypeError, ValueError):
            return False
    except ValueError:
        return False
    if len(values) != len(wanted):
        return False
    return all(abs(a - b) <= TOLERANCE for a, b in zip(values, wanted, strict=True))


class HklPvSync:
    """Settings for the HKL-conversion PV sync.

    Attributes
    ----------
    enabled : bool
        Master switch.  Nothing is written while this is False, whether the
        watcher thread is running or not.
    device_name : str
        Name of the :class:`~id6_b.devices.hkl_pvs.HklConversionPVs` in
        ``oregistry``.
    diffractometer : str
        Registry name of the diffractometer whose UB is published.
    detector : str
        Registry name of the detector whose ``xcenter``/``ycenter`` are
        published.
    convention : str
        Which direction convention to write; a key of
        :data:`~id6_b.devices.hkl_pvs.CONVENTIONS`.
    interval : float
        Seconds between watcher ticks.
    """

    def __init__(self):
        self.enabled = True
        self.device_name = "hkl_pvs"
        self.diffractometer = "psic"
        self.detector = "lambda250k"
        self.convention = "hklpy2"
        self.interval = 1.0
        self._thread = None
        self._stop = None
        self._last_error = None

    # ----------------------------------------------------------- devices

    @property
    def device(self):
        """The ``HklConversionPVs`` device, or None when it was not created."""
        return oregistry.find(self.device_name, allow_none=True)

    @property
    def diffractometer_device(self):
        """The diffractometer whose UB is published, or None."""
        return oregistry.find(self.diffractometer, allow_none=True)

    @property
    def detector_device(self):
        """The detector whose centre channel is published, or None."""
        return oregistry.find(self.detector, allow_none=True)

    @property
    def ready(self):
        """True when the mechanism should be writing."""
        return bool(self.enabled) and self.device is not None

    @property
    def running(self):
        """True when the watcher thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ values

    def values(self):
        """What the PVs should hold, plus a reason for anything missing.

        Returns a dict with ``ub`` (nine floats, row-major), ``center``
        (``[x, y]``), ``directions`` (key -> string) and ``problems``, a list
        of sentences naming whatever could not be worked out.  A missing piece
        is left as None rather than raising, so one absent device does not
        stop the others being published.
        """
        problems = []

        ub = None
        diffractometer = self.diffractometer_device
        if diffractometer is None:
            problems.append(f"No diffractometer named '{self.diffractometer}'.")
        else:
            try:
                rows = diffractometer.sample.UB
                # hklpy2 types this as list[list[float]], but hkl_soleil can
                # hand back numpy scalars, so coerce as hkl_bridge does.
                ub = [float(v) for row in rows for v in row]
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                problems.append(
                    f"Could not read {self.diffractometer}.sample.UB: {exc}"
                )
                ub = None
            else:
                if len(ub) != 9:
                    problems.append(
                        f"{self.diffractometer}.sample.UB has {len(ub)} elements, "
                        "not 9."
                    )
                    ub = None

        center = None
        detector = self.detector_device
        if detector is None:
            problems.append(f"No detector named '{self.detector}'.")
        else:
            x = getattr(detector, "xcenter", None)
            y = getattr(detector, "ycenter", None)
            if x is None or y is None:
                problems.append(f"{self.detector} has no xcenter/ycenter to publish.")
            else:
                try:
                    center = [float(x), float(y)]
                except (TypeError, ValueError) as exc:
                    problems.append(f"{self.detector} centre is not numeric: {exc}")

        directions = CONVENTIONS.get(self.convention)
        if directions is None:
            problems.append(
                f"Unknown convention '{self.convention}'; expected one of "
                f"{', '.join(sorted(CONVENTIONS))}."
            )
            directions = {}

        return {
            "ub": ub,
            "center": center,
            "directions": dict(directions),
            "problems": problems,
        }

    # ------------------------------------------------------------- write

    def push(self):
        """Write whatever differs from what the PVs hold, and report it.

        Comparison is against the **PV readback**, not against a cache of the
        last write, so the PVs are a real mirror: a value changed behind the
        session's back is put right on the next call.  Returns a dict of what
        was written, empty when everything already agreed.
        """
        if not self.ready:
            return {}

        device = self.device
        wanted = self.values()
        written = {}

        if wanted["ub"] is not None:
            if not _close(device.ub_matrix.get(), wanted["ub"]):
                device.ub_matrix.put(wanted["ub"])
                written["ub_matrix"] = wanted["ub"]

        if wanted["center"] is not None:
            if not _close(device.center_pixel.get(), wanted["center"]):
                device.center_pixel.put(wanted["center"])
                written["center_pixel"] = wanted["center"]

        for key in DIRECTION_KEYS:
            new = wanted["directions"].get(key)
            if new is None:
                continue
            signal = device.direction_signal(key)
            if signal.get() != new:
                signal.put(new)
                written[key] = new

        if written:
            logger.info("HKL PVs updated: %s", ", ".join(sorted(written)))
        return written

    # ----------------------------------------------------------- watcher

    def start(self):
        """Start the watcher thread.  A no-op if it is already running."""
        if self.running:
            return True
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="hkl_pv_sync", daemon=True
        )
        self._thread.start()
        logger.info("HKL PV sync watcher started (every %s s).", self.interval)
        return True

    def stop(self, timeout=5.0):
        """Stop the watcher thread and wait briefly for it to finish."""
        if self._stop is not None:
            self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._stop = None

    def _watch(self):
        """Push, then wait -- so the first write happens without a delay."""
        stop = self._stop
        while True:
            try:
                self.push()
            except Exception as exc:  # noqa: BLE001 - a tick must not kill it
                # Rate-limited: a disconnected PV would otherwise write to the
                # log once a second for the length of the experiment.
                message = f"{type(exc).__name__}: {exc}"
                if message != self._last_error:
                    logger.warning("HKL PV sync failed: %s", message)
                    self._last_error = message
            else:
                if self._last_error is not None:
                    logger.info("HKL PV sync recovered.")
                    self._last_error = None
            # A settings change takes effect on the next tick, deliberately:
            # interval is read here rather than cached at start().
            if stop.wait(max(self.interval, 0.1)):
                break

    # -------------------------------------------------------------- repr

    def __repr__(self):
        state = "ON" if self.enabled else "off"
        lines = [f"\n-- HKL conversion PVs ({state}) --"]
        lines.append(f"Device:         {self.device_name}")
        lines.append(f"Diffractometer: {self.diffractometer}")
        lines.append(f"Detector:       {self.detector}")
        lines.append(f"Convention:     {self.convention}")
        lines.append(
            f"Watcher:        {'running' if self.running else 'stopped'}"
            f" (every {self.interval} s)"
        )
        try:
            wanted = self.values()
            device = self.device
            if device is None:
                lines.append(f"  ! No device named '{self.device_name}'.")
            else:
                distance = device.distance.get()
                lines.append(f"Distance:       {distance} (not synced)")
            if wanted["ub"] is not None:
                rows = [wanted["ub"][i : i + 3] for i in range(0, 9, 3)]
                lines.append("UB:")
                for row in rows:
                    lines.append("  " + "  ".join(f"{v: .6g}" for v in row))
            if wanted["center"] is not None:
                x, y = wanted["center"]
                lines.append(f"Centre pixel:   {x:g}, {y:g}")
            if wanted["directions"]:
                lines.append(
                    "Directions:     "
                    + "  ".join(
                        f"{k}={wanted['directions'][k]}" for k in DIRECTION_KEYS
                    )
                )
            for problem in wanted["problems"]:
                lines.append(f"  ! {problem}")
        except Exception as exc:  # noqa: BLE001 - a repr must never raise
            lines.append(f"  ! {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def __str__(self):
        return self.__repr__()


hkl_pv = HklPvSync()


def hkl_pv_setup(
    enabled=True,
    device=None,
    diffractometer=None,
    detector=None,
    convention=None,
    interval=None,
):
    """Configure (or switch off) the HKL-conversion PV sync.

    Parameters
    ----------
    enabled : bool, optional
        Turn the mechanism on (the default) or off.
    device : str, optional
        Registry name of the ``HklConversionPVs`` device.
    diffractometer : str, optional
        Registry name of the diffractometer whose UB is published.
    detector : str, optional
        Registry name of the detector whose centre channel is published.
    convention : str, optional
        ``"hklpy2"`` (the default) or ``"spec"``.
    interval : float, optional
        Seconds between watcher ticks.

    An argument left out keeps its current setting.  Nothing is applied unless
    the whole call validates, so a refusal cannot leave half the new settings
    behind for the next call to trip over.  Starts the watcher when enabled and
    stops it when not, then writes once so the change is visible immediately.
    """
    snapshot = {k: v for k, v in hkl_pv.__dict__.items() if not k.startswith("_")}
    try:
        if device is not None:
            hkl_pv.device_name = device
        if diffractometer is not None:
            hkl_pv.diffractometer = diffractometer
        if detector is not None:
            hkl_pv.detector = detector
        if convention is not None:
            hkl_pv.convention = convention
        if interval is not None:
            hkl_pv.interval = float(interval)
        hkl_pv.enabled = bool(enabled)

        if hkl_pv.interval <= 0:
            raise ValueError("interval must be a positive number of seconds.")
        if hkl_pv.convention not in CONVENTIONS:
            raise ValueError(
                f"Unknown convention '{hkl_pv.convention}'; expected one of "
                f"{', '.join(sorted(CONVENTIONS))}."
            )
        if hkl_pv.enabled and hkl_pv.device is None:
            raise ValueError(
                f"No device named '{hkl_pv.device_name}' in the registry. "
                "Is the HklConversionPVs entry in devices.yml?"
            )
    except Exception:
        hkl_pv.__dict__.update(snapshot)
        raise

    if hkl_pv.enabled:
        hkl_pv.start()
        hkl_pv.push()
    else:
        hkl_pv.stop()

    print(repr(hkl_pv))


def hkl_pv_wrapper(plan):
    """Publish the current geometry, then run *plan* unchanged.

    A no-op when the mechanism is not armed.  The push is deliberately not
    fatal: a scan must not be lost because a bookkeeping PV was unreachable.
    """
    if hkl_pv.ready:
        try:
            hkl_pv.push()
        except Exception:  # noqa: BLE001 - logged; the scan still runs
            logger.exception("Could not publish the HKL conversion PVs.")
    return (yield from plan)


hkl_pv_decorator = make_decorator(hkl_pv_wrapper)
