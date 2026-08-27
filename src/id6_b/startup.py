"""
Start Bluesky Data Acquisition sessions of all kinds.

Includes:

* Python script
* IPython console
* Jupyter notebook
* Bluesky queueserver
"""

# Standard Library Imports
import logging
from pathlib import Path

import gi  # noqa
import hklpy2
from apsbits.core.best_effort_init import init_bec_peaks
from apsbits.core.catalog_init import init_catalog
from apsbits.core.instrument_init import make_devices
from apsbits.core.instrument_init import oregistry

# Core Functions
from apsbits.core.run_engine_init import init_RE

# Utility functions
from apsbits.utils.aps_functions import aps_dm_setup
from apsbits.utils.aps_functions import host_on_aps_subnet
from apsbits.utils.baseline_setup import setup_baseline_stream

# Configuration functions
from apsbits.utils.config_loaders import load_config
from apsbits.utils.helper_functions import register_bluesky_magics
from apsbits.utils.helper_functions import running_in_queueserver
from apsbits.utils.logging_setup import configure_logging
from hklpy2.backends.hkl_soleil import libhkl

from id6_b.plans.sim_plans import sim_count_plan  # noqa
from id6_b.plans.sim_plans import sim_print_plan  # noqa
from id6_b.plans.sim_plans import sim_rel_scan_plan  # noqa

# Configuration block
# Get the path to the instrument package
# Load configuration to be used by the instrument.
instrument_path = Path(__file__).parent
iconfig_path = instrument_path / "configs" / "iconfig.yml"
iconfig = load_config(iconfig_path)

# Additional logging configuration
# only needed if using different logging setup
# from the one in the apsbits package
extra_logging_configs_path = instrument_path / "configs" / "extra_logging.yml"
configure_logging(extra_logging_configs_path=extra_logging_configs_path)


logger = logging.getLogger(__name__)
logger.info("Starting Instrument with iconfig: %s", iconfig_path)

# Discard oregistry items loaded above.
oregistry.clear()

# Configure the session with callbacks, devices, and plans.
aps_dm_setup(iconfig.get("DM_SETUP_FILE"))

# Command-line tools, such as %wa, %ct, ...
register_bluesky_magics()

# Bluesky initialization block
# Instrument = ...
# oregistry = ...
# oregistry.clear()
bec, peaks = init_bec_peaks(iconfig)
cat = init_catalog(iconfig)
RE, sd = init_RE(iconfig, bec_instance=bec, cat_instance=cat)
RE.md["versions"]["hklpy2"] = hklpy2.__version__
RE.md["versions"]["hkl_soleil"] = libhkl.VERSION

# Populate run_engine module so plans can access RE without circular imports.
import id6_b.utils.run_engine as _re_module  # noqa: E402

_re_module.RE = RE
_re_module.bec = bec
_re_module.peaks = peaks
_re_module.cat = cat

# NeXus writer — imported for use by local_scans (subscribed per-scan, not globally).
from .callbacks.nexus_data_file_writer import nxwriter  # noqa: F401, E402

# Optional SPEC callback block
# delete this block if not using SPEC
if iconfig.get("SPEC_DATA_FILES", {}).get("ENABLE", False):
    from .callbacks.spec_data_file_writer import init_specwriter_with_RE
    from .callbacks.spec_data_file_writer import newSpecFile  # noqa: F401
    from .callbacks.spec_data_file_writer import spec_comment  # noqa: F401
    from .callbacks.spec_data_file_writer import specwriter  # noqa: F401

    init_specwriter_with_RE(RE)

# These imports must come after the above setup.
# Queue server block
if running_in_queueserver():
    ### To make all the standard plans available in QS, import by '*', otherwise import
    ### plan by plan.
    from apstools.plans import lineup2  # noqa: F401
    from bluesky.plans import *  # noqa: F403
else:
    # Import bluesky plans and stubs with prefixes set by common conventions.
    # The apstools plans and utils are imported by '*'.
    from apstools.plans import *  # noqa: F403
    from apstools.utils import *  # noqa: F403
    from bluesky import plan_stubs as bps  # noqa: F401
    from bluesky import plans as bp  # noqa: F401


# Experiment specific logic, device and plan loading
RE(make_devices(clear=False, file="devices.yml"))  # Create the devices.

if host_on_aps_subnet():
    RE(make_devices(clear=False, file="devices_aps_only.yml"))

# Apply each device's own default configuration.  apsbits' make_devices() does
# NOT do this, and skipping it leaves LocalScalerCH with its unnamed channels
# at the default Kind.hinted|normal -- their empty EPICS names then reach the
# descriptor and every scan that reads the scaler dies with
# "ValidationError: '' does not match any of the regexes".
# Runs before setup_baseline_stream() so baseline devices are already
# configured when they are first read.
for _device in oregistry.all_devices:
    _apply_defaults = getattr(_device, "default_settings", None)
    if callable(_apply_defaults):
        try:
            _apply_defaults()
        except Exception:  # noqa: BLE001 - one bad device must not stop startup
            logger.exception("default_settings() failed for '%s'.", _device.name)
        else:
            logger.debug("Applied default_settings() for '%s'.", _device.name)

# Setup baseline stream with connect=False is default
# Devices with the label 'baseline' will be added to the baseline stream.
setup_baseline_stream(sd, oregistry, connect=False)

from id6_b.utils.counters_class import counters  # noqa: F401, E402
from id6_b.plans.local_scans import (  # noqa: F401, E402
    abs_set,
    ascan,
    count,
    grid_scan,
    lup,
    mv,
    mvr,
    rel_grid_scan,
)
from id6_b.plans.center_maximum import (  # noqa: F401, E402
    cen,
    cen2,
    com,
    maxi,
    maxi2,
    mini,
    mini2,
)
from id6_b.utils.experiment_utils import (  # noqa: F401, E402
    experiment,
    experiment_change_sample,
    experiment_setup,
)
from id6_b.plans.auto_attenuation import (  # noqa: F401, E402
    attenuation_setup,
    auto_atten,
)
