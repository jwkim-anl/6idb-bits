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
9. Import `counters` singleton, `local_scans` plans, the `center_maximum` plans (`cen`, `com`, `maxi`, `mini` and the `*2` aliases), `attenuation_setup`/`auto_atten`, `pva_streaming_setup`/`pva_stream` and `hkl_pv_setup`/`hkl_pv` into the session namespace
10. `from id6_b.utils.hkl_utils_pete import *` — the spec-like console API (`wh`, `ca`, `br`, `ubr`, `setmode`, `setaz`, `setor0/1`, `compute_UB`, the configuration-file readers/writers). It **must** come after step 5: the module resolves `psic`/`psic_sim`/`psic_q`/`psic_psi` out of `oregistry` at import time, so an earlier import raises `ComponentNotFound` — and removing any one of those four from `devices.yml` now aborts the session start rather than just losing that device. Two more import-time side effects: it builds a **second** `RunEngine` on its own event loop, and it calls `set_diffractometer(psic)`, so hklpy2's global current diffractometer is the *real* machine from the moment the session opens — that is what `compute_UB()`, `ca()`, `br()` and `read_diffractometer_config_scan()` act on unless told otherwise. The star import is safe only because `RE` is **not** in that module's `__all__`; adding it there would shadow the session RunEngine.
11. `ConfigurationRunWrapper(oregistry["psic"])` appended to `RE.preprocessors` — saves the diffractometer orientation into every run's start document under the `diffractometers` key, which is the write side of `read_diffractometer_config_scan()`. The list is `["psic"]` only, so `psic_sim` runs carry no configuration and reading one back raises "does not have any hklpy2 configuration saved"; add `"psic_sim"` to make simulator runs restorable too.
12. `hkl_pv.start()` — start the watcher that publishes the orientation, detector centre and axis directions to the `6idb1:` conversion PVs. Deliberately **not** a `default_settings()`: that loop (step 7) runs before the channels are guaranteed connected, and waiting for them there would add seconds to every session start whenever that IOC is down. The first write happens on the watcher's own first tick instead — same visible result, off the startup path.

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
| `id6_b.devices.pva_streaming.PvaStreamControl` | PVA streaming cache control (flag + file name + directory) | `pva_stream` |
| `id6_b.devices.hkl_pvs.HklConversionPVs` | HKL-conversion PVs (UB matrix, detector centre/distance, axis directions); `"baseline"` | `hkl_pvs` |
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
- **`filters.py`** — `FilterBank` (prefix `6idb1:filter:`): `transmission` (readback + `TransmissionSetpoint` write PV), `allin()`/`allout()` helpers, and a read-only `energy` `AttributeSignal` that reports `energy_beamline` or `energy_local` according to `energy_select` ("Mono"/"Local"). Ported from `bluesky/instrument/devices/filter.py`; renamed from `filter` so it no longer shadows the Python builtin. **Do not `mv()` the transmission** — put completion is off and the readback is the transmission the IOC realized rather than the one requested, so `EpicsSignal.set` polls forever for a match that never comes; see the `_WriteOnly` shim under `plans/auto_attenuation.py`.
- **`pva_streaming.py`** — `PvaStreamControl` (prefix `6idb1:`): the three PVs
  that drive the PVA streaming writer — `ScanOn:Value` (cache flag),
  `FileName:Value` and `FilePath:Value` — plus `start_caching()`/`stop_caching()`
  convenience methods. Every component is `kind="omitted"`: the device is never
  in a detector or extras list, and the file it will write is recorded in run
  metadata by `plans/pva_streaming.py` instead. Not labeled `"baseline"` — two
  string PVs buy nothing there, and the streaming server is not always running.

  **The two path PVs hold 39 usable characters.** They are `stringout`
  records (`ScanOn:Value` is a `longout`), and a `stringout`'s `VAL` *is* the
  40-byte `DBF_STRING` field, so there is no longer buffer behind it and the
  long-string (`$`) suffix buys nothing. `/home/beams18/USER6IDB` alone is 22
  characters. Anything longer
  is truncated by the server **silently** — nothing on the client side reports
  it, which is why `plans/pva_streaming.py` abbreviates the home directory to
  `~` and warns on the rest.

- **`hkl_pvs.py`** — `HklConversionPVs` (prefix `6idb1:`): the Channel Access
  PVs the beamline's area-detector analysis converts images with —
  `spec:UB_matrix:Value` (a 9-element waveform, row-major),
  `DetectorSetup:CenterChannelPixel` (2 elements),
  `DetectorSetup:Distance`, the eight `stringout`s naming each axis's sign
  convention (`Mu:DirectionAxis` … `Delta:DirectionAxis`,
  `DetectorSetup:PixelDirection1`/`2`) and the three
  `PrimaryBeamDirection:AxisNumber1`/`2`/`3` scalars. Written by
  `plans/hkl_pv_sync.py`; this module is only the write side.

  The device **is** labeled `"baseline"`, so the geometry a run was taken with
  is recorded in that run rather than only in the IOC, where the next
  experiment overwrites it. The six values an analysis needs to reproduce the
  conversion — UB, centre channel, distance and the two pixel directions — are
  `kind="normal"`; the eight axis sign strings and the three beam components
  stay `kind="omitted"`, being a convention the geometry already implies rather
  than a measurement.

  **That the two waveforms survive a baseline descriptor was checked against a
  real data file**, not assumed — the concern was real enough to be worth
  testing. Baseline is a separate stream read once at open and once at close,
  so an array is described with a shape and is fine there; what an array must
  never be is a *scalar* data key, which is why
  `Lambda250kDetector.default_kinds` strips its ndarray attributes, and why
  `PvaStreamControl` — whose two path strings buy nothing in a stream — is
  still left out of the baseline entirely.

  **Two direction conventions are declared here as module constants**, so they
  are greppable and stated once: `HKLPY2_DIRECTIONS` (what the session solves
  in today, and the default) and `SPEC_DIRECTIONS` (what the future
  `ad_hoc_diffractometer` geometry will use), keyed by `DIRECTION_KEYS` and
  selected through `CONVENTIONS`. `set_directions(convention)` writes only the
  PVs that disagree and returns `{key: (old, new)}`, so a repeated call is
  silent. Nothing selects `spec` yet — it is inert until someone passes
  `convention="spec"` to `hkl_pv_setup`.

  **A convention is eleven PVs, not eight**: the axis sign strings *and* the
  primary beam direction, `(1, 0, 0)` under hklpy2 and `(0, 1, 0)` under spec.
  The beam half is a second mapping — `BEAM_DIRECTIONS`, keyed by `BEAM_KEYS`,
  built from `HKLPY2_BEAM_DIRECTION`/`SPEC_BEAM_DIRECTION` — because it is
  numeric where the other is strings, and three scalar `DBF_DOUBLE` records
  where the axis directions are `stringout`s. Keeping the two mappings from
  drifting is the point of writing them together: `set_directions()` looks up
  both before writing either and refuses a name known to only one, and
  `HklPvSync.values()` reports it as a problem rather than publishing half a
  geometry. Add a convention to one and it must go in the other.

  **Four PV names in the original request were wrong**, confirmed against the
  live IOC and corrected here: the prefix is `6idb1:`, not `6id1:`; the pixel
  directions are `DetectorSetup:PixelDirection1`/`2` with a **colon**, not a
  dot; and `PixelDirection1` was listed twice where the second is
  `PixelDirection2`.
- **`keithley.py`** — `Keithley2400` (prefix `6idb1:K24K:`): `inp` (programmed voltage/current + ranges) and `meas` (sensed voltage/current, `sense_function`) sub-devices, plus `source_function`. Not labeled `"baseline"` — `meas.voltage` reads the EPICS UDF sentinel `9.91e37` whenever the sense function is not voltage. Ported from `bluesky/instrument/devices/keith2400.py`.
- **`lakeshore_controllers.py`** — `LS340Device` for Lakeshore 340 temperature controller (currently disabled in devices.yml).
- **`lambda_detector.py`** — `Lambda250kDetector` area detector with HDF5, ROI (1–4), and stats (1–5) plugins. **Enabled** in `devices.yml` with labels `["detector", "detectors"]` (no `"baseline"` — area detectors require `stage()` before reading and must not be in the baseline stream). Implements the `CountersClass` interface: `plot_options` returns `["Stats1"…"Stats5"]`; `select_plot(channels)` sets `Kind.hinted` on selected stats; `plot_signals` maps those names to the `statsN.total` signals for the GUI's Detectors tab. Call `configure_lambda(lambda250k)` after enabling to wire up ROI/stats ports and set default kinds.

  **`stats5` is the whole frame; `stats1`–`stats4` are not.** `configure_lambda`
  points stats1–4 at `ROI1`–`ROI4` and stats5 at `PROC1`, so
  `lambda250k.stats5.max_value` is the brightest pixel on the detector and the
  others are per-ROI maxima. `max_value`/`min_value` are already in every event
  — `default_kinds()` adds them to each stats plugin's `read_attrs` — but
  nothing *computes* them unless the plugin is enabled and asked to, so
  `configure_lambda` now also sets `enable` and `compute_statistics`. A stats
  plugin that is merely connected reports a stale zero. `auto_attenuation`
  watches `stats5.max_value`, which is what made this matter; note that stats5
  sees the image *after* `PROC1`, so check that plugin's clipping is off before
  trusting the number (high clipping caps `max_value` and the "too bright"
  branch can then never fire).

  **`setup_images()` and `save_image_flag` do not exist on this class**, despite
  `_setup_paths` (`local_scans.py:165-167`) probing for them with `getattr`. The
  probe therefore always fails and the Lambda is skipped: no per-scan HDF5 path
  is configured for it and no NeXus `ExternalLink` is built. That is worth
  knowing before adding them — `auto_attenuation` drops rejected events, and
  every dropped event still triggered the detector, so a frame and its datum
  exist with no event pointing at them. Today that is only stray files, because
  the accepted event still references its own datum; the day the NeXus writer
  starts assuming frame index equals event index, it stops being harmless.

### Key configuration files (`src/id6_b/configs/`)

- **`iconfig.yml`** — master instrument config: databroker catalog name (`6idb`), metadata defaults, SPEC/NeXus enable flags, BEC settings, DM_SETUP_FILE path. Contains `AREA_DETECTOR: HDF5_FILE_TEMPLATE: "%s/%s_%05d"` used by `local_scans` to build per-scan output file paths. Also `GUI: AUTO_SETUP:`, the session defaults the GUI applies at startup — see `gui/session_setup.py`.
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

  **`Kind` is not stored here.** `plot_names` and `selected_plot_detectors` are
  derived on every access from `det.hints["fields"]`, i.e. read straight off the
  ophyd signals; the only state the class holds is `_dets`, `_mon` and
  `_extra_devices`. That makes `Kind` a single store with **two writers** —
  `select_plot_channels()`, which sets `Kind` *and* `_dets` together, and the
  Detectors tab's `_gui_set_kinds()`, which historically set only `Kind`.

  **`sync_selection()` is what keeps the pair from drifting.** Un-hint every
  channel of a detector from the GUI and it stayed in `detectors` — still
  triggered and read at every point — while dropping out of
  `selected_plot_detectors`, which is the list `local_scans._setup_detectors`
  validates the monitor against, so a monitor-counts scan that should have been
  refused would pass. `sync_selection()` re-derives `_dets` from what is hinted
  now and is called by `_gui_set_kinds` after a successful apply. A **scaler is
  kept whatever its channels do**, matching `select_plot_channels`: it carries
  the time channel. A detector whose `hints` raise is *kept*, not dropped —
  losing a detector out of a scan silently is worse than counting one too
  many.

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

- **`hkl_utils_pete.py`** — the spec-like console API for hklpy2 (`wh`, `ca`,
  `br`, `ubr`, `setmode`, `setaz`, `setor0/1`, `compute_UB`, …). `startup.py`
  star-imports it at step 10, so the whole API is in the session namespace —
  see that step for the three import-time side effects that buys.
  `gui/hkl_bridge.py` still deliberately **mirrors** its call sequences
  rather than importing it: the module builds its own `RunEngine` at module
  scope, and the bridge must not acquire one. Its imports are grouped in a
  `try` and it predates the repo's Ruff rules, so it carries ~40 legacy lint
  errors; leave the old code as it is and keep new additions clean rather than
  reformatting the file.

  **Configuration files** — `write_diffractometer_config_file(filename,
  overwrite)`, `read_diffractometer_config_file()` and
  `read_diffractometer_config_scan(scan_id, diffractometer, clear)`, ported
  from `polar-bits`' `id4_common/utils/hkl_utils.py`. The first is
  `Diffractometer.export()` behind a name rule and an overwrite prompt; the
  second lists `*_6idb_config.yml` in the cwd and restores the chosen one; the
  third restores the orientation `ConfigurationRunWrapper` saved in a previous
  run, via `hklpy2.run_utils.get_run_orientation(cat[scan_id])`. Both readers
  end by recomputing UB from the restored reflections rather than trusting the
  stored matrix — `_recompute_ub(diffractometer)`, shared so the two cannot
  drift. Five things differ from the POLAR original:

  - **`CONFIG_SUFFIX` / `CONFIG_COMMENT` are module constants**
    (`_6idb_config.yml`, `"6-ID-B beamline"`), replacing POLAR's hard-coded
    `_polar_config.yml` and `"4-ID-G POLAR beamline"`. The suffix is what makes
    a file recognisable to the reader, so it is stated once.
  - **`cat` comes through the module, not a from-import**
    (`from . import run_engine as _re_module`), the indirection
    `utils/run_engine.py` documents: this module can be imported before or
    after `startup.py` fills the catalog in, and a from-import would freeze it
    at `None`. A missing catalog raises a sentence naming the startup rather
    than `TypeError: 'NoneType' is not subscriptable`.
  - **`_psi_geometry()` replaces `oregistry.find(name + "_psi")`**, which
    cannot work here: 6-ID-B registers a single `psic_psi` on the real motors,
    so `psic_sim` has no `psic_sim_psi` twin and the lookup would raise on the
    simulator — the more common case. It resolves `geometries.psi` and warns
    instead of raising, serving the original's purpose (surface a missing psi
    geometry at restore time rather than later inside `compute_UB()`).
  - **`read_diffractometer_config_scan` recomputes UB on the diffractometer it
    restored**, not on the current one. The original calls bare `compute_UB()`,
    which goes through `get_diffractometer()` — correct only while the
    `diffractometer=` argument is left at its default, and silently wrong the
    moment it is passed.
  - **The recompute is guarded on the orienting pair existing.** POLAR's
    `compute_UB()` indexes `sample.reflections.order[0]` and `[1]`
    unconditionally, so restoring a configuration whose active sample has fewer
    than two reflections raises `IndexError: list index out of range` *after*
    the restore has already happened. That is not an edge case: a lattice is
    routinely entered before the first peak is found, hklpy2 stores a default
    UB (2πB) for such a sample, and `restore(restore_samples=True)` makes it
    the active one — which is exactly what happened on the first real use here
    (`test123`, a=3 b=4 c=5, zero reflections). `_recompute_ub` keeps the
    restored UB in that case and says so, naming the sample and the count.

  **Three non-interactive functions sit under them**, and are what the GUI
  calls: `export_diffractometer_config(diffractometer, path)`,
  `apply_diffractometer_config(diffractometer, config, clear=True)` (a path
  *or* a dict) and `scan_diffractometer_configs(scan_id)`. The extraction
  happened when the HKL tab grew Save/Load buttons: all three public functions
  block on `input()` and the GUI kernel runs with `allow_stdin=False`
  (`gui/kernel.py:612-617`), so a Qt callback reaching them would hang the
  kernel. The point of extracting rather than reimplementing is that the
  `restore()` keyword arguments, the `_psi_geometry()` check and the guarded UB
  recompute keep **one owner** — the `IndexError` fixed in `_recompute_ub`
  would otherwise have had to be fixed twice. The three public functions keep
  their prompts and their printed listings and now end by calling these; only
  the tail moved.

  `restore()` is called with `restore_samples`/`restore_extras`/
  `restore_constraints` all explicitly `True`, because hklpy2 defaults them to
  False on hardware-backed diffractometers. Wavelength and mode are
  deliberately left alone, so restoring a configuration cannot silently
  retarget motors.

### Plans (`src/id6_b/plans/`)

- **`sim_plans.py`** — simulation-only plans for testing (`sim_count_plan`, `sim_rel_scan_plan`, `sim_print_plan`)
- **`dm_plans.py`** — APS Data Management workflow integration (`dm_submit_workflow_job`, `dm_list_processing_jobs`)
- **`local_preprocessors.py`** — Plan decorators used by `local_scans`:
  - `configure_counts_decorator(detectors, time)` — sets `preset_monitor` on all detectors; restores originals on exit
  - `extra_devices_decorator(extras)` — temporarily demotes extra devices from `Kind.hinted` to `Kind.normal` so they don't appear in BEC plots
- **`local_scans.py`** — Main user-facing scan plans. All plans configure the NeXus writer, collect extra devices (undulator energies when scanning energy, diffractometer when scanning HKL), and write rich run-start metadata. Available plans: `count`, `ascan`, `lup`, `grid_scan`, `rel_grid_scan`, `mv`, `mvr`, `abs_set`. All support `fixq=True` to hold HKL position constant (useful for energy scans). No dichro, lock-in, phase plate, or qxscan support.

  There are now **two** things that can put a custom step on a scan, and they
  compose: `fixq` needs a `per_step` (`one_local_step`, which moves back to the
  stored hkl before reading), and `auto_attenuation` needs a `take_reading`
  (which retakes the point at a different transmission). They are separate
  hooks precisely so neither has to know about the other — `one_local_step`'s
  `take_reading` default is `attenuated_trigger_and_read`, which falls through
  to `trigger_and_read` when attenuation is off. So the `per_step` selection in
  `ascan`/`grid_scan` is `if per_step is None and (fixq or auto_atten.ready)`,
  and `count` — which has no `per_step` — gets `one_local_shot` as its
  `per_shot` instead.

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

- **`auto_attenuation.py`** — threshold-driven filter transmission control.
  Watches one detector channel at each scan point; above `high` the filters
  close a step and the point is retaken, below `low` they open a step. A point
  that cannot be brought into range is accepted as it stands and the scan moves
  on — covering all three dead ends: already at `max_transmission` and still
  too dim, already at `min_transmission` and still too bright, and the IOC
  unable to realize a further step. **Off until `attenuation_setup()` is
  called**, and while off the plans behave exactly as they did before it
  existed (`auto_atten.ready` is False → `per_step` stays `None`,
  `_collect_extras` adds nothing, `one_local_step` falls through).

```python
attenuation_setup(
    signal=lambda250k.stats5.max_value,          # brightest pixel, whole frame
    low=200, high=3000,
    counter_signal=lambda250k.stats5.array_counter,
    factor=10,                                    # or AUTO (default): calculated
)
auto_atten.enabled = False                        # off, settings kept
```

  **Rejected readings are `drop`ped, not saved**, which is the whole reason
  this is a `take_reading` hook built from `create`/`read`/`drop`/`save` rather
  than a loop around `trigger_and_read`: the latter saves the event before
  anyone can look at it, so every rejected point would land in the primary
  stream, spiking the scan plot and poisoning `cen()`. The decision is taken
  *inside* the bundle but from the reading alone — `save()` or `drop()` first,
  move the filters afterwards.

  **The transmission is recorded at every point.** `_collect_extras` appends
  the filter bank when the mechanism is armed, so the descriptor is identical
  for every point (not just the retaken ones) and the events carry the
  transmission they were taken at. Without it the points are on different
  scales with nothing to renormalize by. Nothing divides by it automatically —
  that is still the analysis's job.

  **`mv` on the transmission would deadlock the RunEngine**, hence the
  `_WriteOnly` shim. `FilterBank.transmission` is an `EpicsSignal` with a
  separate `write_pv` and no `put_complete`, so `EpicsSignal.set` falls through
  to `Signal._set_and_wait`, which polls the *readback* until it equals the
  setpoint — and `EpicsSignalBase.__default_write_timeout` is `None`, "wait
  forever". The readback is the transmission the IOC actually realized out of a
  discrete absorber set, which generally is not what was asked for, so the
  first unrealizable step would hang the scan — precisely the case this module
  exists to handle. The shim puts and reports done, leaving "did it get there"
  to the explicit re-read after `settle`, which is also what drives the
  stuck-filter detection. `put_complete=True` on the component would be the
  tidier fix and would help every other caller, but it depends on the IOC
  supporting put completion on that PV and wants testing against the real
  filter bank first.

  Three more details worth keeping:
  - **`counter_signal` is not optional in practice for an area detector.** AD
    plugin callbacks are asynchronous, and the only thing otherwise making the
    stats match the frame just taken is `MySingleTrigger._acquire_changed`
    sleeping `delay_time` (0.1 s). A stale value is a cosmetic blemish on an
    ordinary scan, but here it *drives a decision* and can send the loop the
    wrong way. `_wait_for_new_frame` watches the plugin's `array_counter` and
    warns rather than hanging if it never advances.
  - **`factor` is the step size, and `AUTO` is the default.** A number
    divides/multiplies the transmission by exactly that each adjustment, so
    `factor=10` walks 1 → 0.1 → 0.01; `AUTO` works each step out from how far
    off the reading is and lands inside the window in one step when the
    signal is near-linear in transmission (2 exposures against 7 for
    `factor=10` from 1e9). A fixed step is more predictable when the response
    is *not* linear — dead time, which is often exactly why you are
    attenuating. `attenuation_setup` uses an `_UNSET` sentinel rather than
    `None` for "argument not given", so `factor=None` can mean AUTO instead
    of "leave the current setting alone"; without that the calculated mode
    was unreachable through the setup macro.
  - **`move_timeout` is what makes the retake loop actually iterate.** The
    loop keeps adjusting until the reading is inside the window, and the only
    early exits are `max_tries` and a filter bank that cannot step further.
    Deciding "cannot step further" from a blind `settle` sleep conflates it
    with "has not caught up yet": a filter bank slower than `settle` looks
    stuck on the first adjustment, so the point is accepted still far out of
    range after a single retake. `_wait_for_transmission` instead polls the
    readback until it *moves*, and only calls it stuck once `move_timeout`
    expires. `settle` is now a minimum dwell, not the whole wait.
  - **`attenuation_setup` is atomic.** The rules are about combinations
    (`low` against `high`, `min` against `max`), so validation has to run
    after the writes — which means a refusal would otherwise leave half the
    new settings in place, and a rejected `low`/`high` pair would stay behind
    to fail every later call, *including the one trying to correct it*. It
    snapshots `__dict__`, applies, validates, and rolls back on any
    exception.
  - **A hot pixel defeats the whole thing.** `stats5.max_value` is by
    definition the single worst pixel on the detector, and a stuck-high one
    makes every point read "too bright", so the loop attenuates to
    `min_transmission` and moves on — a whole scan quietly attenuated to
    nothing. Take a blank frame and check `max_value` is at the noise floor
    before arming it.

- **`pva_streaming.py`** — start and stop the PVA streaming data cache around a
  scan. Writes the directory and file name, sets `ScanOn` to 1 before the scan,
  and clears it a second later after. **Off until `pva_streaming_setup()` is
  called**, and while off the plans behave exactly as they did before it
  existed (`pva_stream.ready` is False → the wrapper yields the plan unchanged
  and `pva_metadata()` returns `{}`).

```python
experiment_setup("~/6idb-bits", sample="Fe3O4", base_name="scan")
pva_streaming_setup()                 # arm
RE(ascan(psic.eta, -1, 1, 51, 1.0))
#   FilePath -> ~/6idb-bits/Fe3O4
#   FileName -> pva_scan_00042.h5
#   ScanOn   -> 1 ... scan ... 1 s ... ScanOn -> 0

pva_stream.enabled = False            # off, settings kept
pva_stream                            # settings, and the next file name
```

  Wired into `count`, `ascan` and `grid_scan` as the **outermost** decorator, so
  the cache is on before `subs_decorator` opens the NeXus writer and off after
  it closes; `lup` and `rel_grid_scan` need nothing, since they delegate.

  Five details worth keeping:

  - **The flag is cleared from a `finalize_wrapper`.** A scan that is Ctrl-C'd,
    hits a soft limit or raises would otherwise leave the cache collecting
    forever, filling a disk with a file nobody is going to close. Tested against
    a real RunEngine for all three exits — clean, exception, and pause-then-
    abort — and `ScanOn` ends at 0 in each.
  - **The 1 s delay is `bps.sleep`, not `time.sleep`.** A plan message is
    interruptible and shows in the RunEngine's own timing; a process sleep
    blocks the whole thread including the Ctrl-C handler. `stop_delay` is the
    knob if the cache turns out to need longer than a second to flush.
  - **`_WriteOnly` again, not plain `bps.mv`.** Same shim, same reason as
    `auto_attenuation` — these three PVs are not IOC records, so whether a write
    echoes back byte for byte is unknown, and `EpicsSignal.set()` polls the
    readback until it matches with `write_timeout=None`, i.e. forever. A path
    the server normalises or truncates would hang the RunEngine on the *first*
    scan. Imported from `auto_attenuation` rather than copied, so there stays
    one copy of that workaround.
  - **Paths are abbreviated to `~`** (`use_tilde`, on by default), because the
    PV holds 39 characters and the full prefix eats 22 of them. **Whatever
    consumes the PV therefore has to expand `~` itself.** This is a stopgap, not
    the fix — `_check_length()` logs a warning and `repr(pva_stream)` prints a
    `!` line whenever a value still overflows, so the truncation cannot happen
    unremarked.
  - **`_home_relative()` resolves both sides before comparing.** `$HOME` here is
    `/home/beams/USER6IDB`, an alias for the `/home/beams18/USER6IDB` that
    experiment paths are built from, so `p.is_relative_to(Path.home())` is
    `False` and a naive check would silently never abbreviate anything —
    producing exactly the truncation it exists to avoid. With both sides
    `.resolve()`d it is `True`.

  The scan id comes from `RE.md["scan_id"] + 1`, the same expression
  `local_scans._setup_paths` uses: the counter is bumped when the run opens,
  which has not happened yet when the wrapper and the metadata hook run.
  `pva_metadata()` puts the resulting `file_path`/`file_name` in the run start
  document, which is the only durable record linking a scan to its cache file.

- **`hkl_pv_sync.py`** — keep the `6idb1:` HKL-conversion PVs matching the
  live session. The session is the source: `psic.sample.UB` is flattened
  row-major into `spec:UB_matrix:Value`, `lambda250k.xcenter`/`ycenter` go to
  `CenterChannelPixel`, and the eleven convention PVs — eight axis sign
  strings plus the three beam-direction components — get whichever convention
  is selected. Same singleton-plus-`*_setup()` shape as `pva_streaming` and
  `auto_attenuation`, including the atomic setup that snapshots `__dict__`,
  applies, validates and rolls back.

```python
hkl_pv                                    # settings, and what the PVs hold
hkl_pv_setup(diffractometer="psic_sim")   # publish the simulator instead
hkl_pv_setup(convention="spec")           # the ad_hoc geometry's directions
hkl_pv.enabled = False                    # stop writing, settings kept
hkl_pv.push()                             # write once, by hand
```

  **On by default** (`enabled=True`), unlike the other two: this only
  publishes, it does not change what a scan does.

  There are two writers, and both are needed. A **1 Hz daemon-thread watcher**
  (`start()`/`stop()`) is what covers everything: UB changes in nine
  kernel-side `_gui_hkl_*` helpers, in every MCP op, and across the whole
  `utils/hkl_utils_pete.py` console API, so explicit hooks would be both
  numerous and permanently incomplete — a poll reaches the console paths no
  GUI-side hook could. And `hkl_pv_decorator`, on `count`/`ascan`/`grid_scan`
  as the **outermost** decorator, pushes once as the scan opens, so the values
  are right for the data being taken even with the watcher stopped.

  Five details worth keeping:

  - **Comparison is against the PV readback, not a cached last-write.** That
    is what makes the PVs a genuine mirror rather than a write log: a stray
    `caput` is corrected on the next tick. A disconnected, empty or
    wrong-length waveform counts as different, so the first push after the IOC
    comes back writes rather than skips.
  - **The watcher's loop pushes first and waits after**, so `hkl_pv.start()`
    at the tail of `startup.py` publishes immediately rather than a second
    later, and `interval` is read each time round so a change takes effect on
    the next tick.
  - **Every tick is wrapped, with rate-limited logging.** A disconnected PV
    would otherwise write to the log once a second for the length of an
    experiment; the message is logged when it *changes*, and recovery is
    logged once.
  - **`hkl_pv_wrapper` never lets a push kill a scan** — the failure is logged
    and the plan runs. A bookkeeping PV is not worth an aborted run.
  - **`DetectorSetup:Distance` is exposed but not synced**, because nothing on
    the Bluesky side sources it. Set it directly
    (`hkl_pvs.distance.put(400.644)`); `repr(hkl_pv)` reports what it holds
    and says it is not synced, rather than inventing a source.

  **`xcenter`/`ycenter` are plain Python ints on the detector class**
  (`lambda_detector.py:169-170`), not signals, so they are 256 in a fresh
  session regardless of what the PV held — and the first push of a session
  therefore overwrites the PV with 256. Set the session's value first with
  `lambda250k.image_cen(x, y)`. Any future area detector carrying those two
  attribute names works by passing `detector=` to `hkl_pv_setup`.

  Not included, and additive on top of `_gui_*` wrappers over `hkl_pv_setup`
  when wanted: a Detectors-tab group and MCP tools, in the shape of
  `gui/pva_bridge.py` and `gui/atten_bridge.py`.

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

- **`spec_data_file_writer.py`** — SPEC-format data file output (enabled in
  `iconfig.yml`). `spec_file_name(title)` is the naming rule on its own —
  `MM_DD_<cleanupText(title)>.<ext>` — extracted from `newSpecFile` because the
  GUI's Session tab has to *show* the resolved name before anyone presses the
  button, and duplicating the rule would give it two owners.

  **The scan counter is one behind, and `scan_id=True` is what resets it.** The
  next scan is `RE.md["scan_id"] + 1` (`bluesky/run_engine.py:209`), so "the
  next scan is #1" needs the counter at **0**. `newSpecFile` computes
  `scan_id or 1`, and `True or 1` is `True`, which
  `SpecWriterCallback2.newfile` maps to `SCAN_ID_RESET_VALUE` — which is 0.
  Passing `scan_id=1` would give a first scan numbered 2. On a file that
  already holds scans, `newfile`'s `scan_id = max(scan_id or 0, highest)` runs
  first and wins, so the counter follows the file and the reset cannot happen —
  forcing it would put duplicate `#S` numbers in one file, which is corrupt for
  spec2nexus. `newSpecFile` logs a warning and appends instead.
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
- **`session_setup.py`** — `SESSION_SETUP_CODE`, the last string appended to the
  bootstrap cell. `experiment_setup()` and `counters()` have to be answered
  before the first scan of every session and the answers are nearly always the
  same, so it applies them from `iconfig.yml`'s `GUI: AUTO_SETUP:` block:
  `EXPERIMENT` (base path, sample, file base name — an empty `BASE_PATH` means
  the directory the GUI was started in) and `COUNTERS` (detector channels and
  monitor). **GUI only** — the plain IPython workflow and the queue server never
  see this module and still prompt.

  `COUNTERS` takes **either channel names or the row indices** `counters()`
  prints. Names are the documented preference: a renamed channel fails loudly
  and lists what is available, while a row index that has shifted — one more
  scaler channel, `lambda250k` absent — would quietly select the *wrong*
  detector. The rows are resolved and checked here rather than passed straight
  through, because `plotselect()` falls back to `input()` on a bad argument and
  stdin is closed in this kernel, so an invalid index would hang the tail of the
  bootstrap instead of reporting itself.

  Read kernel-side (`iconfig` is already in the namespace after
  `from id6_b.startup import *`), so the GUI process neither finds nor parses
  the file a second time. `_gui_auto_setup()` cannot raise: this runs at the
  tail of the bootstrap cell, and a session that is merely un-configured must
  not look like one that failed to start. It is appended **after**
  *follow_up_code* so a bad block cannot cost the live-plot subscription, and so
  its summary is what the console is left showing once the ~40 s device-loading
  log has scrolled past.
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

  **Pop out** puts the plot in its own window, so it stays in view while
  another tab is on top — the request that prompted it. It is a **move, not a
  copy**: everything the plot is made of lives in one child widget
  (`self._body`) that is reparented into a `_PlotWindow` and back, so there is
  one canvas, one `_series` and one selector panel either way. A mirror — a
  second `ScanPlotTab` fed the same documents — was the alternative and was
  rejected on two counts: the state would be duplicated and have to be kept in
  step, and a window opened part-way through a scan would start blank unless
  the run's documents were buffered and replayed (a 200×200 grid scan is
  ~40 000 events to hold). Reparenting has neither problem, and it needs no
  change in `app.py`: the tab stays the one thing on the dispatch list whether
  the plot is showing inside it or not.

  Four details. The window is **parented to `self.window()`** with the
  `Qt.Window` flag rather than left parentless — a parentless top-level widget
  keeps the Qt application alive, so closing the session window would leave the
  process running with an orphaned plot; the parent is resolved at pop-out
  time, since in `__init__` the tab is not yet inside the session window.
  Closing the window **reattaches** rather than losing the plot
  (`_PlotWindow.closing` → `_reattach`), and `_reattach` clears `self._window`
  **first**, which is what makes the re-entrant call from its own `close()` a
  no-op. The tab shows a notice with its own *Bring it back here* button in
  place of the plot, so a blank tab is never a mystery. Geometry is remembered
  in `QSettings("APS", "id6b-gui")` under `scanplot/window_geometry`.
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

  **"Unchanged" is measured against `_options["detectors_selected"]`, so that
  snapshot has to be current.** It used to be re-read only on the *first* idle
  poll and on `stop` documents, so a `counters()` typed in the console did not
  reach the tab until the next scan *finished* — and the omit rule was then
  comparing the boxes against a stale default, which is what decides whether
  the plan is handed an explicit detector list at all. `on_kernel_state` now
  refreshes on **every** busy→idle edge; a console command is exactly such an
  edge, and `_gui_scan_options()` is an in-process walk of `oregistry` that the
  tab already ran after every scan.

  **A refresh keeps the boxes as the user left them** unless
  `detectors_selected` itself changed between the two replies. Refreshing an
  order of magnitude more often would otherwise wipe a deliberate deviation
  every time anything was typed in the console. When counters really did
  change, the tab follows counters — the source of truth moved, so showing it
  is the right answer.

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
  (h k l, six angles, 2θ, ψ, ψ reference, λ/energy), an hkl → angles
  calculator with a Move button, and a Configuration group that saves and
  restores the whole orientation. A selector chooses `psic` (red "real motors"
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

  **The ψ readout comes from a second geometry, and it needs the whole sample.**
  `_gui_hkl_derived` reads ψ off `psic_psi` (the `psi` engine on the same
  motors) and 2θ off `psic_q`. 2θ is `4π sin θ / λ` from the angles alone, so
  `psic_q` needs nothing; ψ is measured against `UB · (h2, k2, l2)`, and hklpy2
  pushes *lattice and UB together* to the solver, so a `psic_psi` left on its
  default 1 Å cubic lattice answers against the wrong reference vector.
  Copying UB alone — what the code did — happens to give the right ψ for a
  cubic sample with an on-axis reference, and a wrong one otherwise: on
  a=b=3, c=12, γ=120 with the reference at (1, 0, 1), −90° instead of −37.8°.
  `_gui_hkl_mirror_sample(src, dst)` copies both, and three things about it are
  deliberate. **Lattice first**, because a lattice write flags
  `_SolverDirty.SAMPLE | _SolverDirty.UB` and hklpy2's own comment warns that
  some backends discard U/UB as a side effect. **Parameter by parameter**,
  because `Sample.lattice`'s setter rebinds `_on_change` on whatever object it
  is handed, so assigning the source's `Lattice` would redirect the *source*
  sample's change notification. And **each half guarded by an equality check**,
  because this runs on the 1 Hz poll and every assignment flags the solver
  dirty; verified to write nothing on a repeat poll and to propagate on the
  next poll after a real lattice change.

  Note `devices.yml` binds `psic_psi` to `psic`'s *real* motors, so the ψ shown
  while the tab is displaying `psic_sim` is derived from the real machine's
  angles. Pre-existing, and not something the mirror changes.

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

  **ψ is in this block too, and it is not a constant axis.** A `psi_constant_*`
  mode holds ψ fixed exactly as surely as it holds `mu` and `nu`, but ψ is a
  member of `core.extras` — a different hklpy2 concept — and never appears in
  `constant_axis_names`, so a `constant_axis_names`-only panel showed `mu` and
  `nu` and silently omitted the one value the mode is named after.
  `_gui_hkl_state` therefore also returns **`extra_axes`**, the extras that are
  not the ψ reference (`h2/k2/l2` have their own boxes), and
  `_rebuild_preset_rows(axes, extras)` renders those as rows in the same grid.
  They get **no Fix check box** — `(always)` in place of one — because an extra
  has no "follow the motor" fallback to untick: a mode that defines an extra
  always uses it.

  Which is why **the Calculate row no longer has its own ψ box**. ψ used to
  live in both places, and they disagreed: typing ψ under Fixed angles started
  the 400 ms debounce, and pressing Calculate immediately after sent the
  Calculate row's stale value instead. There is now one ψ widget, in the Mode
  group, and `_calculate()` reads it out of `_extra_values()`.

  Writes are debounced 400 ms (`_presets_timer`), a clone of the ψ reference
  boxes' `_reference_timer`, and go through `_gui_hkl_set_fixed` — presets and
  extras in **one** call, because the tab shows them in one block and applies
  them from one timer, and two calls would race for the same reply slot so only
  one message would survive. Note the asymmetry between the two setters:
  hklpy2's `presets` setter drops an unknown axis silently, while its `extras`
  setter **raises `ConfigurationError`**, so `_gui_hkl_set_extras` filters
  against `list(dev.core.extras)` before writing. It also relies on
  `_extras.update()` merging, which is what lets ψ be written without
  disturbing `h2/k2/l2`. Three more details that matter:
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

  The **Calculate and move** row caps its h/k/l boxes at `VALUE_WIDTH` and ends
  in `addStretch(1)`, the `QHBoxLayout` counterpart of the explicit column
  stretches `tabs/scan.py` documents: with every item left at the default
  stretch Qt shared the row's surplus width out over all of them, so each
  one-character label was stretched to ~107 px and `h` sat an inch from its own
  value box.

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

  **The Configuration group** is the last section of the stack: one button to
  write the current orientation to a `*_6idb_config.yml` file, and a load path
  with two sources — a file on disk, or the orientation
  `ConfigurationRunWrapper` saved inside a previous scan. It exists because an
  orientation costs real beam time to build and could previously only be saved
  or restored by typing one of three console functions, none of which is
  discoverable from the GUI and **none of which can be called from it** (they
  block on `input()`). Backed by `gui/hkl_config_bridge.py`.

  ```
  Configuration
    Save the current orientation:  [ Save to file… ]
    Load from:  (•) File   ( ) Previous scan          [ Refresh ]
    ┌──────────────────────────────────────────────────────────┐
    │ si_6idb_config.yml     Si — 2 reflection(s)   09-04 14:22│
    └──────────────────────────────────────────────────────────┘
    (•) Replace everything   ( ) Add to what's there
    [ Load selected ]  [ Browse… ]
    status line
  ```

  It stays in this file rather than becoming its own widget module: unlike
  `Diffract3DPanel` it is not a pure view — it needs the tab's `device`,
  `_kernel_idle`, `request()`, `run_in_console()` and `refresh()`.

  - **One `QTableWidget` shared by both sources**, its columns rebuilt when
    the source radio changes — fewer widgets than two tables, and the left
    pane is already narrow (a 3:2 splitter against the 3D panel). Column 1
    (Contents) stretches; the other two size to their contents, with
    `setStretchLastSection(False)` so the last one does not absorb the width.
  - **A reply for the source that is no longer selected is discarded.**
    `_show_config_files` / `_show_config_scans` each return early unless
    `_config_source()` still names them: the listing is a one-off request and
    the radio can change while it is in flight.
  - **Listings are one-off requests, never polled** — a directory walk plus a
    YAML parse per file, and ~40 ms per catalog run, is what `tabs/status.py`
    describes as not something to do once a second for a group nobody is
    looking at. `_configs_listed` fires the first one when the kernel first
    reports idle, and is cleared by the Refresh button, by a change of source
    and by a change of diffractometer.
  - **Replace vs merge is an explicit radio pair**, mirroring the console's
    `[o]verwrite/[a]ppend` prompt, with the choice repeated in the
    confirmation dialog. Replace is the default, matching hklpy2's own
    `restore()` default.
  - **The confirmation says `REAL DIFFRACTOMETER.`, not `REAL MOTORS WILL
    MOVE.`** — nothing moves on a restore. What it does change is where every
    later move goes, which is worth its own warning rather than a borrowed
    one. The body also states that UB is recomputed from the restored
    reflections rather than taken from the saved matrix, and that the
    wavelength and mode are left alone.
  - **Load and Save go through the console**, not `_act()`: restoring an
    orientation reframes everything the tab shows, so it belongs in the
    history and the transcript — the reasoning behind Move and the Session
    tab's New data file.
  - **The post-console reload is a self-re-arming `QTimer`**, not the idle
    edge. `run_in_console` is fire-and-forget and `kernel_state_changed` fires
    only on *transitions*, so a command that starts and finishes between two
    1 Hz polls produces none at all; `_reload_after_console` re-arms itself
    while the kernel is busy and then calls `refresh()` and relists.
  - **The Save dialog appends `CONFIG_SUFFIX` and then asks again.** The
    console's writer appends the suffix to whatever base name it is given and
    the reader lists only files carrying it, so a file without it would be
    written and then never offered back. Qt's own overwrite prompt has
    already been answered about the name that was *typed*, not the one with
    the suffix on it, hence a second `QMessageBox.question` when the appended
    path exists.
  - **File dialogs are seeded from the polled `"cwd"`, never
    `BaseTab.session_cwd`**, which is the directory the kernel was *launched*
    in and goes stale the moment anything chdirs — and `_gui_new_spec_file`
    chdirs. The last-used directory is remembered in
    `QSettings("APS", "id6b-gui")` under `hklconfig/directory`, the
    `macro.py` pattern (`type=str` read, `is_dir()` validation, store the
    chosen file's *parent*).
  - **`_update_enabled()` is called at the end of `__init__`.** Everything
    here is gated on `editable = self._kernel_idle and bool(self._state)`,
    but nothing called it before the first poll answered, so the controls
    were live for that second — harmless for a combo box, not for Save to
    file. Load additionally needs a selected row, and the reason it is
    disabled is in its tooltip rather than discovered by pressing it.
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

  The tab also carries the **Automatic attenuation** group — watched channel,
  accept window, step factor, max retakes and frame counter, with an Apply
  gated on kernel idle. It belongs here rather than on the Scan tab for the
  same reason the `Kind` tree does: it is kernel-side configuration that
  changes what *future* scans do, and the channel it watches is one of the
  detector channels listed directly above it.

  Two details differ from the rest of the tab. The Apply goes out as a polled
  **expression**, not through `execute_once()`, because `_gui_atten_set`
  *returns* the new state — or a dict with `error` when it refuses — and
  `execute_once` discards the value, which would make a rejected setting look
  as though it had been applied; the reply lands under the same key as the
  ordinary read, so one code path renders both. And the value widgets are
  filled from the kernel **once** (`_atten_loaded`), because they sit on the
  1 Hz poll and rewriting them every reply would pull the text out from under
  whoever is typing. `_show_attenuation` separates "module missing"
  (`available: False`) from "apply refused" (`error` alone): only the latter
  latches `_atten_loaded`, since the former can arrive before the session is
  up and would otherwise stop the fields ever being populated.

  Below it sits the **PVA streaming cache** group — armed flag, file-name
  format, stop delay, the `~` abbreviation, the next file that will be written
  and a **live readback of the three PVs** — on the same Apply-when-idle rule,
  and here for the same reason: it changes what future scans do. Arming
  mid-scan would name the cache file for a scan already under way, which is why
  it waits for idle rather than merely for the kernel to answer.

  It follows every convention the attenuation group established (Apply as a
  polled *expression* so a refusal cannot read as success, once-only field fill
  via `_pva_loaded`, `available: False` not latching while an `error` does), so
  only what is new is worth recording:

  - **The readback line exists because the truncation is silent.** The
    character counts (`path_over` / `name_over`) are a *prediction* about a
    39-character `DBF_STRING`; what the IOC kept after the last scan is the
    *result*, and it is the only thing that settles whether a path fitted.
  - **`ScanOn` is on that line for the other half of the same worry.** Any
    reply the GUI receives was evaluated while the kernel was idle, so no scan
    is running — which makes a flag reading 1 proof of a cache left collecting
    by a scan that died in a way the `finalize_wrapper` could not catch. The
    group says so in a warning rather than leaving it to be noticed as a full
    disk.
  - **`_pva_flag()` reads through `float` before `bool`.** The PV comes back as
    `0`, `0.0` or `"0"` depending on how it is served, and `bool("0")` is True
    — which would report a stuck cache on every idle poll.
  - **An empty box means "leave that setting alone"**, as in the attenuation
    group: `name_format` and `stop_delay` are omitted from the payload when
    blank, since `""` would come back as a refusal to parse a number.

  Extra devices get `Kind` control too, as `<name>   (extra)` nodes in the same
  tree. They have no `plot_signals`, so their channels come from
  `walk_signals(include_lazy=False)` and are set by dotted attribute path
  (`_gui_extra_kinds` / `_gui_set_extra_kinds`) — hence two helpers and two
  editor dicts, with `_apply` routing each set to the right one. Omitted
  signals are hidden behind a checkbox: they are not recorded anyway and on a
  motor bundle they dominate (53 of `sl1` + `filters`' 105). Setting an extra to
  `omitted` auto-ticks that checkbox, since the row would otherwise be filtered
  out of the next listing and the change could not be undone.
- **`atten_bridge.py`** — `ATTEN_HELPERS_CODE`, the kernel-side
  `_gui_atten_state()` / `_gui_atten_set()` for automatic attenuation,
  appended to the bootstrap cell next to the HKL and MCP helper strings. It
  lives in its own module because it serves **two** callers — the Detectors
  tab and the MCP dispatcher — exactly as `hkl_bridge.py` serves the HKL tab
  and the hkl ops. Both go through
  `plans.auto_attenuation.attenuation_setup`, so there is no second copy of
  the validation rules; the two `_gui_mcp_atten_*` wrappers at the bottom
  exist only to absorb the device argument `_gui_mcp_ops()` gives every op.

  `_gui_atten_channels()` builds the watch-channel list from
  `walk_signals(include_lazy=False)` over `counters.detectors`, keeping only
  what is read at every point — the test is `kind & Kind.normal`, since
  `Kind` is a flag and `hinted` includes `normal`. A `config` signal is
  recorded once per descriptor rather than per event, so it can never be what
  a per-point threshold watches.

  **That filter is load-bearing, not tidiness.** A reply comes back through
  `user_expressions` as a repr rendered by IPython's pretty printer, which
  truncates a sequence past `MAX_SEQ_LENGTH` (1000) by writing a literal
  `...` into it; `ast.literal_eval` then hands the GUI a list with an
  `Ellipsis` in the middle, and `QComboBox.addItems` raises `TypeError`. An
  unfiltered walk of `lambda250k` is over 2000 signals, so pressing Reload
  crashed the GUI and took the kernel with it. The kind filter cuts that to
  ~15, `_GUI_ATTEN_MAX_NAMES` caps it at 300 as a backstop, and
  `_show_attenuation` drops non-`str` entries before they reach `addItems` —
  three layers, because the failure mode was losing a live session.

  `_gui_atten_counter_channels()` is a **separate** list, matched on a
  trailing `array_counter`: a plugin counter is usually `config` kind, so it
  is filtered out of the watch list, but the retake loop reads it directly
  with `rd()`, for which kind is irrelevant. `_gui_atten_find_signal()`
  searches every signal for the same reason, and resolves a data key back to
  the signal *object* the loop needs.

- **`pva_bridge.py`** — `PVA_HELPERS_CODE`, the kernel-side
  `_gui_pva_state()` / `_gui_pva_set()` behind the Detectors tab's PVA
  streaming group, appended to the bootstrap cell next to the attenuation
  string. Its own module for the reason `motion.py` is one rather than more
  lines in `bridge.py`: one feature's kernel code with its own validation
  surface. It differs from `atten_bridge.py` in having **one** caller — there
  are no `_gui_mcp_pva_*` wrappers, and adding them if an MCP client ever
  wants to arm the cache is the same two functions at the bottom.

  Everything routes through `plans.pva_streaming.pva_streaming_setup`, so
  there is no second copy of the rules and the GUI is held to what the console
  is. Two checks live here rather than there, both because the alternative is
  a failure that surfaces at the wrong moment:

  - **The name format is `%`-tested at Apply**, with `fmt % ("scan", 1)`. It
    is otherwise only ever applied inside the scan wrapper, so a bad format
    would surface as a `TypeError` that kills the *first scan after it was
    set* — long after the mistake, and with the cache flag already on.
  - **`path_over` / `name_over` are computed here**, not in the GUI, so
    `MAX_STRING` has one owner.

  `_gui_pva_readback()` guards each `.get()` separately and reports `None` on
  failure: the streaming server is not always running, and a disconnected PV
  must leave the rest of the Detectors tab working. It uses `getattr(device,
  attr)` — an explicit `device.__getattr__(attr)` bypasses normal attribute
  lookup on an ophyd `Device` and returned `None` for all three.

  Note `pva_streaming_setup` ends in `print(repr(pva_stream))`, which from the
  GUI's Apply fires *inside* the `user_expressions` evaluation and so carries
  the poller's `msg_id` as parent — `_drain_iopub` filters it out. Invisible
  from the tab, still useful from the console.

- **`spec_bridge.py`** — `SPEC_HELPERS_CODE`, the kernel-side helpers behind the
  Session tab's *New data file* group, appended to the bootstrap cell next to
  the PVA string. Its own module for the same reason `pva_bridge.py` is one.

  It **imports `id6_b.callbacks.spec_data_file_writer` directly** rather than
  looking the names up in the session namespace, because `startup.py` only
  binds `newSpecFile` and `specwriter` when `SPEC_DATA_FILES: ENABLE` is true —
  and a session with the writer switched off is exactly the one that should be
  told so, rather than raising `NameError`. `_gui_spec_enabled()` reports that
  flag.

  Three functions matter. `_gui_spec_preview(title, sample=None)` is **pure** —
  the resolved SPEC path and whether it exists, its `#S` count and highest scan
  number (via `spec2nexus.spec.SpecDataFile`, the same source `newfile` uses,
  so the two cannot disagree), the cleaned base name, the experiment path the
  typed sample would put in force, the `<base>_00001_master.hdf` the next scan
  would write and whether it is free, and hence the scan number that would come
  next. `_gui_spec_state()` is what the file, base name, sample and counter are
  now. `_gui_new_spec_file(title, sample=None)` is the action: it applies the
  sample, sets `experiment.file_base_name` to the `cleanupText`-cleaned form
  **and** calls `newSpecFile(title, scan_id=True, RE=RE)`, then prints a plain
  summary.

  - **One name, both uses.** The base name is set alongside the SPEC file, not
    separately: it is what `local_scans` builds `<base>_00001_master.hdf` from,
    and a counter reset with a stale base name walks straight into
    `_setup_paths`' `FileExistsError` — which it raises whether or not the
    NeXus writer is enabled. That is why the preview checks the master path
    too.
  - **`scan_id` is read live from `RE.md`**, not from `.re_md_dict.yml`, which
    `StoredDict` writes from a background thread and can be seconds behind. A
    preview that promises a scan number has to be current.
  - **The result is read back, not assumed.** The route to counter 0 (see the
    `spec_data_file_writer.py` bullet) is subtle enough that
    `_gui_new_spec_file` re-reads `RE.md["scan_id"]` and reports the real next
    scan number, so a regression shows in the summary line rather than in the
    data.
  - **The sample travels with them, for the same reason the base name does.**
    `experiment_path` is `base_experiment_path / sample` and is the folder the
    masters go in, so a preview describing the SPEC file against the *typed*
    base name but the masters against the *old* sample would be answering half
    the question. Both fields default to what is in force — an empty one means
    "keep it" — so changing only the sample is one field and one button, and
    the SPEC file the preview then names is the one already open (the append
    branch, which is the right answer there).
  - **A sample containing a separator is refused by name**
    (`_gui_spec_sample_error`). `Path("/a") / "/etc"` is `/etc`, so an absolute
    or nested sample silently *escapes* the base path rather than failing —
    the one input here whose mistake is invisible.
  - **`experiment.change_sample()` is called when the sample changes**, and
    `experiment.file_base_name` assigned directly when it does not. That
    reverses the base-name-only rule on purpose: `change_sample` is wanted here
    for exactly the two side effects that made it wrong before — it `mkdir`s
    `<base>/<sample>` and `chdir`s to the base path, and the `chdir` must
    happen **before** `newSpecFile`, which resolves the SPEC name against the
    cwd. Its `input()` prompts (stdin is closed in this kernel) are never
    reached because both arguments are given. A failure — no base path set yet
    — records the sample anyway rather than losing the whole action over a
    folder that could not be made.

- **`hkl_config_bridge.py`** — `HKL_CONFIG_HELPERS_CODE`, the kernel-side
  helpers behind the HKL tab's *Configuration* group: save the current
  orientation to a file, and load one back from a file or from a previous
  scan. Its own module for the reason `spec_bridge.py` is one, and
  deliberately **not** appended to `hkl_bridge.py`, whose helper string ends
  `''' % {...}` so every literal `%` inside it has to be doubled.

  Everything routes through the three non-interactive functions in
  `utils/hkl_utils_pete.py` (above), so the GUI writes the file the console
  writes and restores it the way the console does. The console's own three
  functions cannot be called from here at all — they block on `input()`, and
  the GUI kernel runs with `allow_stdin=False`.

  Two reads, `_gui_hklconf_files(directory=None)` and
  `_gui_hklconf_scans(device, limit=15)`; three actions,
  `_gui_hklconf_save` / `_gui_hklconf_load_file` / `_gui_hklconf_load_scan`.
  **The actions `print()` their report and return `None`** — they go out
  through `run_in_console()`, and a return value would only add an `Out[n]`
  above the lines worth reading. `_recompute_ub`'s `!` warning ("restored, but
  UB was kept: fewer than two orienting reflections") reaches the console for
  free, since it prints its own.

  - **`hkl_utils_pete` is imported lazily, inside each helper.** Its module
    scope builds a second `RunEngine` and resolves four devices out of
    `oregistry`, so it must never be imported at GUI-process import time; in
    the kernel `startup.py` has already imported it, so the lazy import is
    free.
  - **The helpers act on the diffractometer they are given**, never on
    hklpy2's process-global `get_diffractometer()` — which is set to the real
    `psic` when `hkl_utils_pete` is imported, while the tab has its own
    `psic`/`psic_sim` selector.
  - **Scans are keyed by uid, not by scan number.** The counter is reset with
    every new SPEC file, so this catalog already holds several runs numbered
    1; a picker keyed on the number would silently resolve to the most recent
    match with nothing on screen saying which run it used.
  - **A run holding the orientation under the other name is still offered**,
    with `matches` False so the row can say so. `psic` and `psic_sim` are the
    same E6C geometry with the same axis names, so a simulator configuration
    restores onto the real device and the other way round. A run holding
    *several* is refused by name, listing them.
  - **Paths arrive absolute.** A relative one is resolved against the
    kernel's *live* working directory, which is not the one the GUI was
    launched in — `_gui_new_spec_file` chdirs — so the saver refuses one
    rather than writing somewhere nobody asked for. The file listing reports
    the `os.getcwd()` it actually used.
  - **A file that will not parse is listed with its error, not dropped.** It
    is still on disk, and saying why it cannot be offered beats leaving the
    operator to wonder where it went. One unreadable *run*, by contrast, is
    skipped — it must not cost the whole catalog listing.
  - `_GUI_HKLCONF_MAX_ROWS` (200) is the same backstop `atten_bridge.py`
    documents: IPython's pretty printer writes a literal `...` into a
    sequence past 1000 items, which reaches the GUI as an `Ellipsis` and
    raises inside Qt.

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

  The **New data file** group starts a fresh SPEC file and, with it, a fresh
  scan counter and the sample the data goes under — two fields (Sample, Base
  name), one button. Backed by `gui/spec_bridge.py`; the Files group above it
  gained a **File base name** row (`experiment.file_base_name`, a plain polled
  attribute) so what the button set is visible afterwards.

  **Both fields in one group with one button**, rather than a separate sample
  control, because the Masters line is `<base_experiment_path>/<sample>/
  <base>_00001_master.hdf` — a sample applied elsewhere would leave this
  group's preview describing a folder that is no longer the one in force. The
  preview is computed against the sample *typed*, so it cannot be stale. The
  base experiment path is deliberately **not** here: changing it also `chdir`s
  the session, a bigger act, left to `experiment_setup()` in the console.

  - **The preview is a debounced one-off request, not a polled expression.**
    400 ms after typing stops (a clone of `HklTab._presets_timer`),
    `BaseTab.request({SPEC_PREVIEW_KEY:
    f"_gui_spec_preview({title!r}, {sample!r})"})` merges into the next *idle*
    poll. It depends on what has been typed, and it walks a SPEC file with
    spec2nexus — not something to do once a second
    for a field nobody is looking at. A request made while a scan is running
    waits for idle rather than queueing behind it (`StatusPoller._once` is
    cleared only on a successful send, and `_poll_kernel` returns early while
    busy), which is also what makes the after-the-button re-preview work by
    simply restarting the timer.
  - **The action goes through the console**
    (`run_in_console(f"_gui_new_spec_file({title!r}, {sample!r})")`), not
    `execute_once()`: starting a new file reframes the whole data record, so
    it belongs in the history and the transcript — the Agent tab's Approve and
    the HKL tab's Move reasoning.
  - **Neither field is ever written from a poll reply** — it is the user's
    text and they may still be typing into it. What the poll *does* set is the
    **placeholder** (`keep jwkim` / `keep S1`), which is how "blank means keep
    what is in force" is stated without occupying the field.
  - **The first preview is primed, not typed for.** `_spec_primed` fires the
    timer once on the first reply carrying a base name, so the group opens
    showing the file already open and the sample in force rather than
    `Type a base name.` The flag is set *before* the timer starts, so a reply
    that never comes cannot loop.
  - **The scan-number row is an indicator, not a control.** There is no
    "append without resetting" option to offer, and on an existing file the
    counter follows the file whatever anyone ticks. Deliberately **not**
    `setEnabled(False)` — a disabled widget receives no events, so its
    tooltip, which is where the reason lives, would never appear. It is
    enabled and made deaf instead: `WA_TransparentForMouseEvents` stops the
    mouse, `Qt.NoFocus` stops the space bar, and a `toggled` guard
    (`_restore_reset_indicator` against `_spec_reset_state`) undoes anything
    that gets through either — `WA_TransparentForMouseEvents` blocks only
    *real* mouse events, so a synthetic `click()` still toggles it.
  - Rendering states the collisions before the button is pressed: the
    Experiment path line as `— exists` or `— will be created`, the SPEC line
    as `— new` or `— exists, N scan(s)`, the Masters line as `— free` or
    `— exists`, and a status line that says plainly `Appending. The next scan
    will be #5, not #1.` with the remedy, rather than promising a reset that
    cannot happen. A sample change and a defaulted base name each add their
    own leading sentence, so the two "blank means keep it" fields never act
    silently.

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
- **`tabs/agent.py`** — `AgentTab`, where the human sits in the loop for
  anything an MCP client asks to move (see "MCP server" below for the layer
  that parks the requests). Its own tab rather than a banner in the HKL tab:
  the scope covers slits, the sample stage and scans as well as the
  diffractometer, and one place to look beats three.

  - **The pending card** — device, what will move, the solved angles for an
    hkl move, the exact command, when it was asked, and whether a guard was
    overridden — with **Approve** / **Reject**. Approve runs
    `_gui_mcp_execute('<token>')  # <command>` through `run_in_console()`;
    Reject goes out invisibly through `poller.execute_once()` as
    `_gui_mcp_cancel(...)`, since a refusal is not something to read back later.
  - **Auto mode** — `Allow LLM moves without asking`, with a duration
    (30 min default, 1–240) and a live countdown. Unchecked at construction and
    unchecked again by `app.py`'s restart path (`clear_auto_mode()`), so it can
    never be inherited from a previous session or survive a kernel restart.
    While live, a request is approved on arrival **through the same
    `_gui_mcp_execute` path** — one code path for motion, not two.
  - **Refuse all motion requests**, the master off switch, enforced kernel-side
    so a request is declined with a sentence the model can act on rather than
    sitting unanswered. `on_kernel_values` mirrors the kernel's `blocked` flag
    into the box **with signals blocked**, so following the kernel never echoes
    a write back at it.
  - **History**, so "what did it do while I was at lunch" has an answer that
    does not need the transcript.

  `_acted` (a set of tokens already answered) guards both the button and auto
  mode: the console runs asynchronously, so the next poll can still show a
  request that is about to start, and without it auto mode would approve the
  same token twice.

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

  `_echo_llm` puts an MCP client's changes on screen: for a `stream` message
  whose text starts with `[LLM]` and whose `parent_header["session"]` is not the
  console's own, it calls `console.append_stream(text)`. Driven off
  `poller.iopub_message` rather than qtconsole's `include_other_output`, because
  `BaseFrontendMixin.from_here()` compares **session ids, not msg ids** — the
  trait would also echo `StatusPoller`'s empty 1 Hz cell as `[remote] In [n]:`
  once a second. The session check is what stops a `_gui_mcp(...)` call typed by
  hand in the console from being printed twice.

  It also **raises the Agent tab** when a request arrives
  (`AgentTab.request_arrived` → `tabs.setCurrentIndex`): a move waiting behind
  another tab is a move nobody approves. `_do_restart()` calls
  `clear_auto_mode()` on it. Both are guarded by `self._agent_tab is not None`,
  set in `_build_tabs`, so the restart path works whether or not `AgentTab` is
  in `TABS`.

  `MainWindow._dispatch(hook, *args)` fans the poller signals out to the tabs
  **inside a `try`**, logging and carrying on rather than letting one tab's
  exception escape. These run in Qt slots, and an exception escaping a slot
  does not merely fail the update — PyQt aborts the process, which takes the
  kernel and the running experiment with it. A malformed `channels` list once
  killed a live session that way through `QComboBox.addItems`; losing an
  experiment to a display bug in one tab is far too high a price, so the
  failing tab is named in the status bar and the rest still update.

**Adding a tab:** subclass `BaseTab`, set `title`, override the hooks you need,
add the class to `TABS` in `app.py`. The window wires the poller signals to
every tab automatically.

**Restart button** does a full kernel restart, clears the console, then waits
for `ready` before re-running the bootstrap. Note `QtKernelManager.kernel_restarted`
fires *only* for an autorestart, never for a deliberate `restart_kernel()` — so
the restart path must drive the bootstrap itself rather than rely on that signal.
The button is disabled until the kernel is ready.

### MCP server (`src/id6_b/mcp_server/`)

An MCP server that lets an LLM run the experiment in words: the **HKL setup**
— sample, lattice, reflections, UB, geometry mode, fixed angles, ψ reference,
hkl → angles — plus **motion and scans behind a human-in-the-loop gate**, in a
**running GUI session**, so the result appears in the HKL tab and is what
subsequent scans use. Run over stdio as `id6b-mcp`, next to `id6b-gui`.

Needs the SDK, which is not part of a plain install: `conda activate 6idb-bits
&& pip install mcp` (the `mcp` extra). Installed in `6idb-bits` as `mcp` 2.0.0.
Adding the `id6b-mcp` script to an existing editable checkout needs
`pip install -e . --no-deps --no-build-isolation`.

**Both diffractometers, but the real one must be named.** `ALLOWED_DEVICES =
{"psic_sim", "psic"}`; every tool takes `device=` and defaults to `psic_sim`,
so reaching the real machine is always an explicit act. `psic_psi`/`psic_q`
stay out — separate engines on the same motors, and nothing here needs them.
The allow-list is checked **in the kernel**, so a bug in the server, or a
second client that found the connection file, is still bounded by it.

#### The invariant

> **The MCP layer can only ever *request*. The only process that emits motion
> is the GUI, and only from a human click or an operator-armed auto window.**

Mechanically: `_gui_mcp` never calls `RE(...)`. A motion or scan op *validates*
and parks a request; the Agent tab sees it on the existing 1 Hz poll and, on
Approve, runs `_gui_mcp_execute(token)` through `BaseTab.run_in_console()` —
the same path the HKL tab's Move button uses. Every LLM-originated motion is
therefore an ordinary console command: in the history, in the transcript,
interruptible with Ctrl-C, on the session RunEngine.

It also sidesteps a problem that would otherwise be nasty. A running plan holds
the kernel's shell channel and `_probe_idle()` refuses rather than queueing, so
a *blocking* MCP move would exceed `CALL_TIMEOUT` and leave every later call
reporting "busy". Requests return in milliseconds; progress is followed from
outside the kernel (`status()`, below).

Four layers, split so the middle two are testable before the SDK exists:

- **`bridge.py`** — `MCP_HELPERS_CODE`, the kernel-side dispatcher, appended to
  `parts` in `KernelSession.bootstrap()` next to `HKL_HELPERS_CODE`. Every
  operation is one of the existing `_gui_hkl_*` functions; this module adds no
  diffractometer logic. Entry point `_gui_mcp(payload)`, where `payload` is a
  **JSON string** — the only value interpolated into executed code, sent through
  `repr()`, so a sample name cannot become code.

  **The two allow-lists live here, not in the server.** `_GUI_MCP_DEVICES`
  (`ALLOWED_DEVICES = {"psic_sim", "psic"}`) is checked in the kernel, so a bug
  in the server — or a second, unofficial client that found the connection file
  — is bounded by it; enforcing it client-side would be a comment, not a
  control. `op` likewise indexes an explicit dict of
  op → `(function, argnames, mutating)`; nothing is `getattr`'d off the payload,
  and unexpected argument names are refused by name. Each entry also records
  which devices it accepts, so an axis move cannot arrive addressed to a
  diffractometer engine and an hkl op cannot arrive addressed to
  `SESSION_DEVICE` (`"session"`, the sentinel for the ops that are not about
  one diffractometer — axes, scans, counters, the request queue).

  Before every mutating op the previous `dev.sample.UB` is stashed in
  `_gui_mcp_ub_backup`, which `restore_ub` reads — `compute_ub` on two mistyped
  reflections otherwise destroys a working orientation with no way back. One
  level of undo, UB only.

  A mutating op prints one `[LLM]` line pair for the operator to read:

  ```
  [LLM] set_lattice(psic_sim): values(a=5.43)
  [LLM]   -> Lattice updated. UB computed from r1 and r2.
  ```

  It is **one `print()` with the prefix on every line**, because the GUI selects
  the lines to echo by that prefix and a stream message the kernel happened to
  split would otherwise lose its tail. Reads print nothing.

  `FAILURE_PREFIXES` maps the `_gui_hkl_*` refusal sentences onto the `ok` flag.
  Advisory only — the message is always returned and is the authoritative
  answer, since those helpers report errors as prose rather than raising.

- **`motion.py`** — `MOTION_HELPERS_CODE`, a second kernel string appended to
  the same bootstrap cell so `bridge.py` stays readable. Same namespace, and
  `_gui_mcp_ops()` resolves names at call time, so the order of the two strings
  does not matter. It holds the request/approve state machine.

  *State* — `_gui_mcp_pending` (**one** request at a time; a second is refused
  in words, which is also what stops a model retrying itself into a queue of
  moves), `_gui_mcp_history` (the last `HISTORY_LIMIT` outcomes, for the Agent
  tab) and `_gui_mcp_motion_blocked` (the GUI's master off switch).

  *Requests.* `_gui_mcp_request_hkl` **solves first**, through the existing
  `_gui_hkl_calc`, so the six angles are known before anyone is asked to
  approve — and the parked command moves *those angles*, not `h/k/l`: moving
  the pseudo axes would solve again at execution time, against whatever the
  presets and the wavelength are by then, which is not what the operator saw on
  the banner. `_gui_mcp_request_axes` takes dotted path → number, resolved
  against the axis list `_gui_scan_options()` already builds, which is the
  allow-list for this op (101 axes, containers excluded); anything else is
  refused by name. `_gui_mcp_request_scan` builds the call with
  `gui/scancode.py:format_scan_call()`, so the Scan tab, the Macro tab and the
  model cannot disagree about `ascan`'s argument order; a scan moves motors, so
  it goes through the same gate.

  *Guards* — `_gui_mcp_check_targets()`, run at request time and **again**
  inside `_gui_mcp_execute`, since minutes may pass at the banner:

  1. **Soft limits.** `positioner.check_value(target)` where it exists (ophyd
     raises `LimitError`, the canonical check, and it covers pseudo axes),
     falling back to `.limits`. Every axis is checked before anything is parked
     and one failure refuses the whole request — no partial move, ever. The
     refusal leads with the axis and its limits and keeps ophyd's own text in
     brackets after it: `LimitError` names the *setpoint signal* and says
     "outside of range", which reads as an internal error rather than as an
     instruction.
  2. **Maximum travel.** `MAX_TRAVEL_DEG = 90.0` for diffractometer angles,
     plus a unit-free `MAX_TRAVEL_FRACTION = 0.5` of the soft-limit span for
     any axis with finite limits — a single absolute cap cannot mean the same
     thing in degrees, mm, keV and kelvin. **The numbers are deliberately
     loose**: driving to a reflection from the home position is routinely 40–60°
     on eta, so a tighter cap would refuse the *first* move of nearly every
     experiment and teach a client to pass `allow_large_move` by habit — which
     is how a guard stops being one. This one catches a decimal point in the
     wrong place; the approval banner is the real protection. `allow_large_move`
     lifts both, and the banner says when it was used.

  *Approval* — `_gui_mcp_execute(token)` re-validates, prints the `[LLM]` lines
  and *then* runs `RE(...)`. The GUI sends it as
  `_gui_mcp_execute('a1b2c3')  # RE(bps.mv(psic.eta, 12.5, …))` — the trailing
  comment carries the readable command into the console and the transcript,
  while the re-validation stays kernel-side where a client cannot skip it.
  `_gui_mcp_cancel(token, reason)` clears a rejected, withdrawn or expired one.

  *Reads*, all in `MOTION_READ_OPS` so they print nothing and leave nothing in
  the transcript: `_gui_mcp_list_axes()` (`axis`, `class`, `position`, `low`,
  `high`), `_gui_mcp_read_axes()` (`values` + `unknown`), `_gui_mcp_counters()`
  and `_gui_mcp_last_scan()` (scan id, plan, motors, and the peak statistics
  from `utils/peak_statistics.py` — the same numbers the Scan plot tab shows).

- **`session.py`** — `HklSession`, a plain `BlockingKernelClient` with one
  method that matters, `call(op, **args)`. **No `mcp` import**, which is what
  makes the whole path above testable before the SDK is installed and keeps
  `server.py` a declaration of tools and nothing else.

  *Discovery.* The GUI writes `.id6b-gui-kernel.json` into its kernel's cwd;
  `find_pointer()` walks up from the current directory, so a client launched
  anywhere inside the session's tree finds it. `ID6B_GUI_KERNEL_FILE` and
  `--connection-file` override, and `resolve_connection_file()` tells a pointer
  file from a connection file by content so the wrong one still works.
  Deliberately **not** `jupyter_client.find_connection_file()`, whose
  no-argument form returns the newest runtime file — possibly an unrelated
  notebook, and attaching to the wrong kernel is the one failure this must not
  have. A pointer whose `pid` is dead is refused with a sentence.

  *Reply channel.* The value comes back through `user_expressions`, the same
  round trip `StatusPoller._collect_reply` uses — not `execute_result`.
  **Reads and mutations are sent differently, on purpose:**

  ```python
  if op in READ_OPS:      # empty code -> no execute_input on iopub at all
      code, expressions = "", {"r": expression}
  else:                   # real code -> console + transcript keep it
      code = f"{REPLY_NAME} = {expression}"
      expressions = {"r": "_gui_mcp_last"}
  ```

  A model polling `get_state` therefore leaves nothing in the console or the
  log, while a change is on the record. The mutating form **assigns** rather
  than calling bare: a bare call is an expression, and IPython displays its JSON
  as `Out[n]`, burying the readable `[LLM]` line. A trailing `;` does *not*
  suppress that here — measured, not assumed. `REPLY_NAME` is `_gui_mcp_reply`,
  not `_`, which is IPython's own last-output variable.

  `call()` takes a **per-call** `device` (defaulting to `psic_sim`) rather than
  a fixed one, and `ALL_READ_OPS = READ_OPS | MOTION_READ_OPS`, so the motion
  reads stay invisible too.

  *Status without the kernel.* While a move or a scan runs the shell channel is
  blocked and every kernel call is refused by design — so `status()` does not
  use it. Two sources, both already proven elsewhere in this package:
  **Channel Access from the client process** (`epics.PV`, exactly as the HKL
  tab's progress bar does) over the readback PVs cached from `real_pvs`
  whenever state was last read while idle; and **`.re_md_dict.yml`** in the
  kernel's cwd for `scan_id` and metadata, which `StoredDict` writes from a
  background thread and is therefore readable *during* a scan. A very early
  call — before any state read — has no PVs cached and says so.

  *Never queue behind a scan.* A running plan holds the shell channel and a
  request sent to a busy kernel is **queued** — it would run when the scan ends,
  possibly an hour later, silently changing an orientation long after anyone
  asked. `_probe_idle()` sends an empty silent no-op first and refuses within
  `PROBE_TIMEOUT` (3 s) if it does not come back, with a sentence the model can
  act on; a queued probe that lands later executes nothing. `call()` never
  raises — transport failures come back in the same `{"ok", "message", "data"}`
  shape as a refusal, so a caller does not have to tell them apart by type.

- **`server.py`** — the protocol layer: 29 tools, each one line into
  `HklSession.call`, plus `build()` and `main()` (the `id6b-mcp` console
  script). Under `[project.scripts]`, **not** `[project.gui-scripts]`, which on
  Windows builds a console-less launcher whose stdout — the protocol channel —
  goes nowhere. A missing SDK prints the install command to **stderr** for the
  same reason.

  `_server_class()` accepts **either SDK generation**: `mcp` 2.0 renamed
  `mcp.server.fastmcp.FastMCP` to `mcp.server.MCPServer`, and `@server.tool()`
  and `run()` are the same on both, so the rename does not reach the tool
  declarations. `version` is passed only when the constructor takes it — on 1.x
  an unknown keyword lands in the settings object and raises.

  The real work here is the descriptions. They carry the ordering rules that
  make hklpy2 setup succeed and that are otherwise invisible at the keyboard:
  mode before the angles it holds constant (which axes *can* be fixed changes
  with the mode), two orienting reflections before a UB, a `psi_constant` mode
  before a fixed ψ, an axis left out of `set_fixed_angles` having *no* preset
  rather than its current value. `hkl_add_reflection` with `angles` omitted uses
  the **live motor positions** — the intended division of labour: the human
  drives to the peak, the model does the bookkeeping.

  The ten added by this feature:

  | Tool | Notes |
  |---|---|
  | `move_hkl(h, k, l, device, allow_large_move)` | solves, validates, requests approval |
  | `move_axes(targets, allow_large_move)` | dotted paths → values; one refusal fails all |
  | `run_scan(plan, axes, points, time, detectors, fixq)` | `count`/`ascan`/`lup`/`grid_scan`/`rel_grid_scan` |
  | `get_request_status()` / `cancel_request(reason)` | poll or withdraw the parked request |
  | `list_axes()` / `read_axes(axes)` | positions and soft limits |
  | `get_counters()` / `get_last_scan()` | selection, monitor, peak statistics |
  | `get_session_status()` | kernel-free: busy flag, scan id, readbacks over CA |

  **Automatic attenuation is two more tools** — `get_attenuation()` and
  `set_attenuation(values)` — backed by `gui/atten_bridge.py`. `get_` is in
  `READ_OPS`, so a model polling it leaves nothing in the console; its
  `channels` list is the allow-list `signal` and `counter_signal` must be
  chosen from, and it has to be read first because those keys depend on which
  detectors the operator selected and cannot be guessed.

  `set_attenuation` is **mutating but not parked for approval**, which is a
  deliberate line. It moves nothing itself: it has the same standing as
  `set_mode` or `set_fixed_angles`, which decide where a later approved move
  actually goes and are likewise ungated — and are the more dangerous of the
  two. The filters do move once it is armed, but only inside a scan the
  operator has already approved, the setting shows in the Detectors tab and
  in every run's metadata, and each adjustment prints to the console. It
  still gets the `[LLM]` report line every mutating op gets. If that trade
  ever looks wrong, moving it behind the gate is one edit: change its entry
  in `_gui_mcp_ops()` to a `_gui_mcp_request_*` wrapper.

  Their descriptions carry the gate the same way: that a move **returns before
  it happens**, that the answer is "awaiting approval", that the model should
  poll `get_request_status` and must not re-request in the meantime, and that a
  guard refusal is a reason to ask the operator rather than to retry with
  `allow_large_move`.

  **Signals are set, not moved** — `list_signals(device_name)` and
  `set_signals(targets, allow_large_move)`, the second pair of tools, for
  everything writable that is not an axis: a filter transmission, a source
  meter's programmed voltage, a mode selector. `move_axes`' allow-list is
  `_gui_scan_options()["axes"]`, whose movable test needs a `.position` and
  which only walks one level down, so `filters.transmission` and
  `keithley2400.inp.voltage` were never reachable through it.

  They get a **second allow-list rather than a wider first one**.
  `_gui_mcp_settable_paths(root)` walks one root device's
  `walk_signals(include_lazy=False)`, keeps what has `write_access`, and drops
  any path that *is* an axis or lives under one. Widening `_movable()` instead
  would have pushed several hundred `velocity` / `user_offset` /
  `user_setpoint` entries into the Scan and Macro tabs' axis combos — those
  read the same list — and made a motor's coordinate system settable by
  accident. The two lists are disjoint by construction, and each op refuses the
  other's paths by name with a pointer to the right tool.

  Both request paths share `_gui_mcp_check_number()`, so the axis and signal
  guards cannot drift apart, and both re-validate inside `_gui_mcp_execute` —
  a signal is parked and approved through exactly the same gate as a move.
  Two differences: the `MAX_TRAVEL_DEG` cap is for angles and is skipped, so a
  signal with no soft limits has no travel cap either (the bargain a limitless
  axis already makes), and an `enum_strs` signal accepts a **choice by name**
  or an integral in-range index, nothing else. Values are therefore kept in
  their own type end to end — `_gui_mcp_value()` passes strings through, and
  `_gui_mcp_execute` writes the parked value rather than a `float()` of it, or
  `"Local"` would not survive the round trip.

  `list_signals()` with no argument lists the devices and their signal counts;
  a name lists that device's signals with class, value, limits, choices and
  units. It answers **one device at a time** because each value is a
  channel-access round trip, and reads only where `connected` is true, since a
  disconnected signal's `.get()` blocks.

#### Connecting a client

The server is **stdio**: the client spawns `id6b-mcp` as a child process and
talks JSON-RPC over its stdin/stdout. `mcp.run()` at `server.py:662` takes no
transport argument, so there is no port and nothing is listening.

Locally, that is one entry in the client's config — this is the one in
`~/.claude.json`, scoped to the project directory:

```json
"6idb-hkl": {
  "type": "stdio",
  "command": "/home/beams/USER6IDB/.conda/envs/6idb-bits/bin/id6b-mcp",
  "args": [],
  "env": {}
}
```

Two preconditions. **The GUI must be running first** — it writes
`.id6b-gui-kernel.json` on `start()` and removes it on `shutdown()`, and the
server has nothing to attach to without it. And **the server's cwd must be
inside the session's tree**, since `find_pointer()` walks up from it; otherwise
override with `--connection-file <pointer>` or `ID6B_GUI_KERNEL_FILE`. A
pointer left behind by a GUI that did not shut down cleanly is refused by name:
*"points at PID …, which is no longer running."*

**From another machine, run the server over SSH.** Not the ZMQ ports:
`QtKernelManager` binds `127.0.0.1`, and the three things the server reads
outside the shell channel — the motor PVs over Channel Access, `.re_md_dict.yml`
for the scan id, and the pointer file — all assume the beamline host. Carrying
the *stdio* instead leaves every one of them where it works:

```json
"6idb-hkl": {
  "type": "stdio",
  "command": "ssh",
  "args": [
    "-T", "user6idb@reciprocore.xray.aps.anl.gov",
    "/home/beams/USER6IDB/.conda/envs/6idb-bits/bin/id6b-mcp",
    "--connection-file", "/home/beams18/USER6IDB/6idb-bits/.id6b-gui-kernel.json"
  ]
}
```

Pass the **pointer** path, not a connection file: the connection file is a
fresh `/tmp` name on every GUI start, while the pointer path is stable across
restarts.

Two ways this fails, both in the transport rather than in the server:

- **A password prompt** lands in the protocol stream. Key-based auth only.
- **Anything a login script prints to stdout** corrupts the JSON-RPC framing —
  and the account's shell here is tcsh, so `.cshrc`/`.login` are the risk.
  Check with `ssh -T user6idb@reciprocore true | xxd | head`, which must
  produce nothing at all. `-T` (no TTY) matters for the same reason.

**Do not tunnel the kernel instead.** It can be made to work — forward the five
ZMQ ports, copy the connection file, and pass it with `--connection-file`, which
skips the pid check because `resolve_connection_file()` tells the two file types
apart by content — but `get_session_status()` then loses both its PV readbacks
and its scan id, which is precisely the tool that exists for the minutes when a
scan is blocking everything else. The security argument is the stronger one: the
connection file carries the kernel's HMAC key, and anyone who reaches those
ports with it executes arbitrary Python in the live session. **The approval gate
lives inside `_gui_mcp`; a raw kernel client goes around it entirely.** SSH keeps
the authentication in front of the process rather than in front of one code
path.

Proper remote support would be `mcp.run(transport=…)` for streamable HTTP plus a
bind address, auth and a reverse proxy — worth it for several people driving one
session, where the SSH wrapper is the right answer for one.

**No collision model.** The soft limits are the IOC's per-axis limits; they say
nothing about the detector arm meeting the cryostat or the analyzer. The
approval banner showing the solved angles is the real protection, and auto mode
gives that up for its window — which is why it is time-boxed, off by default,
and cleared by a kernel restart. There is one level of undo (UB) and **none for
motion**; `list_axes()`/`read_axes()` before a move is the model's own escape
route.

**Known wart:** because `store_history=False`, the transcript shows
`In [1]: _gui_mcp_reply = _gui_mcp('{"op": …}')` and the operator's next real
command reuses that prompt number. Redundant next to the `[LLM]` line, but it is
the exact bytes that were sent.

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
