"""Local plan decorators for 6-ID-B."""

from bluesky.utils import make_decorator
from bluesky.preprocessors import finalize_wrapper
from bluesky.plan_stubs import mv, null, rd
from ophyd import Kind
from logging import getLogger

from ..utils.counters_class import counters

logger = getLogger(__name__)


def extra_devices_wrapper(plan, extras):
    """Temporarily demote extra devices from hinted to normal during a scan.

    This prevents extra devices (e.g. undulator readbacks added for recording
    purposes) from polluting the BEC plots.
    """
    hinted_stash = []

    def _stage():
        for device in extras:
            for _, component in device._get_components_of_kind(Kind.normal):
                if component.kind == Kind.hinted:
                    component.kind = Kind.normal
                    hinted_stash.append(component)
        yield from null()

    def _unstage():
        for component in hinted_stash:
            component.kind = Kind.hinted
        yield from null()

    def _inner_plan():
        yield from _stage()
        return (yield from plan)

    if len(extras) != 0:
        return (yield from finalize_wrapper(_inner_plan(), _unstage()))
    else:
        return (yield from plan)


def configure_counts_wrapper(plan, detectors, count_time):
    """Set ``preset_monitor`` on all detectors to ``count_time``.

    The original values are stashed and restored at the end of the plan.

    Parameters
    ----------
    plan : generator
        The plan to wrap.
    detectors : list
        Detectors whose ``preset_monitor`` will be adjusted.
    count_time : float or None
        - Positive: count for this many seconds (time monitor) or counts.
        - Negative: count until the selected monitor channel reaches
          ``abs(count_time)`` counts (monitor must not be "Time").
        - None: pass through unchanged.
    """
    original_times = {}

    def setup():
        if count_time < 0:
            if counters.monitor == "Time":
                raise ValueError(
                    'count_time cannot be < 0 because "Time" is the monitor. '
                    "Run counters.plotselect() to change the monitor to a "
                    "scaler channel."
                )
            scaler = counters.monitor_detector
            scaler_channel = getattr(
                scaler.channels, scaler.channels_name_map[counters.monitor]
            )
            yield from mv(scaler_channel.preset, abs(count_time))

        elif count_time > 0:
            args = ()
            for det in detectors:
                original_times[det.preset_monitor] = yield from rd(
                    det.preset_monitor
                )
                args += (det.preset_monitor, count_time)
            yield from mv(*args)

        else:
            raise ValueError("count_time cannot be zero.")

    def reset():
        if count_time < 0:
            scaler = counters.monitor_detector
            scaler_channel = getattr(
                scaler.channels, scaler.channels_name_map[counters.monitor]
            )
            yield from mv(scaler_channel.gate, "N")
        else:
            for mon, time in original_times.items():
                yield from mv(mon, time)

    def _inner_plan():
        yield from setup()
        return (yield from plan)

    if count_time is None:
        return (yield from plan)
    else:
        return (yield from finalize_wrapper(_inner_plan(), reset()))


extra_devices_decorator = make_decorator(extra_devices_wrapper)
configure_counts_decorator = make_decorator(configure_counts_wrapper)
