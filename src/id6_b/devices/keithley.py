"""
Keithley 2400 source meter.

Split into a source sub-device (``inp``, the programmed output) and a measure
sub-device (``meas``, the sensed values).  ``source_function`` selects whether
the instrument sources voltage or current; ``meas.sense_function`` selects what
is measured.
"""

from ophyd import Component
from ophyd import Device
from ophyd import EpicsSignal
from ophyd import EpicsSignalRO


class Keithley2400Source(Device):
    """Programmed (source) side of the Keithley 2400."""

    voltage = Component(EpicsSignal, "setVoltRdAI", write_pv="setVoltAO")
    voltage_range = Component(EpicsSignal, "vRangeMO")

    current = Component(EpicsSignal, "setCurrRdAI", write_pv="setCurrAO")
    current_range = Component(EpicsSignal, "iRangeMO")


class Keithley2400Measure(Device):
    """Measured (sense) side of the Keithley 2400."""

    voltage = Component(EpicsSignalRO, "measVoltAI")
    current = Component(EpicsSignalRO, "measCurrAI")
    sense_function = Component(
        EpicsSignal, "senseFunctionMO", string=True, kind="config"
    )


class Keithley2400(Device):
    """Keithley 2400 source meter.

    Instantiate with prefix ``"6idb1:K24K:"``.
    """

    inp = Component(Keithley2400Source, "")
    meas = Component(Keithley2400Measure, "")
    source_function = Component(
        EpicsSignal, "sourceFunctionMO", string=True, kind="config"
    )
