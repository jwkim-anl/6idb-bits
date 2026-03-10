"""Monochromator with energy controller."""

from numpy import arcsin, cos, pi, sin
from ophyd import Component, EpicsMotor, EpicsSignal, PseudoPositioner, PseudoSingle
from ophyd.pseudopos import pseudo_position_argument, real_position_argument
from scipy.constants import Planck, speed_of_light


class MonoDevice(PseudoPositioner):
    """6-ID-B Kohzu double-crystal monochromator.

    Instantiate with prefix ``"6ida1:"``.
    """

    # Pseudo axis
    energy = Component(PseudoSingle, limits=(2.6, 32))  # keV

    # Real motors used in forward/inverse calculations
    th = Component(EpicsMotor, "m8", labels=("motor",))
    y2 = Component(EpicsMotor, "m11", labels=("motor",))

    _real = ["th", "y2"]

    # Additional crystal motors
    thf2 = Component(EpicsMotor, "m13", labels=("motor",))
    chi2 = Component(EpicsMotor, "m15", labels=("motor",))

    # PZT thf2 fine-tuner readback — uncomment and set full PV when known:
    # pzt_thf2 = FormattedComponent(EpicsSignalRO, "6ida1:???")

    # Crystal parameters (Kohzu IOC records, relative to prefix)
    y_offset = Component(EpicsSignal, "Kohzu_yOffsetAO.VAL", kind="config")
    crystal_h = Component(EpicsSignal, "BraggHAO.VAL", kind="config")
    crystal_k = Component(EpicsSignal, "BraggKAO.VAL", kind="config")
    crystal_l = Component(EpicsSignal, "BraggLAO.VAL", kind="config")
    crystal_a = Component(EpicsSignal, "BraggAAO.VAL", kind="config")
    crystal_2d = Component(EpicsSignal, "Bragg2dSpacingAO", kind="config")
    crystal_type = Component(EpicsSignal, "BraggTypeMO", string=True, kind="config")

    def convert_energy_to_theta(self, energy):
        """Convert energy (keV) to Bragg angle (degrees)."""
        lamb = speed_of_light * Planck * 6.241509e15 * 1e10 / energy
        return arcsin(lamb / self.crystal_2d.get()) * 180.0 / pi

    def convert_energy_to_y(self, energy):
        """Convert energy (keV) to y2 position."""
        theta = self.convert_energy_to_theta(energy)
        return self.y_offset.get() / (2 * cos(theta * pi / 180))

    def convert_theta_to_energy(self, theta):
        """Convert Bragg angle (degrees) to energy (keV)."""
        lamb = self.crystal_2d.get() * sin(theta * pi / 180)
        return speed_of_light * Planck * 6.241509e15 * 1e10 / lamb

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        """Pseudo -> real: energy to (th, y2)."""
        return self.RealPosition(
            th=self.convert_energy_to_theta(pseudo_pos.energy),
            y2=self.convert_energy_to_y(pseudo_pos.energy),
        )

    @real_position_argument
    def inverse(self, real_pos):
        """Real -> pseudo: th to energy (y2 does not affect energy)."""
        return self.PseudoPosition(energy=self.convert_theta_to_energy(real_pos.th))

    def set_energy(self, energy):
        """Set the current theta position to correspond to the given energy (keV)."""
        theta = self.convert_energy_to_theta(energy)
        self.th.set_current_position(theta)
