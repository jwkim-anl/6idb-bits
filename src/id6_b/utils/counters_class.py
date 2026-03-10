"""Counters class — holds monitor and detectors for scans."""

from collections.abc import Iterable
from logging import getLogger

from apsbits.core.instrument_init import oregistry
from ophydregistry import ComponentNotFound
from pandas import DataFrame

logger = getLogger(__name__)

__all__ = ["counters"]

# Priority order for detector display. Add detectors here as they are added
# to the instrument (e.g. "lambda250k", "vortex").
IDEAL_ORDER = [
    "scaler",
    "lambda250k",
]


class CountersClass:
    """Holds monitor and detectors for scans.

    The singleton ``counters`` instance is imported by startup and is the
    default detector list passed to scan plans.

    Attributes
    ----------
    detectors : list
        Devices that will be triggered and read during scans.
    extra_devices : list
        Additional devices to read but not plot.
    monitor : str
        Name of the scaler channel used as monitor (default: ``"Time"``).

    Examples
    --------
    Select "Ion Ch 4" as detector, "Ion Ch 1" as monitor interactively::

        counters()

    Or non-interactively::

        counters.plotselect(dets=[2], mon=1)
    """

    def __init__(self, order=IDEAL_ORDER):
        super().__init__()
        self._dets = []
        self._mon = "Time"
        self._extra_devices = []
        self._order = order

    def __repr__(self):
        read_names = [item.name for item in (self.detectors + self.extra_devices)]
        return (
            "Counters settings\n"
            " Monitor:\n"
            f"  Scaler channel = '{self._mon}'\n"
            " Detectors:\n"
            f"  Read devices = {read_names}\n"
            f"  Plot components = {self.plot_names}"
        )

    def __str__(self):
        return self.__repr__()

    def __call__(self):
        """Interactively select plotting channels and monitor."""
        self.plotselect()

    @property
    def plot_names(self):
        plot_names = []
        for item in self.detectors:
            plot_names.extend(item.hints["fields"])
        return plot_names

    @property
    def _available_scalers(self):
        return oregistry.findall("scaler", allow_none=True)

    @property
    def detectors(self):
        return self._dets

    @property
    def selected_plot_detectors(self):
        return [det.name for det in self.detectors if len(det.hints["fields"]) > 0]

    @property
    def monitor(self):
        return self._mon

    @property
    def monitor_detector(self):
        if self.monitor == "Time":
            return self._available_scalers
        else:
            name = self.detectors_plot_options[
                self.detectors_plot_options["channels"] == self.monitor
            ].iloc[0]["detectors"]
            return oregistry.find(name)

    @property
    def extra_devices(self):
        return self._extra_devices

    @extra_devices.setter
    def extra_devices(self, value):
        try:
            value = list(value)
        except TypeError:
            value = [value]

        self._extra_devices = []
        for item in value:
            if isinstance(item, str):
                raise ValueError(
                    "Input has to be a device instance, not a device name, "
                    f"but '{item}' was entered."
                )
            if item not in self.detectors:
                self._extra_devices.append(item)

    @property
    def _available_detectors(self):
        try:
            _dets = oregistry.findall("detector")
        except ComponentNotFound:
            logger.warning("No detectors were found in oregistry.")
            _dets = []

        dets = []
        for name in self._order:
            dev = oregistry.find(name, allow_none=True)
            if dev in _dets:
                _dets.remove(dev)
            if dev is not None:
                dets.append(dev)

        return dets + _dets

    @property
    def detectors_plot_options(self):
        """Return a DataFrame of available plotting channels across all detectors."""
        table = dict(detectors=[], channels=[])

        if len(self._available_scalers) > 0:
            table["detectors"].append("scalers")
            table["channels"].append("Time")

        for det in self._available_detectors:
            _options = getattr(det, "plot_options", [])

            # "Time" is already added above for all scalers combined.
            if det in self._available_scalers:
                _options = _options[1:]

            table["channels"] += _options
            table["detectors"] += [det.name for _ in range(len(_options))]

        return DataFrame(table)

    def select_plot_channels(self, selection):
        plot_options = self.detectors_plot_options

        if 0 in selection:
            selection.remove(0)
            for scaler in self._available_scalers:
                selection.append(len(plot_options))
                plot_options.loc[len(plot_options)] = [
                    scaler.name,
                    scaler.channels.chan01.chname.get(),
                ]

        groups = plot_options.iloc[list(selection)].groupby("detectors")

        dets = []
        for name, group in groups:
            det = oregistry.find(name)
            getattr(det, "select_plot")(list(group["channels"].values))
            dets.append(det)

        for scaler in self._available_scalers:
            if scaler not in dets:
                dets.append(scaler)
                scaler.select_plot_channels([""])

        self._dets = dets

    def plotselect(self, dets=None, mon=None):
        """Select which channels to plot and which to use as monitor.

        Parameters
        ----------
        dets : list of int or None
            Row indices from ``detectors_plot_options`` to plot.  If *None*,
            prompts interactively.
        mon : int or None
            Row index from ``detectors_plot_options`` for the monitor.  If
            *None*, prompts interactively.
        """
        _valid_dets = False
        _valid_mon = False

        if dets is not None:
            if not isinstance(dets, Iterable):
                dets = [dets]
            number_of_options = self.detectors_plot_options.shape[0]
            if all(isinstance(i, int) for i in dets) and all(
                i < number_of_options for i in dets
            ):
                _valid_dets = True
            else:
                logger.warning(f"The detectors option {dets} is invalid.")

        if mon is not None:
            if isinstance(mon, int):
                _valid_mon = True
            else:
                logger.warning(f"The monitor option {mon} is invalid.")

        if not (_valid_dets and _valid_mon):
            print("Options:")
            print(self.detectors_plot_options)
            print("")

        if not _valid_dets:
            while True:
                dets = input("Enter the indexes of plotting channels: ") or None
                if dets is None:
                    print("A value must be entered.")
                    continue
                try:
                    dets = [int(i) for i in dets.split()]
                except ValueError:
                    print("Please enter the index numbers only.")
                    continue
                if not all(i in self.detectors_plot_options.index.values for i in dets):
                    print("The index values must be in the table.")
                    continue
                break

        self.select_plot_channels(dets)

        if not _valid_mon:
            _mon = self.detectors_plot_options[
                self.detectors_plot_options["channels"] == self.monitor
            ].index[0]
            while True:
                mon = input(f"Enter index number of monitor detector [{_mon}]: ") or _mon
                try:
                    mon = int(mon)
                except ValueError:
                    print("Please enter the index number only.")
                    continue
                if mon in dets:
                    print(
                        f"Monitor {mon} is invalid because it is already selected as a detector."
                    )
                    continue
                break

        self._mon = self.detectors_plot_options.loc[mon]["channels"]

        print()
        print(self)


counters = CountersClass()
