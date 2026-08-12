"""Scaler/counter device."""

import time

from ophyd import Component, EpicsSignal, Kind
from ophyd.scaler import ScalerCH
from ophyd.signal import Signal


class PresetMonitorSignal(Signal):
    """Signal that controls the selected monitor channel preset.

    Transparently converts between seconds (user-facing) and clock counts
    (EPICS) when the monitor is the time channel (chan01).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._readback = 0
        self._freq = getattr(self.parent, "freq", None)

    def get(self, **kwargs):
        self._readback = self.parent._monitor.preset.get()
        if "chan01" in self.parent._monitor.name:
            freq = 1e7 if not self._freq else self._freq.get()
            self._readback /= freq  # convert counts → seconds
        return self._readback

    def put(self, value, *, timestamp=None, force=False, metadata=None):
        if float(value) <= 0:
            raise ValueError("preset_value has to be > 0.")

        if "chan01" in self.parent._monitor.name:
            freq = 1e7 if not self._freq else self._freq.get()
            value_put = freq * value  # convert seconds → counts
        else:
            value_put = value

        old_value = self._readback
        self.parent._monitor.preset.put(value_put)
        self._readback = value

        if metadata is None:
            metadata = {}
        if timestamp is None:
            timestamp = metadata.get("timestamp", time.time())

        metadata = metadata.copy()
        metadata["timestamp"] = timestamp
        self._metadata.update(**metadata)

        md_for_callback = {
            key: metadata[key] for key in self._metadata_keys if key in metadata
        }
        if "timestamp" not in self._metadata_keys:
            md_for_callback["timestamp"] = timestamp

        self._run_subs(
            sub_type=self.SUB_VALUE,
            old_value=old_value,
            value=value,
            **md_for_callback,
        )


class LocalScalerCH(ScalerCH):
    """ScalerCH with monitor selection and channel plot/read helpers.

    Instantiate with prefix ``"6idb1:scaler1"``.

    ``default_settings()`` is called automatically by ``make_devices()`` and
    sets the monitor to the time channel, then selects all named channels for
    reading and plotting.
    """

    preset_time = None
    preset_monitor = Component(PresetMonitorSignal, kind=Kind.config)
    freq = Component(EpicsSignal, ".FREQ", kind=Kind.config)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._monitor = self.channels.chan01  # Time is the default monitor.

    @property
    def channels_name_map(self):
        """Return a dict mapping EPICS channel name → component name (e.g. 'chan02')."""
        name_map = {}
        for channel in self.channels.component_names:
            name = getattr(self.channels, channel).s.name
            if len(name) > 0:
                name_map[name] = channel
        return name_map

    def select_plot_channels(self, chan_names=None):
        """Set Kind.hinted on the given channel names, Kind.normal on all others.

        Iterates *all* channels (not just named ones) so that unnamed channels
        are explicitly demoted from their default Kind.hinted, preventing empty
        strings from appearing in hints["fields"].
        """
        self.match_names()
        name_map = self.channels_name_map

        if not chan_names:
            chan_names = name_map.keys()

        for channel_attr in self.channels.component_names:
            channel = getattr(self.channels, channel_attr)
            epics_name = channel.s.name  # empty string for unnamed channels
            if not epics_name:
                # Unnamed channel: omit completely to prevent empty-string
                # keys in data_keys, which fail event model schema validation.
                channel.s.kind = Kind.omitted
            elif epics_name in chan_names:
                channel.s.kind = Kind.hinted
            else:
                channel.s.kind = Kind.normal

    def select_read_channels(self, chan_names=None):
        """Select channels to read, always including chan01 (time).

        Parameters
        ----------
        chan_names : iterable of str or None
            EPICS channel names to include.  If *None*, all named channels are
            selected.
        """
        self.match_names()
        name_map = self.channels_name_map

        if chan_names is None:
            chan_names = name_map.keys()

        read_attrs = ["chan01"]  # always include time
        for ch in chan_names:
            try:
                read_attrs.append(name_map[ch])
            except KeyError:
                raise RuntimeError(
                    f"The channel {ch} is not configured on the scaler. "
                    f"The named channels are {tuple(name_map)}"
                )

        self.channels.kind = Kind.normal
        self.channels.read_attrs = list(read_attrs)
        self.channels.configuration_attrs = list(read_attrs)
        if len(self.hints["fields"]) == 0:
            self.select_plot_channels(chan_names)

    @property
    def monitor(self):
        return self._monitor.s.name

    @monitor.setter
    def monitor(self, value):
        """Select the monitor channel by EPICS name or component name.

        Parameters
        ----------
        value : str
            Either the EPICS channel label (e.g. ``'Ion Ch 1'``) or the
            component name (e.g. ``'chan01'``).
        """
        name_map = self.channels_name_map
        if value not in (set(name_map.keys()) | set(name_map.values())):
            raise ValueError(
                f"Monitor must be a channel name or component name. "
                f"Valid names: {list(name_map.keys())}, "
                f"valid components: {list(name_map.values())}."
            )

        if value in name_map.keys():
            value = name_map[value]

        channel = getattr(self.channels, value)
        if channel.kind == Kind.omitted:
            channel.kind = Kind.normal

        for channel_name in self.channels.component_names:
            chan = getattr(self.channels, channel_name)
            target = "Y" if chan == channel else "N"
            chan.gate.put(target, use_complete=True)

        self._monitor = channel

    @property
    def plot_options(self):
        """Return all EPICS-named scaler channel labels."""
        return list(self.channels_name_map.keys())

    @property
    def plot_signals(self):
        """Return a mapping of channel label → the signal carrying its counts.

        Companion to :attr:`plot_options`: the names are the same, but this
        resolves each one to the underlying signal so callers can inspect or
        change its ``kind``.  Used by the GUI's Detectors tab.
        """
        return {
            name: getattr(self.channels, component).s
            for name, component in self.channels_name_map.items()
        }

    def select_plot(self, channels):
        self.select_plot_channels(chan_names=channels)

    def default_settings(self):
        self.monitor = "chan01"
        self.select_read_channels()
        self.select_plot_channels()
