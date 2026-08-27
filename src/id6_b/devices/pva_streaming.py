"""PVA streaming data cache control.

Three EPICS PVs drive the PVA streaming writer:

===========================  =================================================
``6idb1:ScanOn:Value``       1 while the cache should collect, 0 when not.
``6idb1:FileName:Value``     Name of the file the cache is written to.
``6idb1:FilePath:Value``     Directory the cache is written into.
===========================  =================================================

They share the ``6idb1:`` prefix, so they are one small ``Device`` rather than
three loose signals -- that is what lets ``devices.yml`` create them and
``oregistry`` find them by name.

Everything is ``kind="omitted"``.  The device is never in a scan's detector or
extras list, so nothing here should reach an event descriptor; the file name
and directory that were used go into the **run start metadata** instead (see
:func:`id6_b.plans.pva_streaming.pva_metadata`), which is where they are useful
when the data is looked up again later.

**The two path PVs are 40-byte Channel Access strings.**  ``cainfo`` reports
them as ``DBF_STRING`` with an element count of 1, and neither ``.RTYP`` nor
pvAccess answers for them, so they are served by something that is not an IOC
record and there is no long-string field to fall back on.  A ``DBF_STRING``
holds 39 characters plus the terminator and the server truncates the rest
silently, which is why :mod:`id6_b.plans.pva_streaming` abbreviates the home
directory to ``~`` and warns when a value is still too long.
"""

from ophyd import Component
from ophyd import Device
from ophyd import EpicsSignal


class PvaStreamControl(Device):
    """Cache control for the PVA streaming writer.

    Instantiate with prefix ``"6idb1:"``.
    """

    scan_on = Component(EpicsSignal, "ScanOn:Value", kind="omitted")
    file_name = Component(EpicsSignal, "FileName:Value", string=True, kind="omitted")
    file_path = Component(EpicsSignal, "FilePath:Value", string=True, kind="omitted")

    def start_caching(self):
        """Set the cache flag (``ScanOn`` -> 1)."""
        self.scan_on.put(1)

    def stop_caching(self):
        """Clear the cache flag (``ScanOn`` -> 0)."""
        self.scan_on.put(0)
