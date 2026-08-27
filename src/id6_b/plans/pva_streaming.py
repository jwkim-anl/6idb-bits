"""Start and stop the PVA streaming data cache around a scan.

Before the scan the cache directory and file name are written and the
``ScanOn`` flag is set; after it the flag is cleared, one second later so the
cache has time to flush.  Which scans this applies to is decided by one
switch -- it is off until :func:`pva_streaming_setup` is called, and while it
is off the scans behave exactly as they did before this module existed.

Usage
-----
::

    experiment_setup("~/6idb-bits", sample="Fe3O4", base_name="scan")
    pva_streaming_setup()                 # arm
    RE(ascan(psic.eta, -1, 1, 51, 1.0))
    #   FilePath -> ~/6idb-bits/Fe3O4
    #   FileName -> pva_scan_00042.h5
    #   ScanOn   -> 1 ... scan ... 1 s ... ScanOn -> 0

    pva_stream.enabled = False            # off, settings kept
    pva_stream                            # settings, and the next file name

The flag is cleared from a ``finalize_wrapper``, so a scan that is Ctrl-C'd,
hits a soft limit or raises cannot leave the cache collecting forever.

**Paths are abbreviated to ``~``.**  ``6idb1:FilePath:Value`` is a 40-byte
Channel Access string -- 39 usable characters -- and
``/home/beams18/USER6IDB`` alone is 22 of them, so a full path leaves almost
nothing for the experiment directory and sample name and the server truncates
the remainder without saying so.  Whatever consumes the PV therefore has to
expand ``~`` itself.  Set ``pva_stream.use_tilde = False`` to send full paths.
"""

__all__ = [
    "pva_stream",
    "pva_streaming_setup",
    "pva_streaming_decorator",
    "pva_streaming_wrapper",
    "pva_metadata",
]

from logging import getLogger
from pathlib import Path

from apsbits.core.instrument_init import oregistry
from bluesky.plan_stubs import mv as bps_mv
from bluesky.plan_stubs import sleep as bps_sleep
from bluesky.preprocessors import finalize_wrapper
from bluesky.preprocessors import make_decorator

from ..utils import run_engine as _re_module
from ..utils.experiment_utils import experiment

# The same write-without-waiting shim auto_attenuation uses; see its docstring
# for why.  These three PVs are served by something that is not an IOC record,
# so whether a write echoes back byte for byte is unknown -- and
# ``EpicsSignal.set()`` polls the readback until it matches, forever.
from .auto_attenuation import _WriteOnly

logger = getLogger(__name__)

#: Channel Access caps a ``DBF_STRING`` at 40 bytes: 39 characters plus NUL.
MAX_STRING = 39


def _next_scan_id():
    """Scan id the upcoming run will get.

    The same expression ``local_scans._setup_paths`` uses.  ``RE.md["scan_id"]``
    is incremented when the run opens, which has not happened yet at the point
    the wrapper and the metadata hook run.
    """
    return _re_module.RE.md["scan_id"] + 1


def _home_relative(path):
    """Return ``~/<rest>`` when *path* is inside the home directory, else None.

    Both sides are **resolved** before comparing.  ``$HOME`` here is
    ``/home/beams/USER6IDB``, an alias for the ``/home/beams18/USER6IDB`` that
    experiment paths are built from, so a textual comparison against
    ``Path.home()`` finds no match at all.
    """
    try:
        home = Path.home().resolve()
        target = Path(path).resolve()
    except (OSError, RuntimeError):  # no home, or an unresolvable path
        return None
    if not target.is_relative_to(home):
        return None
    rest = target.relative_to(home)
    return "~" if str(rest) == "." else f"~/{rest}"


def _check_length(label, value):
    """Warn when *value* will be truncated by the 40-byte CA string field."""
    if len(value) > MAX_STRING:
        logger.warning(
            "PVA streaming %s is %d characters; the PV holds %d and the server "
            "truncates the rest silently: %r",
            label,
            len(value),
            MAX_STRING,
            value,
        )


class PvaStreaming:
    """Settings for the PVA streaming cache.

    Attributes
    ----------
    enabled : bool
        Master switch.  Everything below is ignored while this is False.
    device_name : str
        Name of the :class:`~id6_b.devices.pva_streaming.PvaStreamControl` in
        ``oregistry``.
    name_format : str
        ``printf`` format for the file name, applied to
        ``(file_base_name, scan_id)``.
    stop_delay : float
        Seconds to wait after the scan before clearing ``ScanOn``, so the
        cache can finish writing.
    use_tilde : bool
        Abbreviate the home directory to ``~`` in the directory sent to the
        PV, to fit the 39 characters it holds.
    """

    def __init__(self):
        self.enabled = False
        self.device_name = "pva_stream"
        self.name_format = "pva_%s_%05d.h5"
        self.stop_delay = 1.0
        self.use_tilde = True

    @property
    def device(self):
        """The ``PvaStreamControl`` device, or None when it was not created."""
        return oregistry.find(self.device_name, allow_none=True)

    @property
    def ready(self):
        """True when a scan should drive the cache."""
        return bool(self.enabled) and self.device is not None

    def file_path(self):
        """Directory the cache writes into: ``<working directory>/<sample>``."""
        path = experiment.experiment_path
        if self.use_tilde:
            short = _home_relative(path)
            if short is not None:
                return short
        return str(path)

    def file_name(self, scan_id):
        """``pva_<base name>_<scan number>.h5`` for *scan_id*."""
        return self.name_format % (experiment.file_base_name, scan_id)

    def __repr__(self):
        state = "ON" if self.enabled else "off"
        lines = [f"\n-- PVA streaming ({state}) --"]
        lines.append(f"Device:      {self.device_name}")
        lines.append(f"Name format: {self.name_format}")
        lines.append(f"Stop delay:  {self.stop_delay} s")
        lines.append(f"Home as ~:   {self.use_tilde}")
        try:
            path = self.file_path()
            name = self.file_name(_next_scan_id())
        except Exception:  # noqa: BLE001 - a repr must never raise
            lines.append("Next file:   (experiment not set up)")
        else:
            lines.append(f"Next file:   {path}/{name}")
            for label, value in (("FilePath", path), ("FileName", name)):
                if len(value) > MAX_STRING:
                    lines.append(
                        f"  ! {label} is {len(value)} characters; the PV holds "
                        f"{MAX_STRING} and truncates the rest."
                    )
        return "\n".join(lines)

    def __str__(self):
        return self.__repr__()


pva_stream = PvaStreaming()


def pva_streaming_setup(
    enabled=True,
    device=None,
    name_format=None,
    stop_delay=None,
    use_tilde=None,
):
    """Arm (or disarm) the PVA streaming cache for subsequent scans.

    Parameters
    ----------
    enabled : bool, optional
        Turn the mechanism on (the default) or off.
    device : str, optional
        Registry name of the ``PvaStreamControl`` device.
    name_format : str, optional
        ``printf`` format applied to ``(file_base_name, scan_id)``.
    stop_delay : float, optional
        Seconds between the end of the scan and ``ScanOn`` -> 0.
    use_tilde : bool, optional
        Abbreviate the home directory to ``~`` in the directory PV.

    An argument left out keeps its current setting.  Nothing is applied unless
    the whole call validates, so a refusal cannot leave half the new settings
    behind for the next call to trip over.
    """
    snapshot = dict(pva_stream.__dict__)
    try:
        if device is not None:
            pva_stream.device_name = device
        if name_format is not None:
            pva_stream.name_format = name_format
        if stop_delay is not None:
            pva_stream.stop_delay = float(stop_delay)
        if use_tilde is not None:
            pva_stream.use_tilde = bool(use_tilde)
        pva_stream.enabled = bool(enabled)

        if pva_stream.stop_delay < 0:
            raise ValueError("stop_delay cannot be negative.")
        if pva_stream.enabled and pva_stream.device is None:
            raise ValueError(
                f"No device named '{pva_stream.device_name}' in the registry. "
                "Is the PvaStreamControl entry in devices.yml?"
            )
    except Exception:
        pva_stream.__dict__.clear()
        pva_stream.__dict__.update(snapshot)
        raise

    print(repr(pva_stream))


def pva_metadata():
    """Run-start metadata naming the cache file this scan will produce."""
    if not pva_stream.ready:
        return {}
    return {
        "pva_streaming": {
            "file_path": pva_stream.file_path(),
            "file_name": pva_stream.file_name(_next_scan_id()),
        }
    }


def pva_streaming_wrapper(plan):
    """Set the cache file, flag on before *plan*, flag off after it.

    A no-op that yields *plan* unchanged when the mechanism is not armed.
    """
    if not pva_stream.ready:
        return (yield from plan)

    device = pva_stream.device
    path = pva_stream.file_path()
    name = pva_stream.file_name(_next_scan_id())
    _check_length("FilePath", path)
    _check_length("FileName", name)

    def _final():
        # The cache lags the scan, so give it a moment before closing the file.
        yield from bps_sleep(pva_stream.stop_delay)
        yield from bps_mv(_WriteOnly(device.scan_on), 0)
        logger.info("PVA streaming cache off.")

    yield from bps_mv(
        _WriteOnly(device.file_path),
        path,
        _WriteOnly(device.file_name),
        name,
    )
    yield from bps_mv(_WriteOnly(device.scan_on), 1)
    logger.info("PVA streaming cache on -> %s/%s", path, name)

    # finalize_wrapper runs _final() on success, on an exception and on a
    # Ctrl-C abort, so the flag cannot be left set by a scan that died.
    return (yield from finalize_wrapper(plan, _final()))


pva_streaming_decorator = make_decorator(pva_streaming_wrapper)
