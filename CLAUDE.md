# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the **6-ID-B beamline** Bluesky data acquisition instrument package, built on the [BITS (Bluesky Instrument Template System)](https://BCDA-APS.github.io/BITS/) framework from APS (Advanced Photon Source). The main installable package is `polar-bits` (Python ≥3.11), and the instrument module is `id6_b`.

## Environment Setup

```bash
conda activate 6idb-bits
pip install -e .[all]     # install with dev + doc extras
```

## Common Commands

**Run tests:**
```bash
pytest                     # run all tests (stops at first failure)
pytest -v src/             # verbose
```

**Linting / formatting:**
```bash
pre-commit run --all-files   # run all pre-commit checks (ruff lint + format, YAML/TOML validation)
ruff check src/              # lint only
ruff format src/             # format only
```

**Start an interactive Bluesky session (IPython):**
```bash
ipython -i -c "from id6_b.startup import *"
```

**Verify installation with sim plans (inside IPython/notebook after startup):**
```python
from id6_b.plans.sim_plans import *
RE(sim_print_plan())
RE(sim_count_plan())
RE(sim_rel_scan_plan())
```

**Queue server management:**
```bash
./src/id6_b_qserver/qs_host.sh restart   # (re)start QS host in background screen session
./src/id6_b_qserver/qs_host.sh status
./src/id6_b_qserver/qs_host.sh console   # attach to running session
queue-monitor &                           # GUI client
```

## Architecture

### Startup flow (`src/id6_b/startup.py`)

1. Load `configs/iconfig.yml` and configure logging
2. Initialize Bluesky core: BEC/peaks → databroker catalog → RunEngine + supplemental data
3. Conditionally subscribe callbacks (NeXus writer, SPEC writer) based on `iconfig.yml` flags
4. `RE(make_devices(file="devices.yml"))` — creates all devices via Guarneri YAML
5. `RE(make_devices(file="devices_aps_only.yml"))` — only when on APS subnet
6. `setup_baseline_stream(sd, oregistry)` — adds devices labeled `"baseline"` to the supplemental data stream

### Device configuration (`src/id6_b/configs/devices.yml`)

Devices are declared in YAML using **Guarneri-style** definitions: the top-level key is the Python class (or creator function), and each list entry is a set of constructor kwargs. The `apsbits` framework calls `make_devices()` to instantiate them and register them in `oregistry`.

Key device groups currently active:
| YAML key | Device | Name |
|---|---|---|
| `id6_b.devices.monochromator.MonoDevice` | Kohzu DCM — energy pseudo-positioner | `mono` |
| `id6_b.devices.energy_device.EnergySignal` | Coordinated beamline energy | `energy` |
| `id6_b.devices.aps_undulator.PolarUndulatorPair` | Undulator pair (upstream + downstream) | `undulators` |
| `id6_b.devices.scaler.LocalScalerCH` | Scaler/counter | `scaler` |
| `id6_b.devices.aps_status.StatusAPS` | APS machine status (read-only) | `status_aps` |
| `apstools.devices.mb_creator` | CRL (10 lenses) + Mirror1 motor bundles | `crl`, `mirror1` |
| `hklpy2.creator` | E6C diffractometers (hkl, psi, q2 engines) | `psic_sim`, `psic`, `psic_psi`, `psic_q` |
| `apstools.devices.SimulatedApsPssShutterWithStatus` | Simulated shutter | `shutter` |

Several devices are **commented out** in `devices.yml` pending fixes or future work: `lakeshore340`, `lambda250k`.

### Custom device modules (`src/id6_b/devices/`)

- **`aps_undulator.py`** — `PolarUndulatorPair` wraps two `PolarUndulator` instances. Each `PolarUndulator` extends `STI_Undulator` with deadband checking (only moves if `|setpoint - readback| > energy_deadband`) and `TrackingSignal` support. `PolarUndulatorPositioner` is the custom `UndulatorPositioner` that implements this deadband logic in `set()`.
- **`aps_status.py`** — `StatusAPS` is a simple read-only `Device` with four `EpicsSignalRO` components: ring current, desired mode, operating mode, shutter permit.
- **`pv_positioner.py`** — `pvpositioner_factory()` dynamically creates `PVPositioner` subclasses from raw PV strings. Used in `devices.yml` for Mirror1 motors where the ophyd motor record pattern doesn't apply.
- **`monochromator.py`** — `MonoDevice` (PseudoPositioner, prefix `6ida1:`): pseudo axis `energy` (keV, 2.6–32) with real motors `th`→`m8`, `y2`→`m11`, plus additional `thf2`→`m13`, `chi2`→`m15`. Kohzu IOC crystal parameter records (`crystal_2d`, `y_offset`, `crystal_h/k/l/a`, `crystal_type`). `pzt_thf2` is commented out pending PV confirmation.
- **`energy_device.py`** — `EnergySignal` (ophyd `Signal`): coordinates beamline energy by moving `mono.energy` and any device in `oregistry` labeled `"track_energy"` whose `tracking` flag is enabled. Supports optional `energy_offset` per tracking device. Feedback hooks present but must be adapted to 6-ID-B's feedback system before enabling. `mono` must be created before `energy` in `devices.yml`.
- **`scaler.py`** — `LocalScalerCH` (prefix `6idb1:scaler1`): extends `ScalerCH` with `preset_monitor` (seconds ↔ clock-count conversion for the time channel), `freq` component, `monitor` setter (selects monitor and adjusts gates), and `select_read/plot_channels()`. `default_settings()` called by `make_devices()` on startup.
- **`lakeshore_controllers.py`** — `LS340Device` for Lakeshore 340 temperature controller (currently disabled in devices.yml).
- **`lambda_detector.py`** — `Lambda250kDetector` area detector with HDF5, ROI (1–4), and stats (1–5) plugins (currently disabled in devices.yml). Implements the `CountersClass` interface: `plot_options` returns `["Stats1"…"Stats5"]`; `select_plot(channels)` sets `Kind.hinted` on selected stats. Enable by uncommenting in `devices.yml` — it will then appear automatically in `counters()`. Call `configure_lambda(lambda250k)` after enabling to wire up ROI/stats ports and set default kinds.

### Key configuration files (`src/id6_b/configs/`)

- **`iconfig.yml`** — master instrument config: databroker catalog name (`6idb`), metadata defaults, SPEC/NeXus enable flags, BEC settings, DM_SETUP_FILE path
- **`devices.yml`** — active device definitions (Guarneri YAML)
- **`devices_aps_only.yml`** — APS machine parameters device, only loaded on APS subnet

### Utilities (`src/id6_b/utils/`)

- **`counters_class.py`** — `CountersClass` + singleton `counters`. Holds the detector list and monitor channel for scan plans. Looks up devices from `oregistry` lazily (safe to import before devices are created). `IDEAL_ORDER = ["scaler", "lambda250k"]` controls detector priority; add new detector names there as hardware is added. For a detector to appear in `counters()` it must implement `plot_options` (list of channel name strings) and `select_plot(channels)` (sets `Kind.hinted`). Usage:

```python
from id6_b.utils.counters_class import counters
counters()                           # interactive channel/monitor selection
counters.plotselect(dets=[1], mon=0) # non-interactive
RE(bp.count(counters.detectors))
```

### Plans (`src/id6_b/plans/`)

- **`sim_plans.py`** — simulation-only plans for testing (`sim_count_plan`, `sim_rel_scan_plan`, `sim_print_plan`)
- **`dm_plans.py`** — APS Data Management workflow integration (`dm_submit_workflow_job`, `dm_list_processing_jobs`)

### Callbacks (`src/id6_b/callbacks/`)

- **`spec_data_file_writer.py`** — SPEC-format data file output (enabled in `iconfig.yml`)
- **`nexus_data_file_writer.py`** — NeXus/HDF5 data file output (disabled by default)

## polar_example Reference Library

`src/polar_example/polar_common/` is the shared device library from the APS POLAR group (4-ID beamlines). It is **not installed as a package** — it lives in the repo as a reference/copy source. When the user asks to port a device to `id6_b`, copy the relevant file from `src/polar_example/polar_common/devices/` and adapt it.

**Already ported to `id6_b/devices/`:**
- `aps_status.py` — matches the polar_common version
- `aps_undulator.py` — adapted with 6-ID-B-specific deadband/tracking logic
- `monochromator.py` — adapted: prefix `6ida1:`, motors m8/m11/m13/m15, no labjack PZTs
- `energy_device.py` — copied as-is; feedback hooks present but not yet wired to a 6-ID-B feedback device
- `scaler.py` — adapted for single scaler; `counters_class.py` ported to `utils/` with `IDEAL_ORDER = ["scaler"]`

**Available in `polar_common/devices/` for future porting:**

| Module | What it provides |
|---|---|
| `phaseplates.py` | Phase retarder (`PRDevice`) with energy-dependent Bragg angle, PZT, AC/DC mode, crystal reflection selection |
| `shutters.py` | PSS shutter (`PolarShutter`) with auto-open on PSS state change |
| `filters_device.py` | 12-slot APS filter wheel with transmission/energy control |
| `jj_slits.py` | 4-blade slit system with center/size pseudo-positioners |
| `scaler_dual_ctr8.py` | Dual CTR8 scaler (16 channels, for dual-scaler setups) |
| `preamps.py` | SRS 570 pre-amplifier with auto-optimization plans |
| `srs810.py` | SRS 810 lock-in amplifier (`LockinDevice`) |
| `quadems.py` | QuadEM / TetrAMM electrometer with sum/diff/position |
| `hhl_mirror.py` | Toroidal mirror with pitch/roll/curvature benders and pseudo-motors |
| `lakeshore_gaussmeter.py` | Lakeshore 475 gaussmeter for magnetic field measurement |
| `aps_xbpm.py` | Ring XBPM with current monitors and position/angle |
| `polar_diffractometer.py` | Full 6-circle diffractometer variants (`SixCircleDiffractometer`, `CradleDiffractometer`, etc.) |
| `vortex_xspress3_me4.py` | Vortex Xspress3 4-channel fluorescence detector with ROI, SCA, HDF5 |
| `vortex_xspress3_me7.py` | Vortex Xspress3 7-channel variant |
| `ad_eiger1M.py` | Eiger 1M area detector with HDF5 and trigger support |
| `ad_vimba.py` | Vimba camera with stats, ROI, HDF5 |

`polar_common/` also contains:
- `plans/` — `local_scans.py`, `flyscan_demo.py`, `workflow_plan.py`, `center_maximum.py`, `local_preprocessors.py`
- `utils/` — `counters_class.py`, `suspenders.py`, `hkl_utils.py`, `attenuator_utils.py`, `pr_setup.py`, and others
- `callbacks/` — `dichro_stream.py`, additional SPEC/NeXus writers

## Code Style

- Line length: 88 characters (Ruff), 115 (Flake8/Black — legacy)
- Ruff enforces: `E`, `F`, `B`, `I`, `W`, `D100`–`D107` (docstrings for public APIs)
- Quote style: double quotes
- Imports: one per line (`force-single-line = true`)
- Pre-commit is the enforcer — always run before committing

## Adding New Devices

1. Create a new module in `src/id6_b/devices/` with the device class
2. Add a Guarneri entry in `src/id6_b/configs/devices.yml` (or `devices_aps_only.yml` for APS-only)
3. Assign appropriate labels: `"baseline"` to include in supplemental data, `"source"`, `"detectors"`, `"diffractometer"`, etc.
4. Devices in `oregistry` are accessible by name after startup (e.g., `oregistry["undulators"]`)
