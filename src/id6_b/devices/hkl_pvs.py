"""The Channel Access PVs that describe the HKL conversion.

The beamline's area-detector analysis converts pixels to reciprocal space from
a set of PVs under ``6idb1:`` -- the orientation matrix, the detector's centre
channel and distance, and the sign convention of every diffractometer axis.
Nothing in the Bluesky session used to write them, so they could disagree with
the session without anything saying so.  This device is the write side;
:mod:`id6_b.plans.hkl_pv_sync` is what keeps them matching.

Every component is ``kind="omitted"`` and the device is not labelled
``baseline``, for the reason
:class:`~id6_b.devices.pva_streaming.PvaStreamControl` is not: these values
describe a run rather than being measured by it, and they must never reach an
event descriptor.  The two waveforms could not go in one anyway -- Bluesky does
not take arrays in a scalar data key, which is why
``Lambda250kDetector.default_kinds`` strips its ndarray attributes.

Two direction conventions are declared here.  ``hklpy2`` is the one the session
solves in today and is the default; ``spec`` is the one the future
``ad_hoc_diffractometer`` geometry will use, and nothing selects it yet.  A
convention covers eleven PVs: the eight axis sign strings and the three
components of the primary beam direction.
"""

__all__ = [
    "HklConversionPVs",
    "BEAM_DIRECTIONS",
    "BEAM_KEYS",
    "CONVENTIONS",
    "DIRECTION_KEYS",
    "HKLPY2_BEAM_DIRECTION",
    "HKLPY2_DIRECTIONS",
    "SPEC_BEAM_DIRECTION",
    "SPEC_DIRECTIONS",
]

from logging import getLogger

from ophyd import Component
from ophyd import Device
from ophyd import EpicsSignal

logger = getLogger(__name__)

#: The eight axes whose direction convention is published, in report order.
DIRECTION_KEYS = ("mu", "eta", "chi", "phi", "nu", "delta", "pixel1", "pixel2")

#: Direction convention of the hklpy2 geometry the session solves in.
HKLPY2_DIRECTIONS = {
    "mu": "z+",
    "eta": "y-",
    "chi": "x+",
    "phi": "y-",
    "nu": "z+",
    "delta": "y-",
    "pixel1": "y-",
    "pixel2": "z-",
}

#: Direction convention of the spec (``ad_hoc_diffractometer``) geometry.
SPEC_DIRECTIONS = {
    "mu": "x+",
    "eta": "z-",
    "chi": "y+",
    "phi": "z-",
    "nu": "x+",
    "delta": "z-",
    "pixel1": "z-",
    "pixel2": "x-",
}

#: Selectable conventions, by name.
CONVENTIONS = {"hklpy2": HKLPY2_DIRECTIONS, "spec": SPEC_DIRECTIONS}

#: The three components of the primary beam direction, in report order.
BEAM_KEYS = ("beam1", "beam2", "beam3")

#: Primary beam direction of each convention, as (axis1, axis2, axis3).
#: A separate mapping from CONVENTIONS because this half is numeric and the
#: other is strings, but it is keyed the same way and belongs to the same
#: choice -- a convention added to one must be added to the other, and
#: :meth:`HklConversionPVs.set_directions` writes both halves together so a
#: caller cannot pick up one without the other.
HKLPY2_BEAM_DIRECTION = (1.0, 0.0, 0.0)
SPEC_BEAM_DIRECTION = (0.0, 1.0, 0.0)
BEAM_DIRECTIONS = {
    "hklpy2": HKLPY2_BEAM_DIRECTION,
    "spec": SPEC_BEAM_DIRECTION,
}

# Beam key -> component name on HklConversionPVs.
_BEAM_ATTRS = {
    "beam1": "beam_direction1",
    "beam2": "beam_direction2",
    "beam3": "beam_direction3",
}

# Direction key -> component name on HklConversionPVs.
_DIRECTION_ATTRS = {
    "mu": "mu_direction",
    "eta": "eta_direction",
    "chi": "chi_direction",
    "phi": "phi_direction",
    "nu": "nu_direction",
    "delta": "delta_direction",
    "pixel1": "pixel_direction1",
    "pixel2": "pixel_direction2",
}


class HklConversionPVs(Device):
    """The orientation, detector geometry and axis-direction PVs."""

    # 9 elements, row-major, matching hklpy2's Matrix3x3.
    ub_matrix = Component(EpicsSignal, "spec:UB_matrix:Value", kind="omitted")

    # 2 elements: the detector's centre channel, (x, y) in pixels.
    center_pixel = Component(
        EpicsSignal, "DetectorSetup:CenterChannelPixel", kind="omitted"
    )

    # Sample-to-detector distance.  Nothing in the session sources this, so
    # nothing syncs it; it is here to be read and set.
    distance = Component(EpicsSignal, "DetectorSetup:Distance", kind="omitted")

    pixel_direction1 = Component(
        EpicsSignal, "DetectorSetup:PixelDirection1", string=True, kind="omitted"
    )
    pixel_direction2 = Component(
        EpicsSignal, "DetectorSetup:PixelDirection2", string=True, kind="omitted"
    )

    mu_direction = Component(
        EpicsSignal, "Mu:DirectionAxis", string=True, kind="omitted"
    )
    eta_direction = Component(
        EpicsSignal, "Eta:DirectionAxis", string=True, kind="omitted"
    )
    chi_direction = Component(
        EpicsSignal, "Chi:DirectionAxis", string=True, kind="omitted"
    )
    phi_direction = Component(
        EpicsSignal, "Phi:DirectionAxis", string=True, kind="omitted"
    )
    nu_direction = Component(
        EpicsSignal, "Nu:DirectionAxis", string=True, kind="omitted"
    )
    delta_direction = Component(
        EpicsSignal, "Delta:DirectionAxis", string=True, kind="omitted"
    )

    # The primary beam direction, as three scalars rather than one waveform --
    # that is how the IOC serves it (three DBF_DOUBLE records of one element).
    beam_direction1 = Component(
        EpicsSignal, "PrimaryBeamDirection:AxisNumber1", kind="omitted"
    )
    beam_direction2 = Component(
        EpicsSignal, "PrimaryBeamDirection:AxisNumber2", kind="omitted"
    )
    beam_direction3 = Component(
        EpicsSignal, "PrimaryBeamDirection:AxisNumber3", kind="omitted"
    )

    def direction_signal(self, key):
        """The signal publishing direction *key* (one of DIRECTION_KEYS)."""
        try:
            attr = _DIRECTION_ATTRS[key]
        except KeyError:
            raise ValueError(
                f"Unknown direction '{key}'; expected one of "
                f"{', '.join(DIRECTION_KEYS)}."
            ) from None
        return getattr(self, attr)

    def directions(self):
        """The eight direction PVs as they currently read."""
        return {key: self.direction_signal(key).get() for key in DIRECTION_KEYS}

    def beam_signal(self, key):
        """The signal publishing beam component *key* (one of BEAM_KEYS)."""
        try:
            attr = _BEAM_ATTRS[key]
        except KeyError:
            raise ValueError(
                f"Unknown beam component '{key}'; expected one of "
                f"{', '.join(BEAM_KEYS)}."
            ) from None
        return getattr(self, attr)

    def beam_direction(self):
        """The primary beam direction as it currently reads, ``[a1, a2, a3]``."""
        return [self.beam_signal(key).get() for key in BEAM_KEYS]

    def set_directions(self, convention="hklpy2"):
        """Write a whole convention -- both halves -- returning what it changed.

        That is the eight axis sign strings **and** the three primary beam
        direction components.  They are written together on purpose: the two
        halves describe one geometry, and a caller who got one without the
        other would be publishing a convention that exists nowhere.

        Only the PVs that disagree are written, so a repeated call is silent
        and costs nothing.  The return value maps each changed key to
        ``(old, new)``.
        """
        try:
            wanted = CONVENTIONS[convention]
            beam = BEAM_DIRECTIONS[convention]
        except KeyError:
            raise ValueError(
                f"Unknown convention '{convention}'; expected one of "
                f"{', '.join(sorted(set(CONVENTIONS) & set(BEAM_DIRECTIONS)))}."
            ) from None

        changed = {}
        for key in DIRECTION_KEYS:
            signal = self.direction_signal(key)
            old = signal.get()
            new = wanted[key]
            if old != new:
                signal.put(new)
                changed[key] = (old, new)
        for key, new in zip(BEAM_KEYS, beam, strict=True):
            signal = self.beam_signal(key)
            old = signal.get()
            # These are 0 and 1 exactly, so an equality test is honest here;
            # the sync layer still compares with a tolerance, since it has to
            # cope with a disconnected PV handing back None.
            if old is None or float(old) != float(new):
                signal.put(new)
                changed[key] = (old, new)
        if changed:
            logger.info(
                "Directions set to the '%s' convention: %s",
                convention,
                ", ".join(f"{k} {o}->{n}" for k, (o, n) in changed.items()),
            )
        return changed
