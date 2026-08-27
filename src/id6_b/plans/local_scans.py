"""Modified bluesky scans for 6-ID-B.

All scan plans configure the NeXus writer, collect extra devices (undulators
when scanning energy, diffractometer when scanning motors), and optionally hold
a fixed HKL position (``fixq=True``) during the scan.

Usage
-----
Run ``experiment_setup()`` once per session before using these plans::

    experiment_setup("/data/2024-1/user_name", sample="MyFilm", base_name="scan")
    RE(count(5, 1.0))
    RE(ascan(sample_z, -1, 1, 51, 1.0))
"""

__all__ = [
    "lup",
    "ascan",
    "mv",
    "mvr",
    "grid_scan",
    "rel_grid_scan",
    "count",
    "abs_set",
]

from logging import getLogger
from pathlib import Path

from apsbits.core.instrument_init import oregistry
from apsbits.utils.config_loaders import get_config
from bluesky.plan_patterns import chunk_outer_product_args
from bluesky.plan_stubs import abs_set as bps_abs_set
from bluesky.plan_stubs import move_per_step, mv as bps_mv, rd
from bluesky.plans import count as bp_count
from bluesky.plans import grid_scan as bp_grid_scan
from bluesky.plans import scan
from bluesky.preprocessors import (
    relative_set_decorator,
    reset_positions_decorator,
    subs_decorator,
)
from toolz import partition

from ..callbacks.nexus_data_file_writer import nxwriter
from ..utils import run_engine as _re_module
from ..utils.counters_class import counters
from ..utils.experiment_utils import experiment
from .auto_attenuation import attenuated_trigger_and_read
from .auto_attenuation import attenuation_metadata
from .auto_attenuation import auto_atten
from .auto_attenuation import one_local_shot
from .local_preprocessors import configure_counts_decorator, extra_devices_decorator
from .pva_streaming import pva_metadata
from .pva_streaming import pva_streaming_decorator

try:
    from hklpy2 import current_diffractometer
except (ImportError, AttributeError):
    try:
        from hkl.user import current_diffractometer
    except ImportError:

        def current_diffractometer():
            return None


iconfig = get_config()
logger = getLogger(__name__)

HDF1_NAME_FORMAT = iconfig.get("AREA_DETECTOR", {}).get(
    "HDF5_FILE_TEMPLATE", "%s/%s_%05d"
)


class LocalFlag:
    """Stores flags used to select and run local scans."""

    fixq = False
    hkl_pos = {}


flag = LocalFlag()


def _collect_extras(args):
    """Yield extra devices to record alongside scan detectors.

    Adds undulator energies when scanning the energy axis, and adds the
    current diffractometer when scanning diffractometer motors.
    """
    extras = counters.extra_devices.copy()

    energy = oregistry.find("energy", allow_none=True)
    escan_flag = energy is not None and energy in args
    if escan_flag:
        undulators = oregistry.find("undulators", allow_none=True)
        if undulators is None:
            logger.warning(
                "Undulators device not found. Will not record undulator "
                "energy during scan."
            )
        else:
            for und in (undulators.ds, undulators.us):
                und_track = yield from rd(und.tracking)
                if und_track:
                    extras.append(und.energy)

    diff = current_diffractometer()
    huber_flag = diff is not None and diff.name in str(args)
    if huber_flag:
        extras.append(current_diffractometer())

    # Record the transmission each point was taken at.  Added here, rather
    # than only on the points that were retaken, so the descriptor is the
    # same for every point -- and without it the points are on different
    # scales with nothing to renormalize by.
    if auto_atten.ready and auto_atten.record:
        filters = auto_atten.filters
        if filters not in extras:
            extras.append(filters)

    return extras


def one_local_step(
    detectors, step, pos_cache, take_reading=attenuated_trigger_and_read
):
    """Per-step function for fixQ and auto-attenuation scans.

    After moving to the requested motor positions, moves the diffractometer
    back to the stored HKL position before reading.

    The default *take_reading* adjusts the filter transmission and retakes
    the point when the watched channel is outside its accept window; it
    falls back to ``trigger_and_read`` when automatic attenuation is off,
    so this is the plain fixQ step in that case.
    """
    devices_to_read = list(step.keys()) + list(detectors)
    yield from move_per_step(step, pos_cache)

    if flag.fixq:
        huber = current_diffractometer()
        devices_to_read += [huber]
        args = (
            huber.h,
            flag.hkl_pos[huber.h],
            huber.k,
            flag.hkl_pos[huber.k],
            huber.l,
            flag.hkl_pos[huber.l],
        )
        yield from bps_mv(*args)

    yield from take_reading(devices_to_read)


def _setup_paths(detectors):
    """Build output file paths for the upcoming scan.

    Returns
    -------
    _master_fullpath : str
        Full path of the NeXus master file.
    _dets_file_paths : dict
        ``{detector_name: full_path}`` for area-detector HDF5 files.
    _rel_dets_paths : dict
        ``{detector_name: relative_path}`` used for ExternalLinks in the master.
    """
    if None in (experiment.base_experiment_path, experiment.file_base_name):
        raise ValueError(
            "The experiment needs to be set up. Please run experiment_setup()."
        )

    _scan_id = _re_module.RE.md["scan_id"] + 1

    _master_fullpath = HDF1_NAME_FORMAT % (
        str(experiment.experiment_path),
        experiment.file_base_name,
        _scan_id,
    )
    _master_fullpath += "_master.hdf"

    _dets_file_paths = {}
    _rel_dets_paths = {}
    for det in list(detectors):
        _setup_images = getattr(det, "setup_images", None)
        _flag = getattr(det, "save_image_flag", False)
        if _setup_images and _flag:
            _fp, _rp = _setup_images(
                experiment.experiment_path,
                experiment.file_base_name,
                _scan_id,
                flyscan=False,
            )
            _dets_file_paths[det.name] = str(_fp)
            _rel_dets_paths[det.name] = str(_rp)

    for _fname in [_master_fullpath] + list(_dets_file_paths.values()):
        if Path(_fname).is_file():
            raise FileExistsError(
                f"The file {_fname} already exists. Will not overwrite."
            )

    return _master_fullpath, _dets_file_paths, _rel_dets_paths


def _setup_nxwriter(_base_path, _master_fullpath, _rel_dets_paths):
    """Configure the NeXus writer for the upcoming scan."""
    nxwriter.external_files = _rel_dets_paths
    nxwriter.file_name = str(_master_fullpath)
    nxwriter.file_path = str(_base_path)


def _setup_detectors(is_monitor_time):
    """Return the list of detectors for the scan.

    If ``is_monitor_time`` is True, returns ``counters.detectors`` as-is.
    If False, validates that all selected detectors belong to the monitor's
    scaler and returns only that scaler.
    """
    if is_monitor_time:
        return counters.detectors
    else:
        if counters.monitor == "Time":
            raise ValueError(
                "Monitor is set to 'Time', but count_time < 0 was requested. "
                "Run counters.plotselect() to change the monitor to a scaler "
                "channel."
            )
        monitor_scaler = counters.detectors_plot_options.loc[
            counters.detectors_plot_options["channels"] == counters.monitor
        ]["detectors"].iloc[0]

        if any(
            det_name != monitor_scaler
            for det_name in counters.selected_plot_detectors
        ):
            raise ValueError(
                "You can only count against a monitor channel when all "
                "selected detectors belong to the same scaler. Selected: "
                f"{counters.selected_plot_detectors}. "
                "Run counters.plotselect() to change the selection."
            )

        return [oregistry.find(monitor_scaler)]


def count(num, time, detectors=None, delay=None, per_shot=None, md=None):
    """Take one or more readings from detectors.

    Parameters
    ----------
    num : int
        Number of readings.
    time : float
        Count time in seconds (positive) or monitor counts (negative).
        Cannot be zero.
    detectors : list, optional
        Readable devices.  Defaults to ``counters.detectors``.
    delay : float or iterable, optional
        Delay between readings in seconds.
    per_shot : callable, optional
        Hook for customizing the inner loop.
    md : dict, optional
        Extra metadata for the run start document.
    """
    if time == 0:
        raise ValueError("time must be different from zero.")

    if detectors is None:
        detectors = _setup_detectors(time > 0)

    if per_shot is None and auto_atten.ready:
        per_shot = one_local_shot

    _master_fullpath, _dets_file_paths, _rel_dets_paths = _setup_paths(detectors)
    _setup_nxwriter(
        experiment.experiment_path, _master_fullpath, _rel_dets_paths
    )

    extras = yield from _collect_extras(("",))

    _md = dict(
        hints={"monitor": counters.monitor, "detectors": []},
        base_experiment_path=str(experiment.base_experiment_path),
        experiment_path=str(experiment.experiment_path),
        master_file_path=str(_master_fullpath),
        detectors_file_full_path=_dets_file_paths,
        detectors_file_relative_path=_rel_dets_paths,
    )
    for item in detectors:
        _md["hints"]["detectors"].extend(item.hints["fields"])
    _md.update(attenuation_metadata())
    _md.update(pva_metadata())
    _md.update(md or {})

    @pva_streaming_decorator()
    @configure_counts_decorator(detectors, time)
    @extra_devices_decorator(extras)
    @subs_decorator(nxwriter.receiver)
    def _inner_count():
        yield from bp_count(
            detectors + extras, num=num, delay=delay, per_shot=per_shot, md=_md
        )
        yield from nxwriter.wait_writer_plan_stub()

    return (yield from _inner_count())


def ascan(
    *args,
    detectors=None,
    fixq=False,
    per_step=None,
    md=None,
):
    """Scan one or more motors over a trajectory.

    Parameters
    ----------
    *args :
        ``motor1, start1, stop1, motor2, start2, stop2, ..., num_points, time``
    detectors : list, optional
        Defaults to ``counters.detectors``.
    fixq : bool, optional
        Hold the diffractometer HKL position fixed during the scan.
    per_step : callable, optional
        Hook for customizing the inner loop.
    md : dict, optional
        Extra metadata.
    """
    if len(args) % 3 != 2:
        raise ValueError(
            "Expected motor, start, stop, ..., num_points, time "
            f"(multiple of 3 + 2 args), got {len(args)}."
        )

    time = args[-1]
    args = args[:-1]

    if detectors is None:
        detectors = _setup_detectors(time > 0)

    flag.fixq = fixq
    if fixq:
        huber = current_diffractometer()
        flag.hkl_pos = {
            huber.h: huber.h.get().setpoint,
            huber.k: huber.k.get().setpoint,
            huber.l: huber.l.get().setpoint,
        }

    if per_step is None and (fixq or auto_atten.ready):
        per_step = one_local_step

    _master_fullpath, _dets_file_paths, _rel_dets_paths = _setup_paths(detectors)
    _setup_nxwriter(
        experiment.experiment_path, _master_fullpath, _rel_dets_paths
    )

    extras = yield from _collect_extras(args)

    _md = dict(
        hints={"monitor": counters.monitor, "detectors": [], "scan_type": "ascan"},
        base_experiment_path=str(experiment.base_experiment_path),
        experiment_path=str(experiment.experiment_path),
        master_file_path=str(_master_fullpath),
        detectors_file_full_path=_dets_file_paths,
        detectors_file_relative_path=_rel_dets_paths,
    )
    for item in detectors:
        _md["hints"]["detectors"].extend(item.hints["fields"])
    _md.update(attenuation_metadata())
    _md.update(pva_metadata())
    _md.update(md or {})

    @pva_streaming_decorator()
    @subs_decorator(nxwriter.receiver)
    @configure_counts_decorator(detectors, time)
    @extra_devices_decorator(extras)
    def _inner_ascan():
        yield from scan(detectors + extras, *args, per_step=per_step, md=_md)
        yield from nxwriter.wait_writer_plan_stub()

    return (yield from _inner_ascan())


def lup(
    *args,
    detectors=None,
    fixq=False,
    per_step=None,
    md=None,
):
    """Scan over a trajectory relative to current motor positions.

    Same as :func:`ascan` but offsets start/stop from the current position
    and returns all motors to their original positions afterwards.

    Parameters
    ----------
    *args :
        ``motor1, rel_start1, rel_stop1, ..., num_points, time``
    detectors : list, optional
        Defaults to ``counters.detectors``.
    fixq : bool, optional
        Hold the diffractometer HKL position fixed during the scan.
    per_step : callable, optional
        Hook for customizing the inner loop.
    md : dict, optional
        Extra metadata.
    """
    _md = {"plan_name": "rel_scan"}
    _md.update(md or {})
    motors = [motor for motor, _, _ in partition(3, args[:-2])]

    @reset_positions_decorator(motors)
    @relative_set_decorator(motors)
    def _inner_lup():
        return (
            yield from ascan(
                *args,
                detectors=detectors,
                fixq=fixq,
                per_step=per_step,
                md=_md,
            )
        )

    return (yield from _inner_lup())


def grid_scan(
    *args,
    detectors=None,
    snake_axes=None,
    fixq=False,
    per_step=None,
    md=None,
):
    """Scan over a mesh; each motor on an independent trajectory.

    Parameters
    ----------
    *args :
        ``motor1, start1, stop1, num1, motor2, start2, stop2, num2, ..., time``
    detectors : list, optional
        Defaults to ``counters.detectors``.
    snake_axes : bool or iterable, optional
        Which axes to snake.
    fixq : bool, optional
        Hold the diffractometer HKL position fixed during the scan.
    per_step : callable, optional
        Hook for customizing the inner loop.
    md : dict, optional
        Extra metadata.
    """
    if len(args) % 4 != 1:
        raise ValueError(
            "Expected motor, start, stop, num, ..., time "
            f"(multiple of 4 + 1 args), got {len(args)}."
        )

    time = args[-1]
    args = args[:-1]

    if detectors is None:
        detectors = _setup_detectors(time > 0)

    flag.fixq = fixq
    if fixq:
        huber = current_diffractometer()
        flag.hkl_pos = {
            huber.h: huber.h.get().setpoint,
            huber.k: huber.k.get().setpoint,
            huber.l: huber.l.get().setpoint,
        }

    if per_step is None and (fixq or auto_atten.ready):
        per_step = one_local_step

    _master_fullpath, _dets_file_paths, _rel_dets_paths = _setup_paths(detectors)
    _setup_nxwriter(
        experiment.experiment_path, _master_fullpath, _rel_dets_paths
    )

    extras = yield from _collect_extras(args)

    motors = [m[0] for m in chunk_outer_product_args(args)]

    _md = dict(
        hints={"monitor": counters.monitor, "detectors": [], "scan_type": "gridscan"},
        base_experiment_path=str(experiment.base_experiment_path),
        experiment_path=str(experiment.experiment_path),
        master_file_path=str(_master_fullpath),
        detectors_file_full_path=_dets_file_paths,
        detectors_file_relative_path=_rel_dets_paths,
    )
    for item in detectors:
        _md["hints"]["detectors"].extend(item.hints["fields"])
    _md.update(attenuation_metadata())
    _md.update(pva_metadata())
    _md.update(md or {})

    @pva_streaming_decorator()
    @subs_decorator(nxwriter.receiver)
    @configure_counts_decorator(detectors, time)
    @extra_devices_decorator(extras)
    def _inner_grid_scan():
        yield from bp_grid_scan(
            detectors + extras,
            *args,
            snake_axes=snake_axes,
            per_step=per_step,
            md=_md,
        )
        yield from nxwriter.wait_writer_plan_stub()

    return (yield from _inner_grid_scan())


def rel_grid_scan(
    *args,
    detectors=None,
    snake_axes=None,
    fixq=False,
    per_step=None,
    md=None,
):
    """Scan over a mesh relative to current motor positions.

    Same as :func:`grid_scan` but offsets start/stop from the current position
    and returns all motors to their original positions afterwards.

    Parameters
    ----------
    *args :
        ``motor1, rel_start1, rel_stop1, num1, ..., time``
    detectors : list, optional
    snake_axes : bool or iterable, optional
    fixq : bool, optional
    per_step : callable, optional
    md : dict, optional
    """
    _md = {"plan_name": "rel_grid_scan"}
    _md.update(md or {})
    motors = [m[0] for m in chunk_outer_product_args(args[:-1])]

    @reset_positions_decorator(motors)
    @relative_set_decorator(motors)
    def _inner_rel_grid_scan():
        return (
            yield from grid_scan(
                *args,
                detectors=detectors,
                snake_axes=snake_axes,
                fixq=fixq,
                per_step=per_step,
                md=_md,
            )
        )

    return (yield from _inner_rel_grid_scan())


def mv(*args, **kwargs):
    """Move one or more devices to a setpoint and wait for completion.

    Parameters
    ----------
    *args :
        ``device1, value1, device2, value2, ...``
    """

    def _inner_mv():
        yield from bps_mv(*args, **kwargs)

    return (yield from _inner_mv())


def mvr(*args, **kwargs):
    """Move one or more devices to a relative setpoint and wait.

    Parameters
    ----------
    *args :
        ``device1, delta1, device2, delta2, ...``
    """
    objs = [obj for obj, _ in partition(2, args)]

    @relative_set_decorator(objs)
    def _inner_mvr():
        return (yield from mv(*args, **kwargs))

    return (yield from _inner_mvr())


def abs_set(*args, **kwargs):
    """Set a value, optionally waiting for completion.

    Parameters
    ----------
    *args, **kwargs :
        Passed directly to ``bluesky.plan_stubs.abs_set``.
    """

    def _inner_abs_set():
        yield from bps_abs_set(*args, **kwargs)

    return (yield from _inner_abs_set())
