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

**Start the Qt session window (tabs + embedded IPython console):**
```bash
id6b-gui                 # after `pip install -e .`
python -m id6_b.gui.app  # equivalent, no reinstall needed
```
Runs the same `from id6_b.startup import *` bootstrap in a Jupyter kernel, so
the IPython workflow above is unaffected. See "GUI" under Architecture.

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
3. Populate `id6_b.utils.run_engine` module with the live `RE` and `bec` references
4. Conditionally subscribe callbacks (NeXus writer, SPEC writer) based on `iconfig.yml` flags
5. `RE(make_devices(file="devices.yml"))` — creates all devices via Guarneri YAML
6. `RE(make_devices(file="devices_aps_only.yml"))` — only when on APS subnet
7. Call `default_settings()` on every `oregistry` device that defines one — `make_devices()` does **not** do this. Required for `LocalScalerCH`: without it the unnamed scaler channels keep `Kind.hinted|normal` and their empty EPICS names break the event descriptor. Failures are logged per-device rather than aborting startup. Runs before the baseline stream is built so baseline devices are configured before their first read.
8. `setup_baseline_stream(sd, oregistry)` — adds devices labeled `"baseline"` to the supplemental data stream
9. Import `counters` singleton, `local_scans` plans and the `center_maximum` plans (`cen`, `com`, `maxi`, `mini` and the `*2` aliases) into the session namespace

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
| `apstools.devices.mb_creator` | 4-blade slits; `sl1`–`sl3` add IOC-computed center/size axes, `sl4` drives center/gap directly | `sl1`, `sl2`, `sl3`, `sl4` |
| `apstools.devices.mb_creator` | Optics table, cryostat carrier, diffractometer table, polarization analyzer | `opty2`, `cryo`, `diff`, `analy` |
| `id6_b.devices.filters.FilterBank` | Filter/attenuator bank (transmission + energy source) | `filters` |
| `id6_b.devices.keithley.Keithley2400` | Keithley 2400 source meter | `keithley2400` |
| `hklpy2.creator` | E6C diffractometers (hkl, psi, q2 engines); real diffractometers use `EpicsMonochromatorRO` beam | `psic_sim`, `psic`, `psic_psi`, `psic_q` |
| `id6_b.devices.lambda_detector.Lambda250kDetector` | Lambda 250K area detector | `lambda250k` |
| `apstools.devices.SimulatedApsPssShutterWithStatus` | Simulated shutter | `shutter` |

Several devices are **commented out** in `devices.yml` pending fixes or future work: `lakeshore340`; `scaler2` (live, but its IOC `.NM*` channel names duplicate `scaler1`'s, so both would emit the same data keys); and `mirr`, `rp100`, `pilatus100k`, `vortex` (ported from `bluesky/instrument/devices/` but their IOCs did not respond to `caget`).

The `sl1`–`sl3` entries show the per-axis `class:` hook of `mb_creator`: the four blade motors are plain suffixes, while the IOC transform-record center/size axes use `apstools.devices.PVPositionerSoftDoneWithStop` with `readback_pv`/`setpoint_pv`/`tolerance`. The axis `prefix` is a *suffix* onto the bundle prefix, so `"6idb1:"` + `"Slit_1"` + `"Vt2.D"` → `6idb1:Slit_1Vt2.D`. YAML anchors (`&slit1_vcen` / `<<:`) keep the four pseudo-axes from repeating.

### Custom device modules (`src/id6_b/devices/`)

- **`aps_undulator.py`** — `PolarUndulatorPair` wraps two `PolarUndulator` instances. Each `PolarUndulator` extends `STI_Undulator` with deadband checking (only moves if `|setpoint - readback| > energy_deadband`) and `TrackingSignal` support. `PolarUndulatorPositioner` is the custom `UndulatorPositioner` that implements this deadband logic in `set()`.
- **`aps_status.py`** — `StatusAPS` is a simple read-only `Device` with four `EpicsSignalRO` components: ring current, desired mode, operating mode, shutter permit.
- **`pv_positioner.py`** — `pvpositioner_factory()` dynamically creates `PVPositioner` subclasses from raw PV strings. Used in `devices.yml` for Mirror1 motors where the ophyd motor record pattern doesn't apply.
- **`monochromator.py`** — `MonoDevice` (PseudoPositioner, prefix `6ida1:`): pseudo axis `energy` (keV, 2.6–32) with real motors `th`→`m8`, `y2`→`m11`, plus additional `thf2`→`m13`, `chi2`→`m15`. Kohzu IOC crystal parameter records (`crystal_2d`, `y_offset`, `crystal_h/k/l/a`, `crystal_type`). `pzt_thf2` is commented out pending PV confirmation. The Kohzu IOC also provides computed readback PVs `6ida1:BraggERdbkAO` (energy, keV) and `6ida1:BraggLambdaRdbkAO` (wavelength, Å) used by the hklpy2 diffractometers.
- **`energy_device.py`** — `EnergySignal` (ophyd `Signal`): coordinates beamline energy by moving `mono.energy` and any device in `oregistry` labeled `"track_energy"` whose `tracking` flag is enabled. Supports optional `energy_offset` per tracking device. Feedback hooks present but must be adapted to 6-ID-B's feedback system before enabling. `mono` must be created before `energy` in `devices.yml`.
- **`scaler.py`** — `LocalScalerCH` (prefix `6idb1:scaler1`): extends `ScalerCH` with `preset_monitor` (seconds ↔ clock-count conversion for the time channel), `freq` component, `monitor` setter (selects monitor and adjusts gates), and `select_read/plot_channels()`. Also `plot_signals` (channel label → signal), the companion to `plot_options` used by the GUI's Detectors tab to read and set each channel's `Kind`. `default_settings()` is called by the `default_settings` loop in `startup.py` (step 7) — **not** by `make_devices()`, which has no such hook. Without it `scaler.channels.chan32` keeps its default `Kind.hinted|normal` and its empty EPICS name reaches the descriptor, raising `ValidationError: '' does not match any of the regexes` on any scan that reads the scaler. `select_plot_channels()` iterates all channels: unnamed ones get `Kind.omitted` (prevents empty-string keys in `data_keys`), named non-selected get `Kind.normal`, selected get `Kind.hinted`.
- **`filters.py`** — `FilterBank` (prefix `6idb1:filter:`): `transmission` (readback + `TransmissionSetpoint` write PV), `allin()`/`allout()` helpers, and a read-only `energy` `AttributeSignal` that reports `energy_beamline` or `energy_local` according to `energy_select` ("Mono"/"Local"). Ported from `bluesky/instrument/devices/filter.py`; renamed from `filter` so it no longer shadows the Python builtin.
- **`keithley.py`** — `Keithley2400` (prefix `6idb1:K24K:`): `inp` (programmed voltage/current + ranges) and `meas` (sensed voltage/current, `sense_function`) sub-devices, plus `source_function`. Not labeled `"baseline"` — `meas.voltage` reads the EPICS UDF sentinel `9.91e37` whenever the sense function is not voltage. Ported from `bluesky/instrument/devices/keith2400.py`.
- **`lakeshore_controllers.py`** — `LS340Device` for Lakeshore 340 temperature controller (currently disabled in devices.yml).
- **`lambda_detector.py`** — `Lambda250kDetector` area detector with HDF5, ROI (1–4), and stats (1–5) plugins. **Enabled** in `devices.yml` with labels `["detector", "detectors"]` (no `"baseline"` — area detectors require `stage()` before reading and must not be in the baseline stream). Implements the `CountersClass` interface: `plot_options` returns `["Stats1"…"Stats5"]`; `select_plot(channels)` sets `Kind.hinted` on selected stats; `plot_signals` maps those names to the `statsN.total` signals for the GUI's Detectors tab. Call `configure_lambda(lambda250k)` after enabling to wire up ROI/stats ports and set default kinds. Has a `setup_images()` method used by `local_scans` to configure per-scan HDF5 file paths; a `save_image_flag` attribute controls whether images are saved.

### Key configuration files (`src/id6_b/configs/`)

- **`iconfig.yml`** — master instrument config: databroker catalog name (`6idb`), metadata defaults, SPEC/NeXus enable flags, BEC settings, DM_SETUP_FILE path. Contains `AREA_DETECTOR: HDF5_FILE_TEMPLATE: "%s/%s_%05d"` used by `local_scans` to build per-scan output file paths.
- **`devices.yml`** — active device definitions (Guarneri YAML). The `hklpy2.creator` entries for real diffractometers (`psic`, `psic_psi`, `psic_q`) use a `beam_kwargs` key to configure `EpicsMonochromatorRO` as the beam source — this is the native hklpy2 way to link the HKL solver wavelength to EPICS monochromator PVs:
  ```yaml
  beam_kwargs:
    class: hklpy2.incident.EpicsMonochromatorRO
    prefix: "6ida1:"
    pv_energy: "BraggERdbkAO"       # → 6ida1:BraggERdbkAO  (keV readback)
    pv_wavelength: "BraggLambdaRdbkAO"  # → 6ida1:BraggLambdaRdbkAO  (Å readback)
  ```
  The simulated diffractometer (`psic_sim`) has no `beam_kwargs` and uses the default `WavelengthXray` soft signal.
- **`devices_aps_only.yml`** — `ApsMachineParametersDevice` (name `aps`), only loaded on APS subnet. Label is `"aps_machine"` (not `"baseline"`) because `ApsCycleComputedRO.get()` raises `UnboundLocalError` when APS cycle data is unavailable — a known apstools bug. Restore `"baseline"` once fixed upstream.

### Utilities (`src/id6_b/utils/`)

- **`run_engine.py`** — Module-level `RE`, `bec`, `peaks` and `cat` placeholders, all `None`. `startup.py` populates them after `init_RE()` (`cat` at `startup.py:84`). Plans import the module (not the names) so they see the live values at call time: `from ..utils import run_engine as _re_module; _re_module.RE.md[...]`.

- **`peak_statistics.py`** — pure `peak_statistics(x, y)` → `{"cen", "com", "max", "min", "fwhm"}`. Definitions copied from `bluesky.callbacks.fitting.PeakStats` so the numbers match the `cen` table BEC prints: `max`/`min` are `(x, y)` pairs, `com` is the intensity-weighted mean, `cen` the mean of the interpolated half-maximum crossings, `fwhm` their span. Returns `None` for a statistic that is undefined (fewer than 2 points, all-NaN, a flat curve). NaNs are dropped pairwise first — monitor division produces them wherever the monitor read zero. **No ophyd, Qt or bluesky import at module scope**, deliberately: the GUI process and the kernel both import it, which is what keeps the Scan plot buttons and the `cen()` plan from drifting apart.

- **`counters_class.py`** — `CountersClass` + singleton `counters`. Holds the detector list and monitor channel for scan plans. Looks up devices from `oregistry` lazily (safe to import before devices are created). `IDEAL_ORDER = ["scaler", "lambda250k"]` controls detector priority; add new detector names there as hardware is added. For a detector to appear in `counters()` it must implement `plot_options` (list of channel name strings) and `select_plot(channels)` (sets `Kind.hinted`). Adding `plot_signals` (name → signal) is optional but makes the detector's channels appear in the GUI's Detectors tab for `Kind` editing. Usage:

```python
from id6_b.utils.counters_class import counters
counters()                           # interactive channel/monitor selection
counters.plotselect(dets=[1], mon=0) # non-interactive
RE(bp.count(counters.detectors))
```

- **`experiment_utils.py`** — `ExperimentClass` + singleton `experiment`. Manages experiment paths: `base_experiment_path`, `sample`, `file_base_name`. `experiment_path` property returns `base_experiment_path / sample`. Call `experiment_setup()` once per session before running local scans. Also exports `experiment_change_sample()`. No DM/ESAF/proposal dependencies.

```python
from id6_b.utils.experiment_utils import experiment_setup
experiment_setup("/data/2024-1/user_name", sample="MyFilm", base_name="scan")
# or interactively:
experiment_setup()
```

### Plans (`src/id6_b/plans/`)

- **`sim_plans.py`** — simulation-only plans for testing (`sim_count_plan`, `sim_rel_scan_plan`, `sim_print_plan`)
- **`dm_plans.py`** — APS Data Management workflow integration (`dm_submit_workflow_job`, `dm_list_processing_jobs`)
- **`local_preprocessors.py`** — Plan decorators used by `local_scans`:
  - `configure_counts_decorator(detectors, time)` — sets `preset_monitor` on all detectors; restores originals on exit
  - `extra_devices_decorator(extras)` — temporarily demotes extra devices from `Kind.hinted` to `Kind.normal` so they don't appear in BEC plots
- **`local_scans.py`** — Main user-facing scan plans. All plans configure the NeXus writer, collect extra devices (undulator energies when scanning energy, diffractometer when scanning HKL), and write rich run-start metadata. Available plans: `count`, `ascan`, `lup`, `grid_scan`, `rel_grid_scan`, `mv`, `mvr`, `abs_set`. All support `fixq=True` to hold HKL position constant (useful for energy scans). No dichro, lock-in, phase plate, or qxscan support.

```python
# One-time setup per session:
experiment_setup("/nsls2/data/6idb/2024-1/user", sample="Fe3O4", base_name="scan")

# Scans:
RE(count(5, 1.0))                              # 5 points, 1 s each
RE(ascan(energy, 7.1, 7.15, 51, 1.0))         # absolute energy scan
RE(lup(sample_x, -0.5, 0.5, 51, 1.0))         # relative scan (returns to start)
RE(grid_scan(sample_y, -1, 1, 11, sample_x, -1, 1, 11, 1.0))
RE(ascan(energy, 7.1, 7.15, 51, 1.0, fixq=True))  # hold HKL during energy scan
```

- **`center_maximum.py`** — `cen`, `com`, `maxi`, `mini` (plus POLAR's `cen2`/`maxi2`/`mini2` aliases): move one positioner onto a feature of the **last** scan, so the alignment loop is a plan rather than a copy-and-paste of the number BEC printed:

```python
def align():                                   # one RE() call, so Ctrl-C stops the lot
    for step in [0.5, 0.05]:
        yield from lup(sl1.top, -step, step, 41, 1.0)
        yield from cen()
```

  Every argument is optional — `cen(positioner=None, detector=None, monitor=None)`. The positioner is inferred from `start["hints"]["dimensions"]` (refusing anything but a single 1D dimension) and the detector from the primary descriptor's hints. **Both inferences raise a named `ValueError` naming the candidates rather than guessing**, so a macro fails loudly at that step instead of moving the wrong motor. Note the scaler hints every *named* channel, so at 6-ID-B a bare `cen()` only works when `counters` has narrowed the selection to one; otherwise pass `detector="Ion Chamber 2"` (real channel names contain spaces). `monitor=` divides by another field first, matching the Scan plot tab's Mon selection.

  **The values come from the catalog, never from `bec.peaks`** — that is a correctness requirement, not a preference. `BestEffortCallback` is a `QtAwareCallback`: under a Qt matplotlib backend it emits each document to the Qt main thread through a queued signal (`bluesky/callbacks/mpl_plotting.py:76`), so BEC's `stop()` — where `peaks` is filled in — runs *after* the plan has already moved on. Inside a macro `peaks` is still empty when `cen()` executes; bisected as `{'det': 0.12…}` under Agg versus `{}` under qtAgg. `cat.v1.insert` is a plain callback, so the run is in the catalog synchronously. Columns are read one at a time with `run.primary.to_dask()[field].compute()`; a bare `primary.read()` would pull back every Lambda 250K image. Descriptors live at `run.primary.metadata["descriptors"]` (**not** `run.primary.descriptors`, which does not exist), and a hinted key with a non-empty `data_keys[k]["shape"]` is an image and is skipped.

  `_resolve_field()` maps a hint field back to a device by walking `oregistry.all_devices`, because a hint field is *not* a device name (`psic.h` reports `psic_h`) and `oregistry.find()`'s positional argument is a fuzzy `any_of` match that raises `MultipleComponentsFound` as often as it succeeds. A field is hinted by the leaf *and* by every container above it, so the device hinting the fewest fields wins, with the longer name breaking ties.

### Callbacks (`src/id6_b/callbacks/`)

- **`spec_data_file_writer.py`** — SPEC-format data file output (enabled in `iconfig.yml`)
- **`nexus_data_file_writer.py`** — NeXus/HDF5 data file output. Contains `MyNXWriter(NXWriterAPS)` with:
  - `external_files = {}` dict for area-detector HDF5 ExternalLinks (`{det_name: rel_path}`)
  - `write_entry()` — writes `layout_version`, creates `h5py.ExternalLink` entries for each detector, resets `external_files`
  - `write_streams()` — skips external image data (linked separately), writes EPOCH + relative `time` datasets
  - Module-level singleton `nxwriter = MyNXWriter()` configured from `iconfig`. **Not** subscribed globally — `local_scans` subscribes it per-scan via `@subs_decorator(nxwriter.receiver)`. `local_scans` also calls `nxwriter.wait_writer_plan_stub()` at the end of each scan.

## polar_example Reference Library

`src/polar_example/polar_common/` is the shared device library from the APS POLAR group (4-ID beamlines). It is **not installed as a package** and is **not tracked by git** (untracked, lives only in the working tree as a local reference). When the user asks to port a device to `id6_b`, copy the relevant file from `src/polar_example/polar_common/devices/` and adapt it.

**Already ported to `id6_b/`:**
- `devices/aps_status.py` — matches the polar_common version
- `devices/aps_undulator.py` — adapted with 6-ID-B-specific deadband/tracking logic
- `devices/monochromator.py` — adapted: prefix `6ida1:`, motors m8/m11/m13/m15, `y_offset` and `y_sign` as soft `Signal` components, no labjack PZTs
- `devices/energy_device.py` — copied as-is; feedback hooks present but not yet wired to a 6-ID-B feedback device
- `devices/scaler.py` — adapted for single scaler
- `utils/counters_class.py` — ported with `IDEAL_ORDER = ["scaler", "lambda250k"]`
- `utils/experiment_utils.py` — simplified (no DM/ESAF/proposal/server); just path + sample + base_name management
- `utils/run_engine.py` — new module for RE/bec/peaks/cat references (populated by `startup.py`)
- `utils/peak_statistics.py` — new; not in polar_common, which reads BEC's `peaks` directly
- `callbacks/nexus_data_file_writer.py` — adapted: `MyNXWriter(NXWriterAPS)`, per-scan subscription, ExternalLink support
- `plans/local_preprocessors.py` — adapted: no dichro/lockin decorators
- `plans/local_scans.py` — adapted: no dichro/lockin/phase plates/vortex_sgz/qxscan; keeps `fixq`
- `plans/center_maximum.py` — rewritten rather than ported: the polar_common version reads `bec.peaks`, which is empty mid-plan under a Qt backend, and resolves the positioner with `oregistry.find(<hint field>)`, which is the wrong lookup

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

### GUI (`src/id6_b/gui/`)

Qt window launched by `id6b-gui` (`[project.gui-scripts]` → `id6_b.gui.app:main`).
Vertical splitter: parameter tabs on top, a real `RichJupyterWidget` IPython
console below. Purely additive — it runs the same `from id6_b.startup import *`
bootstrap in a Jupyter kernel, so the plain IPython workflow is untouched.

- **`kernel.py`** — `KernelSession` owns a `QtKernelManager` plus **two**
  clients: the console's, and a separate `BlockingKernelClient` used only to
  fill the tabs. They are separate so polling stays invisible in the console
  (`include_other_output` defaults to `False`). `autorestart` is set **False**:
  a silent restart would drop every EPICS connection mid-experiment, and it
  also caused qtconsole to reset the console on a spurious startup poll.
  `StatusPoller` (1 s `QTimer`) reads two sources:
  - `.re_md_dict.yml` — `StoredDict` writes it from a *background* thread
    (`delay=5`), so it keeps updating **while a scan is running**. This is the
    scan-safe source for catalog, user, proposal, beamline, and `scan_id`.
    Resolved against the kernel's cwd, since the file is CWD-relative.
  - `user_expressions` — only when the kernel is idle, for values that never
    reach the file (SPEC filename, experiment paths). Uses
    `silent=False, store_history=False`: **`silent=True` suppresses
    `user_expressions` entirely**, and `store_history=False` keeps the console
    prompt number intact. At most one request is in flight, which is what stops
    a burst of queued polls when a long scan ends.
  `_drain_iopub()` reads the **broadcast** iopub channel — originally only for
  busy/idle — and re-emits each message as `iopub_message`, which is what feeds
  the console log (see `transcript.py`). iopub is a broadcast, so the poll
  client sees everything the console renders even though the console is on a
  different client. **The poller's own traffic is filtered out first**, by
  `msg_id`: `_poll_kernel` must send `silent=False` (or `user_expressions` are
  ignored), so the kernel broadcasts an `execute_input` for an empty cell once a
  second, and `execute_once` — silent, but able to raise — would put GUI-internal
  tracebacks in the log. Both record their `msg_id` in a
  `deque(maxlen=OWN_REQUEST_MEMORY)` and `_drain_iopub` skips any message whose
  `parent_header.msg_id` is in it. Filtering by `msg_id` rather than by empty
  code is what also covers `execute_once`, and it deliberately does **not**
  filter the bootstrap: that goes out on the *console's* client, and its
  device-loading log is exactly what the transcript is for.

  `wait_until_ready()` polls `kernel_info` (non-blocking, via `QTimer`) and
  emits `ready` before anything is executed. **Do not execute before this
  fires:** qtconsole's `_handle_status` treats the kernel's own
  `status: starting` message as a crash-restart if it arrives while the widget
  is executing, which produced a spurious "kernel restarted" banner and reset
  the console. The status poller is stopped during the handshake so only one
  reader touches the shell channel.

  `bootstrap()` sends the startup cell **on the console's own client with
  `silent=True`**, not through `console.execute()`. The cell is several hundred
  lines of GUI helper definitions, and `execute()` echoed all of it into the
  console and spent prompt `In [1]` on it, so the user's first command started
  at `[2]`; `silent=True` suppresses the `execute_input` broadcast and leaves
  the execution counter alone. It is deliberately **not** qtconsole's hidden
  execute (`console.execute(source, hidden=True)`), which sets `_hidden` and so
  swallows every `stream` and `error` message too — sending on the client
  directly means the widget never registers the request, so the device-loading
  log and any traceback still appear, inserted above the prompt the way
  background output is. It must be the *console's* client, not the poll client:
  separate sockets give no ordering guarantee, so the live-plot publisher
  subscription could arrive before the import and fail on a missing `RE` —
  which is also why it is appended to the same cell. The prompt is live while
  this runs, where `execute()` used to block it, so a one-line notice says that
  anything typed before startup finishes will run afterwards.

  `HELPERS_CODE` also installs the `_gui_*` helpers the tabs poll for. Two serve
  the peak buttons: `_gui_scan_options()` returns an `axis_fields` map of
  *hinted field* → *dotted path* alongside the axis list it already built (a
  hinted field is not a device name, so the Scan plot tab cannot turn
  `sim_motor` into something `mv()` accepts without it), and
  `_gui_peak_fields()` returns the last run's hinted scalar detectors as
  suggestions for the Macro tab. The latter goes through
  `center_maximum._hinted_detectors(cat[-1])`, **not** `bec.peaks`, so the
  suggestions are exactly what `cen()` will accept and do not depend on BEC's
  asynchronous fill.

  `start()` also **advertises the kernel**: `_write_pointer()` drops
  `.id6b-gui-kernel.json` (`connection_file`, `pid`, start time, cwd) into the
  kernel's cwd, and `shutdown()` removes it. That is how the MCP server finds
  the session without being configured — see `mcp_server/` below. A failure to
  write it is logged and leaves `pointer_path = None`; the GUI runs regardless.
  `bootstrap()` sends two more helper strings in the same cell as the other
  three — `MCP_HELPERS_CODE` and `MOTION_HELPERS_CODE` — and
  `KERNEL_EXPRESSIONS` carries `"mcp_pending": "_gui_mcp_pending_info()"`, so
  the Agent tab arrives on the existing 1 Hz poll with no new reader on the
  shell channel.
- **`docstream.py`** — live-plot plumbing. The kernel publishes documents over
  ZMQ (`Publisher`) into a `Proxy` + `RemoteDispatcher` running in daemon
  threads *in the GUI process*, which re-emits them as a Qt signal on the main
  thread. **The kernel must stay Qt-free**, which is why plotting is not done
  there: `%matplotlib qt` *before* `id6_b.startup` breaks `import gi` (Qt vs
  PyGObject shared-library clash, and `hklpy2`/`libhkl` need `gi`), while
  *after* startup the RunEngine lacks the Qt teleporter it would have built at
  construction and the next scan raises `QObject::setParent: ... different
  thread` and **hangs the RunEngine**. Ports are chosen free at startup. The
  publisher subscription is appended to the *same cell* as the bootstrap —
  sending it on the poll client instead gives no ordering guarantee against the
  console client, and it would fail on a missing `RE`.
- **`tabs/base.py`** — `BaseTab` with `title` plus three no-op hooks:
  `on_metadata`, `on_kernel_values`, `on_kernel_state`. A tab may also define
  `on_document(name, doc)` to receive streamed Bluesky documents.
- **`tabs/scanplot.py`** — `ScanPlotTab`, a live matplotlib canvas embedded via
  `backend_qtagg` (no `pyplot`, so no global state). x comes from
  `start["hints"]["dimensions"]`, falling back to elapsed time for `count()`.
  **One curve per plottable detector field**, colour-matched to the curve.
  Fields hinted by the descriptor — i.e. whatever `counters` selected — are
  ticked by default; other numeric scalars are listed unticked. Array/image
  keys are excluded by their non-empty `shape`. Data is kept for every field
  regardless of tick state, so enabling a curve mid-scan shows its full
  history, and `relim(visible_only=True)` keeps hidden curves from stretching
  the axes. Fresh axes per run; curves stay after the scan. BEC's inline
  console plot is unaffected, so you get a live plot *and* the after-scan
  inline one.

  **The field selectors are a scrollable panel to the right of the canvas**,
  one row per field, with two columns: a **Plot** check box and an exclusive
  **Mon** radio. The panel is a fixed 260 px and scrolls rather than squeezing
  the canvas, since names like `lambda250k_stats1_total` are long. Choosing a
  monitor divides every plotted value by that field's value **at the same
  point** (`_values()`); the curve label becomes `It / I0` and the status line
  says so. `_series[field]["y"]` always holds the **raw** values, so changing
  or clearing the monitor recomputes the whole history rather than only
  affecting new points — and the "no monitor" row (checked by default) is a
  real member of the button group, which is how the division is switched off.
  A zero or missing monitor reading yields `NaN`, leaving a gap instead of a
  spike or an exception mid-scan. The monitor's own Plot box is **disabled,
  not unticked**, so Qt keeps its state and the curve returns when the monitor
  changes; `_shown()` treats a disabled box as not plotted. `_on_event`
  appends every raw value *before* touching any line, because the monitor's
  own point has to be in before a ratio can be computed.

  `_clear_selectors()` calls `setParent(None)` as well as `deleteLater()` on
  the old rows: a deferred delete is not processed until the event loop gets
  round to it, and until then the old check box is still a child of the panel
  and paints over the new row at its former position.

  **`grid_scan`/`rel_grid_scan` render as a live image instead of curves.**
  Detected from `start["hints"]["gridding"] == "rectilinear"` plus `shape` and
  `extents`; `dimensions` is ordered outer(slow) first. Orientation matches
  BEC's `LiveGrid` — the inner (fast) axis is horizontal, the outer (slow)
  vertical — so the live image and the inline one agree. Cells are located from
  the **readback values against `extents`**, not from event order, so snaked
  and out-of-order points land correctly without modelling the trajectory.
  Unvisited cells stay `NaN` (blank) so the mesh fills in visibly. An image can
  show one channel, so in this mode the check boxes act as a selector, and both
  scanned axes are excluded from the colour candidates. Monitor normalisation
  applies here too: `_grid_cells` records each event's `(row, column)`, so
  `_rebuild_grid()` recomputes the whole mesh from history when the monitor or
  the displayed channel changes. Picking the displayed channel *as* the monitor
  moves the image to another channel rather than showing a field of ones.

  Two related colourbar defects were fixed while adding this. `_on_start` used
  to call `_reset_state()` — which nulls `_colorbar` — before the removal
  check, making the removal dead code and stacking a new colourbar on every
  consecutive grid scan; `_discard_colorbar()` now runs first. And
  `_select_grid_field()` used to null `_image`, so switching the channel
  mid-scan created a second `imshow` *and* a second colourbar; it rebuilds in
  place instead. (The colourbar lives in its own axes, so `axes.clear()` does
  not remove it.)

  **A peak row under the plot**, in two lines:

  ```
  Peak of [field v]  cen …  com …  max …  min …  fwhm …
  [x] Markers  [Go to cen] [Go to com] [Go to max] [Go to min]
  ```

  It moves the scanned axis onto the peak after a scan. `cen` is the
  half-maximum midpoint — what BEC prints, so the button and the console can
  never disagree. `fwhm` is shown for reference and gets no button (it is a
  width, not a position). The statistics come from
  `peak_statistics(self._x_data, self._values(series))` — **the same arrays the
  canvas draws**, so the Mon selection is inherited for free: with a monitor
  chosen the peak is that of `It/I0`, matching what is on screen, and nothing
  extra is fetched from the kernel.

  Two lines rather than one because a `QHBoxLayout` does not wrap — at the
  default 1100 px window the five statistics at `%.10g` plus four buttons
  overflowed and Qt *clipped* the label, silently eating `min` and `fwhm`. For
  the same reason the buttons carry no value in their text; the number is in the
  peak row and the exact `RE(mv(…))` is in the tooltip.

  **Dotted vertical markers.** `MARKER_STYLES` pairs each of `cen`/`com`/`max`/
  `min` with a colour; `_draw_markers()` puts an `axvline` at each in that
  colour, and the same colour is used for the statistic's name in the peak row
  and for its button — that triple is how a line is identified. Deliberately
  **not** through the matplotlib legend, which belongs to the curves:
  `_rescale()` builds the legend only from `self._series` lines and removes it
  below two curves, so labelled markers would come and go with the curve count.
  Hence `label="_nolegend_"`. The colours are picked away from the default curve
  cycle and the markers are dotted where curves are solid, so a marker still
  reads as a marker if a curve lands on the same hue. The **Markers** check box
  toggles all four.

  Markers are updated from `_update_peak()` only — never per event — so they
  settle when the scan stops, or when the curve, the monitor or the toggle
  changes, rather than jittering through the scan. `_remove_marker()` wraps
  `line.remove()` in `except (ValueError, NotImplementedError)`: an
  `axes.clear()` may already have destroyed the artist.

  A button emits a **literal** `RE(mv(sim_motor, 1.023426152))` through
  `run_in_console()`, not `RE(cen())`: the value is already on screen, it needs
  no catalog lookup, and it reads back in the history as a plain move. (The
  Macro tab's equivalent must emit the *plan* instead — see `tabs/macro.py`.)
  The dotted path comes from `axis_fields` in `_gui_scan_options()`; an
  unresolvable field disables the buttons and says which one. The row is
  **hidden** when there is no real x axis — a `count()` (`_use_elapsed_time`) or
  a grid scan — and the buttons are disabled while the scan is running or the
  kernel is busy, each with the reason in the tooltip. A statistic can be `None`
  (`cen` has no half-maximum crossing on a flat trace); that one gets no marker
  and its button alone is disabled, while the others still work.
- **`tabs/scan.py`** — `ScanTab`: pick a plan (`count`, `ascan`, `lup`,
  `grid_scan`, `rel_grid_scan`), detectors, axes, points and time per
  point, and press Scan. The form encodes the argument-order difference between
  the plans — `ascan`/`lup` share one point count across axes (a trajectory),
  the grid plans give each axis its own (a mesh) — so it cannot build a call the
  plan rejects. The exact command is shown before it runs and is executed with
  `run_in_console()`, so a scan stays an ordinary interruptible, logged command
  that the Scan plot tab draws live.

  Axes come from `_gui_scan_options()`, which finds them **by capability, not
  class**: `callable(obj.set) and hasattr(obj, "position")`, plus writable
  root-level `Signal`s. The classes disagree — `EpicsMotor` and the hklpy2
  pseudo axes are `PositionerBase`, `sim_motor` is a `SynAxis` (not a
  positioner but has `set`/`position`), and `energy` is an `EnergySignal`
  (`set` but no `position`). All three are scannable; a positioner-only walk
  misses two of them. A movable with movable children (`mono`, `psic`, `sl1`)
  is treated as a container, so the list offers `mono.energy` and `psic.h`
  rather than the container. 101 axes today.

  Detector checkboxes list only devices with `preset_monitor`. **The
  `detectors=` kwarg is omitted when the selection is unchanged**, because
  passing it makes the plan skip `_setup_detectors()` (`local_scans.py:193`) —
  which is where the negative-time validation lives. For the same reason,
  choosing "monitor counts" (a negative time) disables the detector
  checkboxes and defers to the counters selection.

  **Three axis rows for `ascan`/`lup`, two for the grid plans** — see
  `scancode.axis_limit()`. Each extra row is revealed by its own check box and
  needs the one before it, so the rows can only ever be filled in order. The
  ceiling is a GUI limit, not a plan limit: `local_scans` only checks
  `len(args) % 3` / `% 4`, so both families take any number of motors. The grid
  plans are held at two because `ScanPlotTab`'s live image is 2D
  (`rows, columns = self._grid["shape"]`), and a third dimension would be
  silently collapsed onto the first two rather than refused. A label under the
  rows says so when a grid plan is selected.

  Every `QGridLayout` here sets **explicit column stretches**. With the default
  all-zero stretch Qt spreads leftover width across every column, so a
  left-aligned `QLabel` drifts ~200 px from the field it names; the label
  columns get stretch 0, the axis combo and a trailing filler column take the
  slack, and the numeric boxes are capped at `VALUE_WIDTH`.
- **`scancode.py`** — the argument-order rule, in one place because both the
  Scan tab (runs it now) and the Macro tab (writes it into a plan) need it and
  getting it wrong is silent: `ascan`/`lup` take `motor, start, stop, ..., num,
  time` (`len % 3 == 2`), the grid plans `motor, start, stop, num, ..., time`
  (`len % 4 == 1`). Every numeric slot is a **string** — the Scan tab formats
  its spin-box floats first, the Macro tab passes free text straight through so
  a loop variable (`centre - 0.1`) survives into the generated code. Also holds
  the shared axis-count rules — `MAX_TRAJECTORY_AXES`, `MAX_GRID_AXES`,
  `axis_limit()`, `active_axes()` — for the same reason: the Scan tab and the
  Macro tab's Scan component must agree on them.
- **`tabs/macro.py`** — `MacroTab`: a component chooser on the left inserts code
  into a Python editor on the right, which is the macro. A macro is a **Bluesky
  plan** — one generator function run as a single `RE(macro())` — so Ctrl-C,
  `RE.pause()` and resume apply to the whole loop rather than to whichever scan
  is running. Everything it calls is already in the session namespace: the
  `local_scans` plans (`startup.py:141`) and `bps` (`startup.py:110`); the
  generated `import bluesky.plan_stubs as bps` is there so a saved file also
  works under `%run`.

  Seven components — Loop, Set value, Wait, Scan, **Go to peak**, Print, Code.
  **Every numeric
  field is free text, not a spin box**, which is what lets a loop variable be
  used as a value or a scan limit; validation is "non-empty" and the real check
  is the `compile()` on Load. Loop values are either a literal list or
  start/stop/steps expanded into one, so the temperatures are visible in the
  macro itself; text starting with `[`/`(` is used verbatim, so `range(...)`
  and comprehensions work.

  Generation is **one way**: the builder writes into the editor and never reads
  it back, so nothing hand-edited is ever silently rewritten — what is saved and
  run is exactly what is on screen. Insertion takes its indent from the cursor's
  line (from the previous non-blank line if blank, +4 if that line ends `:`),
  and **replaces a line whose only content is a placeholder** (`pass`, or the
  template's `yield from bps.null()`). That is what chains the components: a
  loop leaves the cursor on its own `pass`, so the next insert becomes the
  loop's first statement. Tab inserts four spaces — a literal tab would raise
  `TabError` against the generated spaces.

  **Load** compiles the source in the GUI process first, so a `SyntaxError` is
  reported with its line number and never reaches the console, and refuses a
  function with no `yield` (`RE(f())` on a plain function fails). It then sends
  the source with `run_in_console()`, defining the macro exactly as if pasted —
  **nothing executes on Load**. **Run** is `RE(<name>())` for the first
  top-level `def`, disabled until a successful Load and re-disabled as soon as
  the text changes, so it can only call a definition the session actually has.
  Both need the kernel idle; the editor is always editable.

  **Go to peak** emits `yield from cen()` (or `com`/`maxi`/`mini`), with
  `axis`, `detector=` and `monitor=` added only when filled in. Here the *plan*
  has to be emitted, not the literal `mv` the Scan plot buttons produce: a macro
  is written before the scan it will align on, so the peak position cannot be
  known at generation time. That is the whole reason `plans/center_maximum.py`
  has to exist as plans rather than as GUI code. The three combos are editable —
  the detector list is only a suggestion from the last scan
  (`_gui_peak_fields()`), because the field a future scan will hint is not known
  either, and real scaler channel names contain spaces (`Ion Chamber 2`), so
  they are emitted with `!r`. This is what makes the two-pass alignment loop a
  single interruptible plan:

```python
def align():
    for step in [0.5, 0.05]:
        yield from lup(sim_motor, -step, step, 41, 1.0)
        yield from cen()
```

  Set-value targets come from `_gui_macro_targets(name)`, fetched per device on
  demand rather than all at once (~600 names otherwise). It returns positioners
  *and* writable signals because `walk_signals` alone is wrong for a motor —
  `mv(sl1.top, 3)` wants the `EpicsMotor`, not `sl1.top.user_setpoint`. The
  device combo is editable, so the fetch hangs off `currentTextChanged`, not
  `currentIndexChanged`: `setCurrentText` on an editable combo only writes the
  line edit and never moves the index. Files are plain `.py`, defaulting to
  `<kernel cwd>/macros`, with the last-used directory in
  `QSettings("APS", "id6b-gui")`.
- **`diffract3d.py` + `tabs/diffract_panel.py`** — 3D model of the 4S+2D
  diffractometer on the right of the HKL tab, ported from
  `~/jwkim/python/diffract/diffractometer_bluesky.py`. The geometry constants
  and kinematic chain are copied **verbatim** — they were matched against the
  real instrument. Radio buttons switch between the *current* angles and the
  last *calculated* ones; check boxes toggle the scattering plane, the ψ
  reference vector and Q. The reference arrow is `UB @ (h2, k2, l2)`, taking UB
  from `_gui_hkl_state()` and h2/k2/l2 from the tab's ψ reference boxes, so
  there is one source of truth.

  **Requires `vtk`, `pyvista`, `pyvistaqt`** (plus `pooch`, `scooby`) — the
  `gui3d` extra in `pyproject.toml`, not part of a plain install.
  `vtk`/`pyvista` are imported lazily and `diffract3d.AVAILABLE` gates the
  panel, so the GUI runs normally without them and the panel shows the install
  command instead. The pure-maths half (`Rx/Ry/Rz`, `rot_about4`,
  `rotation_to_align`, `Diffractometer.chain_matrix`) has no VTK dependency and
  is unit-testable without the packages. They **are** installed in the
  `6idb-bits` env, so `Diffract3DPanel` opens an X render window as soon as
  `HklTab` is constructed: a headless (`QT_QPA_PLATFORM=offscreen`) test of the
  tab has to monkeypatch `tabs.hkl.Diffract3DPanel` to a plain `QWidget` stub or
  VTK aborts the process. Such a test must also `import hklpy2` **before** Qt —
  Qt first leaves `gi` with an undefined symbol against the wrong
  `libgobject`, the same clash that keeps the GUI's kernel Qt-free.
- **`hkl_bridge.py` + `tabs/hkl.py`** — `HklTab`: samples and lattices,
  reflection table (editable h/k/l and angles) with first/second orienting
  selection, Compute UB, mode selection, live current-position readout
  (h k l, six angles, 2θ, ψ, ψ reference, λ/energy) and an hkl → angles
  calculator with a Move button. A selector chooses `psic` (red "real motors"
  banner) or `psic_sim` (green "simulated").

  `hkl_bridge.HKL_HELPERS_CODE` holds the kernel-side `_gui_hkl_*` functions,
  installed with the bootstrap. It mirrors the call sequences in
  `utils/hkl_utils_pete.py` but **does not import it** — that module builds its
  own `RunEngine` at module scope, which would collide with the session's.
  Behaviours copied deliberately: `setmode`'s rule that a `vertical` mode
  presets the horizontal detector to 0 (and vice versa) via
  `core.solver_real_axis_names`; `setaz`'s temporary switch into a
  `psi_constant_*` mode to write `h2/k2/l2` (`core.extras` is empty in other
  modes, so writing without it silently does nothing) then restoring the
  original mode; and `compute_UB`'s `forward(1, 0, 0)` after `calc_UB`, without
  which a later `wh()` fails.

  **Fixed angles.** Every mode solves some real axes and holds the rest
  constant, and for a constant axis `forward()` uses a *preset* if the mode has
  one and otherwise the live motor reading (`hklpy2/ops.py:625`). The Mode group
  therefore carries one row per constant axis — a **Fix check box, an angle box
  and `deg`** — so `psic.core.presets = {"phi": 30}` no longer needs the
  console. Unticked means *no preset*, i.e. that axis follows its motor;
  hklpy2's own distinction, made visible rather than hidden behind an always-on
  value box. Presets change *computed* solutions only — nothing moves — which
  the block says in a caption.

  Which axes get a row comes from **`core.constant_axis_names`**
  (`ops.py:927`), never from parsing the mode name: `constant_phi_vertical`
  holds `mu, phi, nu`, `lifting_detector_phi` holds `mu, eta, chi`,
  `bissector_horizontal` only `delta`. `_rebuild_preset_rows()` runs **only when
  the axis list changes** — values are refreshed on every state read, and
  tearing the widgets down each time would pull the box out from under whoever
  is typing. Old rows get `setParent(None)` *as well as* `deleteLater()`, the
  trap `ScanPlotTab._clear_selectors` documents.

  Writes are debounced 400 ms (`_presets_timer`), a clone of the ψ reference
  boxes' `_reference_timer`, and go through `_gui_hkl_set_presets`. Three
  details that matter:
  - hklpy2's `presets` setter **silently drops** any axis not constant in the
    current mode (`ops.py:862`), so the helper checks against
    `constant_axis_names` first and names the dropped ones in its reply instead
    of letting a typed value vanish.
  - `_gui_hkl_set_mode` **merges** rather than assigns. It used to end with
    `dev.core.presets = {axis: 0}`; presets are stored *per mode* and restored
    on re-selection, so that wiped an angle the user had fixed in that mode
    earlier — re-picking the mode silently undid their setting. It now defaults
    the unused detector angle to 0 only when that axis has no preset yet.
  - `_gui_hkl_calc` takes a `presets=` argument and the tab re-sends the
    on-screen values with every Calculate, so pressing it straight after typing
    cannot solve against a stale preset the debounce timer has not written yet.

  A fixed angle decides *which* of many solutions the Move goes to, so the
  confirmation dialog lists it.

  A **progress bar sits under the Move button**. The kernel's shell channel is
  blocked for the whole move, so progress cannot be polled through it — the
  readback PVs are watched **directly over Channel Access from the GUI
  process** instead (`real_pvs` in `_gui_hkl_state`, read with `epics.PV`).
  Progress is the mean fractional travel over axes that actually move. Soft
  axes (`psic_sim`) have no PVs, so the bar goes indeterminate rather than
  inventing a number. Completion is decided from `StatusPoller.kernel_state`
  after a 2 s grace period, **not** from `kernel_state_changed`: that signal
  only fires on transitions, and a quick move starts and finishes inside one
  poll interval, which left the first version stuck on "moving…" forever.
  A 600 s timeout stops it spinning if the kernel never comes back.

  Safety: every mutating control is disabled unless the kernel is idle, and
  **Move runs through the console** — `RE(bps.mv(...))` via
  `BaseTab.run_in_console()` — so it uses the session RunEngine, lands in
  history, and Ctrl-C aborts it. Move stays disabled until a successful
  Calculate, so the confirmation can only show angles the solver returned.
  Reflections are edited in place (`Reflection.pseudos`/`.reals` are settable).
- **`tabs/detectors.py`** — `DetectorsTab`, a per-channel `Kind` selector
  grouped by detector. Unlike the Scan plot check boxes, which only hide
  already-recorded curves, this changes kernel-side configuration and so what
  *future* scans read. **Apply is disabled unless the kernel is idle**:
  changing `Kind` mid-scan would desync the descriptor. Applies via
  `_gui_set_kinds()` then re-reads so the tree shows what the kernel actually
  accepted, and refreshes on every `stop` document since plans may re-hint
  channels themselves.

  **The four kinds are radio buttons, one tree column each** (`KIND_CHOICES` =
  `hinted` / `normal` / `config` / `omitted`), so a row is one click and the
  tree reads down a column. The kind names appear once, in the header, which is
  also where `KIND_TOOLTIPS` explains them (repeated on every radio). `omitted`
  *is* offered — it is how an unnamed scaler channel is kept out of the
  descriptor — with the tooltip warning that omitting a named channel loses its
  readings entirely.

  Three details that are easy to get wrong. `_KindRow`'s `QButtonGroup` is
  deliberately **parentless** and owned by the row object: parented to the tree
  it would survive `clear()` and leak one dead group per channel on every
  refresh (there is a test asserting zero groups under the tree). The header
  sets `setStretchLastSection(False)`, or the `omitted` column absorbs the whole
  window width and its radio ends up an inch from the other three. And `Kind` is
  a *flag*, so a channel can report a composite like `normal|config` that matches
  no radio — nothing is checked, the raw value is shown in the free-text detail
  column (column 1, which holds the PV prefix on device rows), and the row
  contributes no change until a kind is picked.

  The tab also manages **extra devices** (`counters.extra_devices`) — an Add…
  picker over `oregistry.root_devices` lets any EPICS device (temperature,
  capacitance, source meter) be recorded at every scan point. This is the right
  slot for such devices: `local_scans` does `bp_count(detectors + extras, ...)`,
  so extras are recorded but skip `configure_counts_wrapper`, which calls
  `rd(det.preset_monitor)` on **every detector**. Only `scaler` and
  `lambda250k` have `preset_monitor`, so putting a thermometer in
  `counters.detectors` raises `AttributeError: preset_monitor`, while as an
  extra it works — verified with `keithley2400`. `extra_devices_decorator`
  demotes extras from hinted so BEC ignores them, but the Scan plot tab lists
  every numeric scalar, so they appear there as unticked curves.

  Add/remove go through `poller.execute_once()` rather than a polled
  expression: the poller batches expressions into a single request, so a
  mutation and a read of the same state can share a batch and the read may be
  evaluated first, showing stale values.

  Extra devices get `Kind` control too, as `<name>   (extra)` nodes in the same
  tree. They have no `plot_signals`, so their channels come from
  `walk_signals(include_lazy=False)` and are set by dotted attribute path
  (`_gui_extra_kinds` / `_gui_set_extra_kinds`) — hence two helpers and two
  editor dicts, with `_apply` routing each set to the right one. Omitted
  signals are hidden behind a checkbox: they are not recorded anyway and on a
  motor bundle they dominate (53 of `sl1` + `filters`' 105). Setting an extra to
  `omitted` auto-ticks that checkbox, since the row would otherwise be filtered
  out of the next listing and the change could not be undone.
- **`tabs/devices.py`** — `DevicesTab`, a sortable/filterable table of
  `oregistry.root_devices` (name, class, prefix, labels, connected). Uses
  root devices, not `all_devices`, which also contains every sub-component
  (~1800 entries). Fetched on demand via `BaseTab.request()` →
  `StatusPoller.request_once()`, which merges one-off expressions into the next
  idle poll instead of opening a second reader on the shell channel. The
  per-device `connected` probe needs a `try`, which `user_expressions` cannot
  express, so it calls `_gui_device_table()` — installed in the kernel by
  `kernel.HELPERS_CODE` as part of the bootstrap cell.
- **`transcript.py`** — `ConsoleTranscript`, which appends the console's traffic
  to a plain text file. The console is the record of what was done to the
  instrument and none of it survives on its own: qtconsole trims its scrollback,
  Restart clears the pane, and closing the window loses the lot.

  The content comes from **iopub, not from the widget** — `StatusPoller`'s
  `iopub_message` (above). Taking it there means the file is unaffected by the
  scrollback limit or by a console clear, and it keeps filling **while a scan is
  running**. `execute_input` becomes `In [n]: …` (continuations `   ...: `),
  `stream` goes in verbatim, `execute_result` becomes `Out[n]: …`,
  `display_data` contributes its `text/plain`, and `error` its traceback;
  everything else is ignored, so `status`, `clear_output` and comm traffic cost
  nothing. Deliberately **not** IPython's `%logstart`, which records input and
  results but not stream output, tracebacks or the device-loading log — most of
  what is wanted here. (`apsbits`' `logging_setup` already starts one of those,
  into `<cwd>/.logs/ipython_log.py`; this complements it rather than repeating
  it.)

  The file is opened `"a"` with `buffering=1`, so `tail -f` follows the session
  and a `kill -9` loses nothing; ANSI escapes are stripped (IPython colours its
  tracebacks, and the codes make the file unreadable in an editor). **Append,
  never truncate** — one file can hold a whole experiment and a mis-click on
  Start cannot destroy the earlier transcript. Every write is guarded: an
  `OSError` closes the file and sets `error` rather than raising into the poll
  timer, which would take the whole status display down with it. The module has
  **no Qt import**, so it is testable without a `QApplication`.
- **`tabs/status.py`** — `StatusTab`, the Session/Scan/Files overview. Derives
  RunEngine `running` from kernel-busy, because a running plan holds the shell
  channel and `RE.state` cannot be polled mid-scan.

  The **Console log** group drives the `ConsoleTranscript`: a path field, a
  `Browse…` (`getSaveFileName` with `DontConfirmOverwrite`, since an existing
  file is appended to and Qt's "replace it?" prompt would describe something
  that does not happen), a Start/Stop toggle and a status line. **The path field
  and Browse are disabled while logging**, so the file cannot be swapped out
  from under an open handle. The default is `<session_cwd>/console.log` — the
  kernel's cwd, where the data already goes, not the GUI process's.

  Both the path and the on/off state are remembered in
  `QSettings("APS", "id6b-gui")`, and `app.py` **auto-resumes** logging in
  `MainWindow.__init__` before the poller exists. That is what captures the ~40 s
  device-loading log: `poll_client.start_channels()` runs in `session.start()`,
  so iopub queues from that moment and the first `_drain_iopub` collects all of
  it — otherwise only someone who pressed Start within seconds of launching
  would ever get it. `_do_restart()` writes a `note("kernel restarted")` marker,
  so a cleared console is still explicable from the file, and `closeEvent()`
  stops the log for a footer and a clean close.

  The status line has **its own 1 s `QTimer`, running only while logging**. Not
  `kernel_values_changed`, which stops arriving during a scan — exactly when
  watching the log grow is worth anything — and not `kernel_state_changed`,
  which only fires on transitions. The timer also notices a transcript that
  closed *itself* on a write failure and puts the controls back.
- **`app.py`** — `MainWindow`, the `TABS` list, and the toolbar. The window is
  a vertical splitter kept at **even halves**. Two things are needed for that:
  each tab is wrapped in a `QScrollArea` unless it sets `scrollable = False`
  (otherwise the tallest page pins `tabs.minimumSizeHint()` at ~595 px and the
  console is squeezed to whatever is left), and the even split is applied in
  `showEvent` — `setSizes()` before the first show is measured against size
  hints rather than the real geometry, so it is silently overridden. Console
  appearance controls live here: a font-size spin box (6–32 pt) and a
  Black/White background selector (`set_default_style("linux")` /
  `("lightbg")`; **black is the default**). Both are remembered between
  sessions in `QSettings("APS", "id6b-gui")` →
  `~/.config/APS/id6b-gui.conf`. The font spin box stays in sync with
  qtconsole's own Ctrl+= / Ctrl+- via the `font_changed` signal; changing the
  background does not disturb the font size. When Qt reports a pixel-sized
  font (`pointSize() == -1`) the fallback size is *applied*, not merely
  displayed, so the spin box can never disagree with the console.

**Adding a tab:** subclass `BaseTab`, set `title`, override the hooks you need,
add the class to `TABS` in `app.py`. The window wires the poller signals to
every tab automatically.

**Restart button** does a full kernel restart, clears the console, then waits
for `ready` before re-running the bootstrap. Note `QtKernelManager.kernel_restarted`
fires *only* for an autorestart, never for a deliberate `restart_kernel()` — so
the restart path must drive the bootstrap itself rather than rely on that signal.
The button is disabled until the kernel is ready.

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
