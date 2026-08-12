"""X-Spectrum Lambda area detector suppor."""


import logging
import time as ttime
from os.path import join

from ophyd import ADComponent
from ophyd import Component
from ophyd import EpicsSignalRO
from ophyd import Kind
from ophyd import Staged
from ophyd.areadetector import CamBase
from ophyd.areadetector import DetectorBase
from ophyd.areadetector import EpicsSignalWithRBV
from ophyd.areadetector import TriggerBase
from ophyd.areadetector.filestore_mixins import FileStoreHDF5SingleIterativeWrite
from ophyd.areadetector.plugins import CodecPlugin_V34
from ophyd.areadetector.plugins import HDF5Plugin_V34
from ophyd.areadetector.plugins import ProcessPlugin_V34
from ophyd.areadetector.plugins import ROIPlugin_V34
from ophyd.areadetector.plugins import StatsPlugin_V34
from ophyd.areadetector.trigger_mixins import ADTriggerStatus

logger = logging.getLogger(__name__)

# TODO: Change these paths to S6 ones.
LAMBDA_FILES_ROOT = "/home/xspadmin/6idb"
BLUESKY_FILES_ROOT = "/home/beams/USER6IDB/data/lambda"
TEST_IMAGE_DIR = "%Y/%m/%d/"


class MySingleTrigger(TriggerBase):
    """
    This trigger mixin class takes one acquisition per trigger.
    Examples
    --------
    >>> class SimDetector(SingleTrigger):
    ...     pass
    >>> det = SimDetector('..pv..')
    # optionally, customize name of image
    >>> det = SimDetector('..pv..', image_name='fast_detector_image')
    """
    _status_type = ADTriggerStatus

    def __init__(self, *args, image_name=None, delay_time=0.1, **kwargs):
        super().__init__(*args, **kwargs)
        if image_name is None:
            image_name = '_'.join([self.name, 'image'])
        self._image_name = image_name
        self._monitor_status = self.cam.detector_state
        self._sleep_time = delay_time

    def stage(self):
        self._monitor_status.subscribe(self._acquire_changed)
        super().stage()

    def unstage(self):
        super().unstage()
        self._monitor_status.clear_sub(self._acquire_changed)

    def trigger(self):
        "Trigger one acquisition."
        if self._staged != Staged.yes:
            raise RuntimeError("This detector is not ready to trigger."
                               "Call the stage() method before triggering.")

        self._status = self._status_type(self)
        self._acquisition_signal.put(1, wait=False)
        self.dispatch(self._image_name, ttime.time())
        return self._status

    def _acquire_changed(self, value=None, old_value=None, **kwargs):
        "This is called when the 'acquire' signal changes."
        if self._status is None:
            return
        if (old_value != 0) and (value == 0):
            # Negative-going edge means an acquisition just finished.
            ttime.sleep(self._sleep_time)
            self._status.set_finished()
            self._status = None


class Lambda250kCam(CamBase):
    """
    support for X-Spectrum Lambda 750K detector
    https://x-spectrum.de/products/lambda-350k750k/
    """
    _html_docs = ['Lambda250kCam.html']

    serial_number = ADComponent(EpicsSignalRO, 'SerialNumber_RBV')
    firmware_version = ADComponent(EpicsSignalRO, 'FirmwareVersion_RBV')

    operating_mode = ADComponent(EpicsSignalWithRBV, 'OperatingMode')
    energy_threshold = ADComponent(EpicsSignalWithRBV, 'EnergyThreshold')
    dual_threshold = ADComponent(EpicsSignalWithRBV, 'DualThreshold')

    file_number_sync = None
    file_number_write = None
    pool_max_buffers = None


class MyHDF5Plugin(FileStoreHDF5SingleIterativeWrite, HDF5Plugin_V34):
    # This will use the default "AD_HDF5_SINGLE" from the
    # area-detector-handlers package.
    pass


class MyProcessPlugin(ProcessPlugin_V34):
    high_clip = None
    low_clip = None

    high_clip_threshold = Component(
        EpicsSignalWithRBV,
        "HighClipThresh",
        kind="config"
    )

    high_clip_value = Component(
        EpicsSignalWithRBV,
        "HighClipValue",
        kind="config"
    )

    low_clip_threshold = Component(
        EpicsSignalWithRBV,
        "LowClipThresh",
        kind="config"
    )
    
    high_clip_value = Component(
        EpicsSignalWithRBV,
        "LowClipValue",
        kind="config"
    )


class Lambda250kDetector(MySingleTrigger, DetectorBase):

    _default_configuration_attrs = (
        'roi1', 'roi2', 'roi3', 'roi4', 'codec'
    )
    _default_read_attrs = (
        'cam', 'hdf1', 'stats1', 'stats2', 'stats3', 'stats4', 'stats5'
    )

    cam = ADComponent(Lambda250kCam, 'cam1:', kind='normal')
    hdf1 = ADComponent(
        MyHDF5Plugin,
        "HDF1:",
        write_path_template=join(LAMBDA_FILES_ROOT, TEST_IMAGE_DIR),
        read_path_template=join(BLUESKY_FILES_ROOT, TEST_IMAGE_DIR),
        kind='normal'
    )
    roi1 = ADComponent(ROIPlugin_V34, 'ROI1:')
    roi2 = ADComponent(ROIPlugin_V34, 'ROI2:')
    roi3 = ADComponent(ROIPlugin_V34, 'ROI3:')
    roi4 = ADComponent(ROIPlugin_V34, 'ROI4:')

    stats1 = ADComponent(StatsPlugin_V34, 'Stats1:')
    stats2 = ADComponent(StatsPlugin_V34, 'Stats2:')
    stats3 = ADComponent(StatsPlugin_V34, 'Stats3:')
    stats4 = ADComponent(StatsPlugin_V34, 'Stats4:')
    stats5 = ADComponent(StatsPlugin_V34, 'Stats5:')

    codec = ADComponent(CodecPlugin_V34, 'Codec1:')
    proc = ADComponent(MyProcessPlugin, "Proc1:")

    xcenter = 256
    ycenter = 256

    @property
    def preset_monitor(self):
        return self.cam.acquire_time

    def default_kinds(self):

        # TODO: This is setting A LOT of stuff as "configuration_attrs", should
        # be revised at some point.

        # Some of the attributes return numpy arrays which Bluesky doesn't
        # accept: configuration_names, stream_hdr_appendix,
        # stream_img_appendix.
        _remove_from_config = (
            "file_number_sync",  # Removed from EPICS
            "file_number_write",  # Removed from EPICS
            "pool_max_buffers",  # Removed from EPICS
            # all below are numpy.ndarray
            "configuration_names",
            "stream_hdr_appendix",
            "stream_img_appendix",
            "dim0_sa",
            "dim1_sa",
            "dim2_sa",
            "nd_attributes_macros",
            "dimensions",
            'asyn_pipeline_config',
            'dim0_sa',
            'dim1_sa',
            'dim2_sa',
            'dimensions',
            'histogram',
            'ts_max_value',
            'ts_mean_value',
            'ts_min_value',
            'ts_net',
            'ts_sigma',
            'ts_sigma_xy',
            'ts_sigma_y',
            'ts_total',
            'ts_timestamp',
            'ts_centroid_total',
            'ts_eccentricity',
            'ts_orientation',
            'histogram_x',
        )

        self.cam.configuration_attrs += [
            item for item in Lambda250kCam.component_names if item not in
            _remove_from_config
        ]

        self.cam.read_attrs += ["num_images_counter"]

        for name in self.component_names:
            comp = getattr(self, name)
            if isinstance(
                comp, (ROIPlugin_V34, StatsPlugin_V34, ProcessPlugin_V34)
            ):
                comp.configuration_attrs += [
                    item for item in comp.component_names if item not in
                    _remove_from_config
                ]
            if isinstance(comp, StatsPlugin_V34):
                comp.total.kind = Kind.hinted
                comp.read_attrs += ["max_value", "min_value"]

    def default_settings(self):
        self.stage_sigs['cam.num_images'] = 1

    # Example of roi config.
    def plot_roi1(self):
        self.stats1.total.kind = "hinted"  # Is this signal correct?
        self.stats2.total.kind = "normal"
        self.stats3.total.kind = "normal"
        self.stats4.total.kind = "normal"
        self.stats5.total.kind = "normal"

    def plot_roi2(self):
        self.stats1.total.kind = "normal"  # Is this signal correct?
        self.stats2.total.kind = "hinted"
        self.stats3.total.kind = "normal"
        self.stats4.total.kind = "normal"
        self.stats5.total.kind = "normal"

    def plot_roi3(self):
        self.stats3.total.kind = "hinted"  # Is this signal correct?
        self.stats2.total.kind = "normal"
        self.stats1.total.kind = "normal"
        self.stats4.total.kind = "normal"
        self.stats5.total.kind = "normal"

    def plot_roi4(self):
        self.stats4.total.kind = "hinted"  # Is this signal correct?
        self.stats2.total.kind = "normal"
        self.stats3.total.kind = "normal"
        self.stats1.total.kind = "normal"
        self.stats5.total.kind = "normal"

    def plot_roi5(self):
        self.stats5.total.kind = "hinted"  # Is this signal correct?
        self.stats1.total.kind = "normal"
        self.stats2.total.kind = "normal"
        self.stats3.total.kind = "normal"
        self.stats4.total.kind = "normal"
        
    def plot_all(self):
        for i in range(1, 6):
            getattr(self, f"stats{i}").total.kind = "hinted"
            
    def plot_roi(self, rois = [1]):
        
        if not isinstance(rois, list):
            rois = list((rois,))
        
        for i in range(1, 6):
            getattr(self, f"stats{i}").total.kind = "normal"
        
        for i in rois:
            if not isinstance(i, int):
                print("ROI must be an integer!")
            else:
                if (i > 5) or (i < 1):
                    print(f"ROI #{i} does not exist!")
                else:
                    getattr(self, f"stats{i}").total.kind = "hinted"

    def image_roi1(self, xsize, ysize):
        xmin = int(self.xcenter - xsize/2)
        ymin = int(self.ycenter - ysize/2)
        self.roi1.set(dict(x=[xmin, xsize], y=[ymin, ysize]))

    def image_roi2(self, xsize, ysize):
        xmin = int(self.xcenter - xsize/2)
        ymin = int(self.ycenter - ysize/2)
        self.roi2.set(dict(x=[xmin, xsize], y=[ymin, ysize]))

    def image_roi3(self, xsize, ysize):
        xmin = int(self.xcenter - xsize/2)
        ymin = int(self.ycenter - ysize/2)
        self.roi3.set(dict(x=[xmin, xsize], y=[ymin, ysize]))

    def image_roi4(self, xsize, ysize):
        xmin = int(self.xcenter - xsize/2)
        ymin = int(self.ycenter - ysize/2)
        self.roi4.set(dict(x=[xmin, xsize], y=[ymin, ysize]))

    def image_cen(self, xc, yc):
        self.xcenter = xc
        self.ycenter = yc

    @property
    def plot_options(self):
        """Return stats channel names for use by CountersClass."""
        return [f"Stats{i}" for i in range(1, 6)]

    @property
    def plot_signals(self):
        """Return a mapping of stats channel name -> its ``total`` signal.

        Companion to :attr:`plot_options`: same names, but resolved to the
        underlying signal so callers can inspect or change its ``kind``.
        Used by the GUI's Detectors tab.
        """
        return {f"Stats{i}": getattr(self, f"stats{i}").total for i in range(1, 6)}

    def select_plot(self, channels):
        """Set Kind.hinted on the stats matching *channels*, Kind.normal on others.

        Called by CountersClass.select_plot_channels with the list of channel
        names chosen by the user from detectors_plot_options.

        Parameters
        ----------
        channels : list of str
            Subset of ``plot_options`` (e.g. ``["Stats1", "Stats3"]``).
        """
        for i in range(1, 6):
            stat = getattr(self, f"stats{i}")
            if f"Stats{i}" in channels:
                stat.total.kind = Kind.hinted
            else:
                stat.total.kind = Kind.normal

def configure_lambda(lambda250k):
    """Configure the Lambda 250k detector."""

    logger.info("-- configuring Lambda 250k detector --")
    lambda250k.wait_for_connection(timeout=10)

    logger.info("Setting up ROI and STATS defaults ...")
    for name in lambda250k.component_names:
        if "roi" in name:
            roi = getattr(lambda250k, name)
            roi.wait_for_connection(timeout=10)
            roi.nd_array_port.put("PROC1")
        if "stats" in name:
            stat = getattr(lambda250k, name)
            stat.wait_for_connection(timeout=10)
            if "stats5" in name:
                stat.nd_array_port.put("PROC1")
            else:
                stat.nd_array_port.put(f"ROI{stat.port_name.get()[-1]}")
    logger.info("Done!")

    logger.info("Setting up defaults kinds ...")
    lambda250k.default_kinds()
    logger.info("Done!")
    logger.info("Setting up default settings ...")
    lambda250k.default_settings()
    logger.info("Done!")
    logger.info("All done!")
