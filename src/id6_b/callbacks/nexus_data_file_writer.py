"""NeXus/HDF5 data file writer callback.

The ``nxwriter`` singleton is subscribed per-scan by the local scan plans
(via ``@subs_decorator``), not globally at startup.  This allows each scan
to configure its own file path and external detector links before writing.
"""

import logging
from datetime import datetime

import h5py
from apstools.callbacks import NXWriterAPS
from apsbits.utils.config_loaders import get_config
from numpy import array

logger = logging.getLogger(__name__)

iconfig = get_config()

LAYOUT_VERSION = "APS-6IDB-2024-10"
NEXUS_RELEASE = "v2022.07"


class MyNXWriter(NXWriterAPS):
    """Customized NeXus writer for 6-ID-B.

    Extends NXWriterAPS to support external file links (for area detectors)
    and to write relative timestamps alongside absolute epochs in each stream.

    Set ``nxwriter.file_name``, ``nxwriter.file_path``, and optionally
    ``nxwriter.external_files`` before each scan (done automatically by the
    local scan plans).
    """

    external_files = {}

    def write_root(self, filename):
        super().write_root(filename)
        self.root.attrs["NeXus_version"] = NEXUS_RELEASE
        self.root.attrs["layout_version"] = LAYOUT_VERSION

    def write_entry(self):
        """Called after the stop document is received."""
        nxentry = super().write_entry()
        ds = nxentry.create_dataset("layout_version", data=LAYOUT_VERSION)
        ds.attrs["target"] = ds.name
        nxentry["instrument/layout_version"] = ds

        for name, path in self.external_files.items():
            link_path = (
                "/stream" if name == "positioner_stream" else "/entry/instrument"
            )
            h5addr = f"/entry/externals/{name}"
            self.root[h5addr] = h5py.ExternalLink(str(path), link_path)

        self.external_files = {}

    def write_streams(self, parent):
        """Write all bluesky streams; external image data is linked, not embedded."""
        bluesky = self.create_NX_group(parent, "streams:NXnote")
        for stream_name, uids in self.streams.items():
            if len(uids) != 1:
                raise ValueError(
                    f"stream {stream_name!r} has {len(uids)} descriptors, expecting only 1"
                )
            group = self.create_NX_group(bluesky, stream_name + ":NXnote")
            uid0 = uids[0]
            group.attrs["uid"] = uid0
            acquisition = self.acquisitions[uid0]
            for k, v in acquisition["data"].items():
                d = v["data"]
                subgroup = self.create_NX_group(group, k + ":NXdata")

                if v["external"]:
                    pass  # linked via external_files dict
                else:
                    self.write_stream_internal(parent, d, subgroup, stream_name, k, v)

                t = array(v["time"])
                ds = subgroup.create_dataset("EPOCH", data=t)
                ds.attrs["units"] = "s"
                ds.attrs["long_name"] = "epoch time (s)"
                ds.attrs["target"] = ds.name

                if len(t) > 0:
                    t_start = t[0]
                    iso = datetime.fromtimestamp(t_start).isoformat()
                    ds = subgroup.create_dataset("time", data=t - t_start)
                    ds.attrs["units"] = "s"
                    ds.attrs["long_name"] = "time since first data (s)"
                    ds.attrs["target"] = ds.name
                    ds.attrs["start_time"] = t_start
                    ds.attrs["start_time_iso"] = iso

            # alias image datasets without the '_image' suffix
            for k in group:
                if k.endswith("_image") and k[:-6] not in group:
                    group[k[:-6]] = group[k]

        return bluesky


nxwriter = MyNXWriter()
nxwriter.file_extension = iconfig.get("NEXUS_DATA_FILES", {}).get("FILE_EXTENSION", "hdf")
nxwriter.warn_on_missing_content = iconfig.get("NEXUS_DATA_FILES", {}).get(
    "WARN_MISSING", False
)
