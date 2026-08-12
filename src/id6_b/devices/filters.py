"""
Filter (attenuator) bank.

The 6-ID-B filter IOC exposes a transmission setpoint/readback pair plus the
photon energy used to compute the required absorber combination.  The energy
may either track the beamline monochromator ("Mono") or be set locally
("Local"); ``energy_select`` chooses between them and the read-only ``energy``
attribute reports whichever one is active.
"""

from ophyd import Component
from ophyd import Device
from ophyd import EpicsSignal
from ophyd import EpicsSignalRO
from ophyd.signal import AttributeSignal


class FilterBank(Device):
    """Filter bank with transmission and energy control.

    Instantiate with prefix ``"6idb1:filter:"``.
    """

    transmission = Component(
        EpicsSignal,
        "Transmission",
        write_pv="TransmissionSetpoint",
        kind="hinted",
    )
    energy_beamline = Component(EpicsSignalRO, "EnergyBeamline", kind="config")
    energy_local = Component(
        EpicsSignal, "EnergyLocal", write_pv="EnergyLocal", kind="config"
    )
    energy_select = Component(EpicsSignal, "EnergySelect", kind="omitted", string=True)

    energy = Component(
        AttributeSignal, attr="_energy", write_access=False, kind="config"
    )

    def allin(self):
        """Insert all filters (transmission → 0)."""
        self.transmission.put(0)

    def allout(self):
        """Remove all filters (transmission → 1)."""
        self.transmission.put(1)

    @property
    def transmission_value(self):
        """Current transmission fraction."""
        return self.transmission.get()

    @transmission_value.setter
    def transmission_value(self, value):
        self.transmission.put(value)

    @property
    def _energy(self):
        """Return the active energy, following the ``energy_select`` choice."""
        if self.energy_select.get() in (0, "Mono"):
            return self.energy_beamline.get()
        elif self.energy_select.get() in (1, "Local"):
            return self.energy_local.get()

    @property
    def _energy_choose(self):
        """Return the energy source selection ("Mono" or "Local")."""
        return self.energy_select.get()

    @_energy_choose.setter
    def _energy_choose(self, value):
        self.energy_select.put(value)
