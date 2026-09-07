"""Threshold-driven filter transmission control during scans.

Watches one detector channel at every scan point.  If the reading is above
``high`` the filters are closed one step and the point is retaken; if it is
below ``low`` they are opened one step.  A point that cannot be brought into
range is accepted as it stands and the scan moves on -- that covers all three
dead ends: already at ``max_transmission`` and still too dim, already at
``min_transmission`` and still too bright, and the filter IOC unable to
realize a further step because the absorber combinations are discrete.

Rejected readings are *dropped*, not saved, so only the accepted count for
each point reaches the primary stream and the scan plot stays clean.

This is off until :func:`attenuation_setup` is called, and while it is off the
scans behave exactly as they did before it existed.

Usage
-----
::

    attenuation_setup(signal=lambda250k.stats5.max_value, low=200, high=3000)
    RE(ascan(psic.eta, -1, 1, 51, 1.0))
    auto_atten.enabled = False          # switch off, keep the settings

For the Lambda 250K, ``stats5`` is the stats plugin fed by ``PROC1`` -- the
whole frame -- while ``stats1``-``stats4`` are fed by ``ROI1``-``ROI4``.  So
``lambda250k.stats5.max_value`` is the brightest pixel on the detector and the
others are per-ROI maxima.  ``max_value`` is already in every event:
``Lambda250kDetector.default_kinds`` puts it in each stats plugin's
``read_attrs``.
"""

__all__ = [
    "auto_atten",
    "attenuation_setup",
    "attenuated_trigger_and_read",
    "one_local_shot",
]

from logging import getLogger

from apsbits.core.instrument_init import oregistry
from bluesky.plan_stubs import create
from bluesky.plan_stubs import drop
from bluesky.plan_stubs import mv as bps_mv
from bluesky.plan_stubs import rd
from bluesky.plan_stubs import read
from bluesky.plan_stubs import save
from bluesky.plan_stubs import sleep as bps_sleep
from bluesky.plan_stubs import trigger
from bluesky.plan_stubs import trigger_and_read
from bluesky.plan_stubs import wait
from bluesky.utils import separate_devices
from bluesky.utils import short_uid
from ophyd.status import Status

logger = getLogger(__name__)

#: ``factor=AUTO`` works the step out from how far off the reading is,
#: instead of stepping by a fixed multiplier.  Spelled ``"auto"`` so it can
#: be typed at the prompt.
AUTO = "auto"

#: Distinguishes "argument not given" from ``None`` in
#: :func:`attenuation_setup`, so ``factor=None`` can mean AUTO rather than
#: "leave the current setting alone".
_UNSET = object()


class _WriteOnly:
    """Write a signal without waiting for its readback to match.

    ``FilterBank.transmission`` is an ``EpicsSignal`` with a separate
    ``write_pv``, and its readback is the transmission the IOC actually
    realized out of a discrete set of absorber combinations -- generally
    *not* the value that was requested.  Put completion is off for it, so
    ``EpicsSignal.set`` falls through to ``Signal._set_and_wait``, which
    polls the readback until it equals the setpoint; with ``write_timeout``
    defaulting to None that is a wait forever.  ``mv`` on the transmission
    would therefore deadlock the RunEngine mid-scan on the first adjustment
    that the IOC could not realize exactly -- which is precisely the case
    this module exists to handle.

    Writing through this shim keeps the write inside an ordinary ``set``
    message, and leaves "did it get there" to the explicit re-read after
    :attr:`AutoAttenuation.settle` -- which is also what drives the
    stuck-filter detection.

    Setting ``put_complete=True`` on the component would be the tidier fix
    and would help every other caller too, but it depends on the IOC
    supporting put completion on that PV, so it wants testing against the
    real filter bank first.
    """

    def __init__(self, signal):
        self.signal = signal
        self.name = signal.name
        self.parent = None

    def set(self, value):
        """Put *value* and report done immediately."""
        self.signal.put(value)
        status = Status(self)
        status.set_finished()
        return status


class AutoAttenuation:
    """Settings for threshold-driven transmission control.

    Attributes
    ----------
    enabled : bool
        Master switch.  Everything below is ignored while this is False.
    signal : str
        Data key of the channel to watch, as it appears in the event -- for
        example ``"lambda250k_stats5_max_value"``.  :func:`attenuation_setup`
        also accepts the signal object and resolves its ``name``.
    low, high : float
        The accept window, in the detector's own units.
    factor : float or None
        Transmission is divided by this when the reading is too high and
        multiplied by it when too low.  ``None`` instead targets the
        geometric centre of the window in a single step, which converges
        faster when the signal is close to linear in transmission.
    min_transmission, max_transmission : float
        Clamp on the requested transmission.  The floor is deliberately far
        below anything the filter bank can realize (1e-10), so the real limit
        is the hardware's rather than an arbitrary number here: when the
        absorbers run out the IOC stops moving, and the stuck detection ends
        the retake loop on its own.  Raise it only to stop the loop
        attenuating past some point you care about.
    settle : float
        Minimum seconds to wait after changing the transmission, before
        watching the readback for movement.  The filter transmission is a
        plain ``EpicsSignal`` put, so nothing else tells us the absorbers
        have finished moving.
    max_tries : int
        Adjustments allowed at one scan point before the point is accepted
        as it stands.
    counter_signal : ophyd.Signal or None
        Optional array counter watched to confirm the plugin has processed
        the frame just acquired -- see :func:`_wait_for_new_frame`.  For the
        Lambda, ``lambda250k.stats5.array_counter``.
    counter_timeout : float
        How long :func:`_wait_for_new_frame` waits before giving up and
        using the reading anyway.
    filters_name : str
        Name of the filter bank in the ``oregistry``.
    record : bool
        Add the filter bank to the recorded devices, so every event carries
        the transmission it was taken at.  Leave this on: without it the
        points are on different scales with nothing to renormalize by.
    """

    def __init__(self):
        self.enabled = False
        self.signal = None
        self.low = None
        self.high = None
        self.factor = AUTO
        self.min_transmission = 1e-10
        self.max_transmission = 1.0
        self.settle = 0.3
        self.move_timeout = 5.0
        self.max_tries = 10
        self.counter_signal = None
        self.counter_timeout = 2.0
        self.filters_name = "filters"
        self.record = True

    @property
    def filters(self):
        """The filter bank, or None when it is not in the ``oregistry``."""
        return oregistry.find(self.filters_name, allow_none=True)

    @property
    def ready(self):
        """True when the settings are complete enough to act on.

        The scan plans test this rather than :attr:`enabled`, so a
        half-configured setting can never take a scan down.
        """
        return (
            self.enabled
            and self.signal is not None
            and self.low is not None
            and self.high is not None
            and self.filters is not None
        )

    def step_factor(self):
        """Return the fixed step factor, or None when the step is calculated.

        ``factor = AUTO`` (the default) means "work the step out from how far
        off the reading is", which lands inside the window in one step when
        the signal is close to linear in transmission.  A number instead
        divides or multiplies the transmission by exactly that each time.
        """
        if self.factor is AUTO or self.factor is None:
            return None
        return float(self.factor)

    def target(self, value, transmission):
        """Return the transmission to try next, or None to accept the point.

        None means "as good as it gets": either *value* is inside the accept
        window, or the step would be clipped back to where we already are.
        Anything else is another transmission to try -- the caller keeps
        coming back here until this returns None or the tries run out, so a
        reading many decades off is walked in as many steps as it takes.
        """
        if value is None:
            return None

        factor = self.step_factor()
        if value > self.high:
            new = (
                transmission / factor
                if factor
                else transmission * self._window_centre() / value
            )
        elif value < self.low:
            new = (
                transmission * factor
                if factor
                else transmission * self._window_centre() / max(value, 1e-30)
            )
        else:
            return None

        new = min(max(new, self.min_transmission), self.max_transmission)
        if _same(new, transmission):
            return None  # clamped back to where we already are
        return new

    def _window_centre(self):
        """Geometric centre of the accept window, the calculated target."""
        return (self.low * self.high) ** 0.5

    def __repr__(self):
        """Summarize the settings on one or two lines."""
        if not self.enabled:
            return "AutoAttenuation(disabled)"
        factor = self.step_factor()
        step = f"x{factor:g}" if factor else "auto (calculated)"
        return (
            f"AutoAttenuation(signal={self.signal!r}, "
            f"low={self.low:g}, high={self.high:g}, step={step}, "
            f"transmission in [{self.min_transmission:g}, "
            f"{self.max_transmission:g}], max_tries={self.max_tries})"
        )


auto_atten = AutoAttenuation()


def _same(first, second):
    """True when two transmissions are equal to within rounding."""
    return abs(first - second) <= 1e-9 * max(abs(second), 1e-9)


def attenuation_setup(
    signal=_UNSET,
    low=_UNSET,
    high=_UNSET,
    factor=_UNSET,
    min_transmission=_UNSET,
    max_transmission=_UNSET,
    settle=_UNSET,
    move_timeout=_UNSET,
    max_tries=_UNSET,
    counter_signal=_UNSET,
    counter_timeout=_UNSET,
    enabled=True,
):
    """Configure automatic transmission control.  Call once per session.

    Only the arguments given are changed, so this can be called again to
    adjust one number without restating the rest.

    Parameters
    ----------
    signal : str or ophyd.Signal
        Channel to watch.  A signal object is resolved to its ``name``, which
        is the data key it appears under in the event, so
        ``signal=lambda250k.stats5.max_value`` is the typo-proof spelling of
        ``signal="lambda250k_stats5_max_value"``.
    low, high : float
        Accept window.  Above *high* the filters close one step and the point
        is retaken; below *low* they open one step.  The point is retaken as
        many times as it takes to get inside the window, up to *max_tries*.
    factor : float or AUTO
        Transmission step.  A number divides the transmission by exactly that
        when the reading is too high and multiplies by it when too low, so
        ``factor=10`` walks 1 -> 0.1 -> 0.01 -> ...  ``AUTO`` (the default)
        works each step out from how far off the reading is, which lands
        inside the window in one step when the signal is close to linear in
        transmission.  Must be greater than 1 if given as a number.
    min_transmission, max_transmission : float
        Clamp on the requested transmission.
    settle : float
        Minimum seconds to wait after a transmission change before looking at
        the readback.
    move_timeout : float
        How long to keep waiting, after *settle*, for the transmission
        readback to move at all.  Only once this expires is the filter bank
        taken to be unable to step further.  Too small a value makes a slow
        filter bank look stuck and ends the retake loop early.
    max_tries : int
        Adjustments allowed per scan point before the point is accepted as it
        stands.  Raise it if a scan spans many decades of intensity and
        *factor* is small.
    counter_signal : ophyd.Signal or None
        Array counter confirming the plugin processed the frame just taken --
        ``lambda250k.stats5.array_counter``.  Strongly recommended for an
        area detector, whose plugin callbacks are asynchronous.
    counter_timeout : float
        Seconds to wait on *counter_signal* before using the reading anyway.
    enabled : bool
        Set False to switch the mechanism off without losing the settings.

    Returns
    -------
    AutoAttenuation
        The settings singleton, also printed.
    """
    # All or nothing.  Validation runs after the writes, because the rules
    # are about the combination (low against high, min against max), so a
    # refusal would otherwise leave half the new settings in place -- and a
    # rejected low/high pair stays behind to fail every later call, including
    # the one trying to correct it.
    snapshot = dict(auto_atten.__dict__)
    try:
        if signal is not _UNSET:
            auto_atten.signal = getattr(signal, "name", signal)
        if counter_signal is not _UNSET:
            auto_atten.counter_signal = counter_signal

        for name, value in (
            ("low", low),
            ("high", high),
            ("factor", factor),
            ("min_transmission", min_transmission),
            ("max_transmission", max_transmission),
            ("settle", settle),
            ("move_timeout", move_timeout),
            ("max_tries", max_tries),
            ("counter_timeout", counter_timeout),
        ):
            if value is not _UNSET:
                setattr(auto_atten, name, value)

        auto_atten.enabled = enabled

        if enabled:
            _validate()
    except Exception:
        auto_atten.__dict__.clear()
        auto_atten.__dict__.update(snapshot)
        raise

    print(auto_atten)
    return auto_atten


def _validate():
    """Refuse settings that would fail silently at the first scan point."""
    missing = [
        name for name in ("signal", "low", "high") if getattr(auto_atten, name) is None
    ]
    if missing:
        raise ValueError(
            "Automatic attenuation needs "
            f"{', '.join(missing)}. For the Lambda 250K try "
            "attenuation_setup(signal=lambda250k.stats5.max_value, "
            "low=200, high=3000)."
        )

    if auto_atten.filters is None:
        raise ValueError(
            f"No {auto_atten.filters_name!r} device in the oregistry, so the "
            "transmission cannot be changed."
        )

    if auto_atten.low >= auto_atten.high:
        raise ValueError(
            f"low ({auto_atten.low:g}) must be below high ({auto_atten.high:g})."
        )

    factor = auto_atten.step_factor()
    if factor is not None and factor <= 1:
        raise ValueError(
            f"factor must be greater than 1, got {factor:g}. It is the "
            "multiplier the transmission steps by, so 10 means 1 -> 0.1 -> "
            f"0.01. Use factor={AUTO!r} to have each step calculated."
        )

    if not 0 < auto_atten.min_transmission <= auto_atten.max_transmission <= 1:
        raise ValueError(
            "Need 0 < min_transmission <= max_transmission <= 1, got "
            f"{auto_atten.min_transmission:g} and "
            f"{auto_atten.max_transmission:g}."
        )

    # A warning, not an error: the scan may pass detectors= explicitly.
    try:
        from ..utils.counters_class import counters

        names = [det.name for det in counters.detectors]
    except Exception:  # noqa: BLE001 - advisory check only
        return
    if names and not any(auto_atten.signal.startswith(name) for name in names):
        logger.warning(
            "Automatic attenuation watches %r, which does not look like it "
            "comes from any selected detector (%s). It will not be in the "
            "event and the filters will be left alone.",
            auto_atten.signal,
            ", ".join(names),
        )


def _wait_for_new_frame(counter_signal, previous):
    """Wait until *counter_signal* moves past *previous*, then return it.

    Area-detector plugin callbacks are asynchronous, so the stats for the
    frame just acquired may not have been computed when the trigger status
    completes.  A stale reading is a cosmetic blemish on an ordinary scan but
    here it *drives a decision*, and can send the loop the wrong way.
    """
    waited = 0.0
    poll = 0.02
    while waited < auto_atten.counter_timeout:
        current = yield from rd(counter_signal)
        if current != previous:
            return current
        yield from bps_sleep(poll)
        waited += poll

    logger.warning(
        "Automatic attenuation: %s did not advance within %g s; the reading "
        "may be from the previous frame.",
        counter_signal.name,
        auto_atten.counter_timeout,
    )
    return previous


def _wait_for_transmission(signal, previous):
    """Wait for the transmission readback to move away from *previous*.

    Returns ``(transmission, stuck)``.  *stuck* is True only once
    :attr:`AutoAttenuation.move_timeout` has expired with the readback still
    where it started, which is the one case that means the filter bank
    cannot step further.

    A blind sleep is not enough here.  The absorbers take time to move, and
    a readback that has simply not caught up yet looks exactly like one that
    cannot move -- which ends the retake loop after a single adjustment and
    accepts a point that is still far out of range.
    """
    yield from bps_sleep(auto_atten.settle)

    waited = 0.0
    poll = 0.05
    while True:
        current = yield from rd(signal)
        if not _same(current, previous):
            return current, False
        if waited >= auto_atten.move_timeout:
            return current, True
        yield from bps_sleep(poll)
        waited += poll


def attenuated_trigger_and_read(devices, name="primary"):
    """Read one point, adjusting the filters until the signal is in range.

    Drop-in replacement for :func:`bluesky.plan_stubs.trigger_and_read`, and
    it defers to that when automatic attenuation is off or unconfigured.

    Readings outside the accept window are ``drop``ped rather than saved, so
    a retaken point leaves no trace in the primary stream.
    """
    if not auto_atten.ready:
        return (yield from trigger_and_read(devices, name))

    filters = auto_atten.filters
    counter = auto_atten.counter_signal
    setter = _WriteOnly(filters.transmission)
    devices = separate_devices(list(devices))
    transmission = yield from rd(filters.transmission)
    stuck = False

    for attempt in range(auto_atten.max_tries + 1):
        frame = (yield from rd(counter)) if counter is not None else None

        group = short_uid("trigger")
        triggered = False
        for obj in devices:
            if hasattr(obj, "trigger"):
                triggered = True
                yield from trigger(obj, group=group)
        if triggered:
            yield from wait(group=group)
        if counter is not None:
            yield from _wait_for_new_frame(counter, frame)

        # Decide inside the bundle, but only from what was just read.  No
        # motion here: save() or drop() first, and move afterwards.
        yield from create(name)
        reading = {}
        for obj in devices:
            one = yield from read(obj)
            if one is not None:
                reading.update(one)

        if auto_atten.signal not in reading:
            yield from save()
            logger.warning(
                "Automatic attenuation: %r is not in this event (%s). "
                "Leaving the filters alone for the rest of the scan.",
                auto_atten.signal,
                ", ".join(sorted(reading)),
            )
            return reading

        value = reading[auto_atten.signal]["value"]
        wanted = auto_atten.target(value, transmission)
        last_try = attempt == auto_atten.max_tries

        if wanted is None or stuck or last_try:
            yield from save()
            if wanted is not None:
                why = (
                    "filters cannot step further"
                    if stuck
                    else (
                        f"out of tries after {auto_atten.max_tries} "
                        "adjustments -- raise auto_atten.max_tries, or "
                        "auto_atten.factor for a bigger step"
                    )
                )
                logger.warning(
                    "Automatic attenuation: accepting %s = %g at "
                    "transmission %g, still outside [%g, %g] (%s).",
                    auto_atten.signal,
                    value,
                    transmission,
                    auto_atten.low,
                    auto_atten.high,
                    why,
                )
            return reading

        yield from drop()
        logger.info(
            "Automatic attenuation: %s = %g outside [%g, %g], "
            "transmission %g -> %g, retaking.",
            auto_atten.signal,
            value,
            auto_atten.low,
            auto_atten.high,
            transmission,
            wanted,
        )
        yield from bps_mv(setter, wanted)

        previous = transmission
        # The setpoint is continuous but the absorber combinations are not,
        # so the IOC may land back where it started.  Only conclude that
        # after move_timeout: a readback that has not caught up yet looks
        # identical to one that cannot move.
        transmission, stuck = yield from _wait_for_transmission(
            filters.transmission, previous
        )
        if stuck:
            logger.warning(
                "Automatic attenuation: asked for transmission %g but %s "
                "stayed at %g for %g s. Taking one more reading here and "
                "moving on. Raise auto_atten.move_timeout if the filter "
                "bank is simply slow.",
                wanted,
                filters.transmission.name,
                transmission,
                auto_atten.move_timeout,
            )


def one_local_shot(detectors, take_reading=None):
    """``per_shot`` hook for :func:`~id6_b.plans.local_scans.count`.

    :func:`~bluesky.plans.count` has no ``per_step``, so the attenuation
    control is attached here instead.
    """
    take_reading = take_reading or attenuated_trigger_and_read
    return (yield from take_reading(list(detectors)))


def attenuation_metadata():
    """Return the settings to record in the run start document, or ``{}``."""
    if not auto_atten.ready:
        return {}
    return dict(
        auto_attenuation=dict(
            signal=auto_atten.signal,
            low=auto_atten.low,
            high=auto_atten.high,
            factor=str(auto_atten.factor),
            min_transmission=auto_atten.min_transmission,
            max_transmission=auto_atten.max_transmission,
            max_tries=auto_atten.max_tries,
        )
    )
