# 6-ID-B BITS

Bluesky BITS instrument repository for APS beamline **6-ID-B**.

This is a fork of [BCDA-APS/6idb-bits](https://github.com/BCDA-APS/6idb-bits).
On top of the upstream BITS starter it adds a **Qt graphical interface**
(`id6b-gui`) that wraps the ordinary Bluesky session, plus the 6-ID-B device,
plan and callback modules under `src/id6_b/`.

The GUI is **purely additive**. It runs the same `from id6_b.startup import *`
bootstrap inside a Jupyter kernel and drives it through a real IPython console,
so the plain `ipython`/Jupyter workflow described below is untouched — anything
that works in a terminal session works in the GUI's console, and vice versa.

---

## Contents

- [Installation](#installation)
- [Starting a session](#starting-a-session)
  - [Graphical interface (`id6b-gui`)](#graphical-interface-id6b-gui)
  - [IPython console](#ipython-console)
  - [Jupyter notebook](#jupyter-notebook)
- [The GUI in detail](#the-gui-in-detail)
  - [Window layout](#window-layout)
  - [Toolbar](#toolbar)
  - [Tabs](#tabs)
  - [How the GUI talks to the session](#how-the-gui-talks-to-the-session)
  - [Optional 3D diffractometer view](#optional-3d-diffractometer-view)
  - [Saved preferences](#saved-preferences)
  - [Adding a tab](#adding-a-tab)
  - [Design constraints worth knowing](#design-constraints-worth-knowing)
- [Run sim plan demo](#run-sim-plan-demo)
- [Configuration files](#configuration-files)
- [queueserver](#queueserver)

---

## Installation

```bash
export ENV_NAME=6idb-bits
conda create -y -n $ENV_NAME python=3.11 hkl pyepics
conda activate $ENV_NAME
pip install apsbits
```

Then install this package from a checkout:

```bash
git clone https://github.com/jwkim-anl/6idb-bits
cd 6idb-bits
pip install -e ".[all]"
```

The GUI needs no extra step: `qtconsole` and `qtpy` are ordinary dependencies of
the package, so `id6b-gui` is available as soon as the package is installed. The
only optional piece is the [3D diffractometer view](#optional-3d-diffractometer-view).

### Creating a new instrument from scratch

Using 6-ID-B (`id6_b`) as an example:

```bash
export YOUR_INSTRUMENT_NAME=id6_b
create-bits $YOUR_INSTRUMENT_NAME
pip install -e .[all]
```

---

## Starting a session

### Graphical interface (`id6b-gui`)

```bash
conda activate 6idb-bits
id6b-gui
```

The kernel's working directory decides where `.re_md_dict.yml` and the data
files are written. It defaults to the directory you launch from; point it
somewhere else with:

```bash
id6b-gui --cwd /path/to/experiment
```

The window starts a Jupyter kernel, waits for it to report ready, then runs the
startup bootstrap. Device loading output appears in the console as it happens.
The prompt is live while this runs — anything typed before startup finishes is
queued and runs afterwards.

### IPython console

```bash
conda activate 6idb-bits
ipython -i -c "from id6_b.startup import *"
```

### Jupyter notebook

Start JupyterLab, a Jupyter notebook server, or a notebook in VSCode, then:

```py
from id6_b.startup import *
```

---

## The GUI in detail

Source lives in `src/id6_b/gui/`. The entry point is declared in
`pyproject.toml` as `[project.gui-scripts] id6b-gui = "id6_b.gui.app:main"`.

### Window layout

A vertical splitter, opened at **even halves** and draggable:

```
┌──────────────────────────────────────────────────────────┐
│ [Restart Bluesky] Console font: [10 pt]  Background: [▾] │  toolbar
│                                            kernel: idle  │
├──────────────────────────────────────────────────────────┤
│ Session │ Scan │ Macro │ Scan plot │ HKL │ Detectors │ … │  parameter tabs
│                                                          │
│                    (selected tab)                        │
├──────────────────────────────────────────────────────────┤
│ In [1]:                                                  │  IPython console
│                                                          │  (qtconsole
│                                                          │   RichJupyterWidget)
└──────────────────────────────────────────────────────────┘
```

The lower pane is a genuine Jupyter front end, not a log view. It has history,
tab completion, `?` help, Ctrl-C interrupt and `RE.pause()`/resume. Everything
the tabs do with real-world consequence — a scan, a diffractometer move, loading
a macro — is executed **through this console**, so it appears in the session
history, is logged, and can be interrupted exactly like a typed command.

### Toolbar

| Control | Behaviour |
| --- | --- |
| **Restart Bluesky** | Full kernel restart: clears the console, waits for the kernel to be ready, then re-runs the bootstrap. Disabled until the kernel is ready. |
| **Console font** | 6–32 pt spin box. Stays in sync with qtconsole's own <kbd>Ctrl</kbd>+<kbd>=</kbd> / <kbd>Ctrl</kbd>+<kbd>-</kbd>. |
| **Background** | Black (default) or White. Changing it does not disturb the font size. |
| **Kernel state** | Right-hand readout: `kernel: idle` / `busy` / `not running`. |

Font and background are remembered between sessions
(see [Saved preferences](#saved-preferences)).

### Tabs

Seven tabs, in the order they appear. The list is `TABS` in `app.py`.

#### 1. Session (`tabs/status.py`)

Overview of the running session: catalog, user, proposal, beamline, `scan_id`,
scan state and file paths. RunEngine "running" is derived from kernel-busy,
because a running plan holds the shell channel and `RE.state` cannot be polled
mid-scan.

#### 2. Scan (`tabs/scan.py`)

Build and launch a scan from a form: pick a plan (`count`, `ascan`, `lup`,
`grid_scan`, `rel_grid_scan`), detectors, axes, number of points and time per
point, then press **Scan**.

- **The exact command is shown before it runs**, and is executed through the
  console — so a scan stays an ordinary interruptible, logged command that the
  Scan plot tab draws live.
- The form encodes the argument-order difference between plan families, so it
  cannot build a call the plan rejects: `ascan`/`lup` share one point count
  across axes (a trajectory), the grid plans give each axis its own (a mesh).
- **Axes are found by capability, not class** — anything with a callable `set`
  and a `position`, plus writable root-level `Signal`s. That catches
  `EpicsMotor`, the hklpy2 pseudo axes, `SynAxis` simulated motors and
  `EnergySignal` alike; a positioner-only walk would miss several. A movable
  with movable children (`mono`, `psic`, `sl1`) is treated as a container, so
  the list offers `mono.energy` and `psic.h` rather than the container itself.
  ~101 axes today.
- Detector check boxes list only devices with `preset_monitor`. Choosing
  "monitor counts" (a negative time) disables them and defers to the `counters`
  selection.
- **Three axis rows for `ascan`/`lup`, two for the grid plans.** Each extra row
  is revealed by its own check box and requires the one before it, so rows can
  only be filled in order. This is a GUI limit, not a plan limit — the grid
  plans are held at two because the live image is 2D.

#### 3. Macro (`tabs/macro.py`)

A macro builder. A component chooser on the left inserts code into a Python
editor on the right; the editor's contents are the macro.

A macro is a **Bluesky plan** — one generator function run as a single
`RE(macro())` — so <kbd>Ctrl</kbd>+<kbd>C</kbd>, `RE.pause()` and resume apply
to the whole loop rather than to whichever scan happens to be running.

- **Six components:** Loop, Set value, Wait, Scan, Print, Code.
- **Every numeric field is free text**, not a spin box, so a loop variable can
  be used as a value or a scan limit (`centre - 0.1` survives into the
  generated code). Loop values are either a literal list or start/stop/steps
  expanded into one, so the temperatures are visible in the macro itself; text
  starting with `[` or `(` is used verbatim, so `range(...)` and comprehensions
  work.
- **Generation is one way.** The builder writes into the editor and never reads
  it back, so nothing hand-edited is silently rewritten — what is saved and run
  is exactly what is on screen. Insertion takes its indent from the cursor line
  and replaces a lone placeholder (`pass`, or the template's
  `yield from bps.null()`), which is what chains components together.
- **Load** compiles the source in the GUI process first, so a `SyntaxError` is
  reported with its line number and never reaches the console; it also refuses
  a function with no `yield`. Load only *defines* the macro — nothing executes.
- **Run** calls `RE(<name>())` for the first top-level `def`. It is disabled
  until a successful Load and re-disabled as soon as the text changes, so it can
  only call a definition the session actually has.
- Macros are plain `.py` files, defaulting to `<kernel cwd>/macros`; the
  last-used directory is remembered.

#### 4. Scan plot (`tabs/scanplot.py`)

A live matplotlib canvas embedded via `backend_qtagg` (no `pyplot`, so no global
state). BEC's inline console plot is unaffected — you get a live plot *and* the
after-scan inline one.

**Line mode** (`count`, `ascan`, `lup`):

- One curve per plottable detector field. x comes from
  `start["hints"]["dimensions"]`, falling back to elapsed time for `count()`.
- Fields hinted by the descriptor — whatever `counters` selected — are ticked by
  default; other numeric scalars are listed unticked. Array/image keys are
  excluded by their non-empty `shape`.
- Data is kept for **every** field regardless of tick state, so enabling a curve
  mid-scan shows its full history. Hidden curves do not stretch the axes.
- Fresh axes per run; curves stay after the scan.

**Field selectors** are a fixed 260 px scrollable panel to the right of the
canvas, one row per field, with a **Plot** check box and an exclusive **Mon**
radio. Choosing a monitor divides every plotted value by that field's value *at
the same point*; the curve label becomes `It / I0`. Raw values are always
retained, so changing or clearing the monitor recomputes the whole history
rather than only affecting new points. A zero or missing monitor reading yields
`NaN`, leaving a gap instead of a spike or a mid-scan exception.

**Grid mode** (`grid_scan`, `rel_grid_scan`) renders a **live image** instead of
curves:

- Orientation matches BEC's `LiveGrid` — inner (fast) axis horizontal, outer
  (slow) vertical — so the live image and the inline one agree.
- Cells are located from the **readback values against `extents`**, not from
  event order, so snaked and out-of-order points land correctly without
  modelling the trajectory. Unvisited cells stay blank, so the mesh visibly
  fills in.
- An image shows one channel, so the check boxes act as a channel selector.
  Monitor normalisation applies here too and recomputes the whole mesh.

#### 5. HKL (`tabs/hkl.py`, `hkl_bridge.py`)

Diffractometer control, backed by `hklpy2`.

- Samples and lattices; a reflection table with editable h/k/l and angles and
  first/second orienting selection; **Compute UB**; mode selection.
- Live current-position readout: h k l, six angles, 2θ, ψ, ψ reference, and
  λ/energy.
- An **hkl → angles calculator** with a **Move** button.
- A selector chooses between `psic` (red "real motors" banner) and `psic_sim`
  (green "simulated"), so which one you are driving is unambiguous.
- Optional [3D view](#optional-3d-diffractometer-view) on the right.

**Safety.** Every mutating control is disabled unless the kernel is idle. Move
runs through the console as `RE(bps.mv(...))`, so it uses the session RunEngine,
lands in history, and <kbd>Ctrl</kbd>+<kbd>C</kbd> aborts it. Move stays disabled
until a successful Calculate, so the confirmation can only show angles the
solver actually returned.

**Move progress bar.** The kernel's shell channel is blocked for the whole move,
so progress cannot be polled through it. The readback PVs are watched **directly
over Channel Access from the GUI process** instead, and progress is the mean
fractional travel over axes that actually move. Soft axes (`psic_sim`) have no
PVs, so the bar goes indeterminate rather than inventing a number. A 600 s
timeout stops it spinning if the kernel never comes back.

The kernel-side helpers in `hkl_bridge.HKL_HELPERS_CODE` mirror the call
sequences in `utils/hkl_utils_pete.py` but deliberately **do not import it** —
that module builds its own `RunEngine` at module scope, which would collide with
the session's.

#### 6. Detectors (`tabs/detectors.py`)

A per-channel `Kind` selector grouped by detector. Unlike the Scan plot check
boxes, which only hide already-recorded curves, this changes kernel-side
configuration and therefore **what future scans read**.

- **Four kinds as radio buttons, one tree column each**: `hinted`, `normal`,
  `config`, `omitted`. A row is one click and the tree reads down a column; the
  header explains each kind. `omitted` is offered because it is how an unnamed
  scaler channel is kept out of the descriptor — with a tooltip warning that
  omitting a *named* channel loses its readings entirely.
- **Apply is disabled unless the kernel is idle**: changing `Kind` mid-scan
  would desync the descriptor. After applying, the tab re-reads so the tree
  shows what the kernel actually accepted, and it refreshes on every `stop`
  document since plans may re-hint channels themselves.
- `Kind` is a flag, so a channel can report a composite like `normal|config`
  that matches no radio. Nothing is checked, the raw value is shown in the
  detail column, and the row contributes no change until a kind is picked.

**Extra devices.** The tab also manages `counters.extra_devices` — an *Add…*
picker over `oregistry.root_devices` lets any EPICS device (temperature,
capacitance, source meter) be recorded at every scan point. This is the correct
slot for such devices: `local_scans` does `bp_count(detectors + extras, ...)`,
so extras are recorded but skip `configure_counts_wrapper`, which calls
`rd(det.preset_monitor)` on every *detector*. Only `scaler` and `lambda250k`
have `preset_monitor`, so putting a thermometer in `counters.detectors` raises
`AttributeError: preset_monitor`, while as an extra it works.

Extra devices get `Kind` control too, as `<name>   (extra)` nodes in the same
tree. Their omitted signals are hidden behind a check box, since on a motor
bundle they dominate the listing.

#### 7. Devices (`tabs/devices.py`)

A sortable, filterable table of `oregistry.root_devices`: name, class, prefix,
labels and connected state. It lists *root* devices rather than `all_devices`,
which also contains every sub-component (~1800 entries).

### How the GUI talks to the session

Three separate channels, each chosen for a reason:

| Channel | Used for |
| --- | --- |
| **Console client** | Anything with real-world consequence — scans, moves, macro loads. Visible, logged, interruptible. |
| **Poll client** (`BlockingKernelClient`) | Filling the tabs. Deliberately a *separate* client so polling stays invisible in the console. |
| **ZMQ document stream** | Live plot data. |

**`kernel.py`** owns the `QtKernelManager` and both clients. `StatusPoller` is a
1 s `QTimer` reading two sources:

- **`.re_md_dict.yml`** — written from a *background* thread, so it keeps
  updating **while a scan is running**. This is the scan-safe source for
  catalog, user, proposal, beamline and `scan_id`.
- **`user_expressions`** — only when the kernel is idle, for values that never
  reach the file (SPEC filename, experiment paths). At most one request is in
  flight, which is what stops a burst of queued polls when a long scan ends.

Tabs ask for one-off values with `BaseTab.request()`, which merges expressions
into the next idle poll rather than opening another reader on the shell channel.

**`docstream.py`** carries live plot data. The kernel publishes documents over
ZMQ into a `Proxy` + `RemoteDispatcher` running in daemon threads **in the GUI
process**, which re-emits them as a Qt signal on the main thread. Ports are
chosen free at startup.

> **The kernel must stay Qt-free.** This is why plotting is not done there.
> `%matplotlib qt` *before* `id6_b.startup` breaks `import gi` (Qt vs PyGObject
> shared-library clash, and `hklpy2`/`libhkl` need `gi`), while *after* startup
> the RunEngine lacks the Qt teleporter it would have built at construction —
> the next scan then raises `QObject::setParent: ... different thread` and
> **hangs the RunEngine**.

`autorestart` is set **False** on the kernel manager: a silent restart would drop
every EPICS connection mid-experiment.

### Optional 3D diffractometer view

`diffract3d.py` + `tabs/diffract_panel.py` draw a 3D model of the 4S+2D
diffractometer on the right of the HKL tab. Radio buttons switch between the
*current* angles and the last *calculated* ones; check boxes toggle the
scattering plane, the ψ reference vector and Q. The reference arrow is
`UB @ (h2, k2, l2)`, taking UB and h2/k2/l2 from the HKL tab, so there is one
source of truth.

It needs `vtk`, `pyvista` and `pyvistaqt`, which are **not** installed by
default:

```bash
pip install -e ".[gui3d]"
```

These are imported lazily and gated on `diffract3d.AVAILABLE`, so **the GUI runs
normally without them** — the panel shows the install command instead. The
pure-maths half (`Rx`/`Ry`/`Rz`, `rot_about4`, `rotation_to_align`,
`Diffractometer.chain_matrix`) has no VTK dependency.

The geometry constants and kinematic chain were matched against the real
instrument and are used verbatim.

### Saved preferences

Console font size, background, and the Macro tab's last-used directory are
stored in `QSettings("APS", "id6b-gui")` — on Linux,
`~/.config/APS/id6b-gui.conf`.

### Adding a tab

1. Subclass `BaseTab` (`src/id6_b/gui/tabs/base.py`).
2. Set `title`.
3. Override the hooks you need:
   - `on_metadata(metadata)` — a fresh read of `.re_md_dict.yml`, including
     while a scan is running.
   - `on_kernel_values(values)` — evaluated `user_expressions`; idle only.
   - `on_kernel_state(state)` — `"idle"`, `"busy"` or `"dead"`.
   - `on_document(name, doc)` — streamed Bluesky documents.
4. Add the class to `TABS` in `app.py`.

The window wires the poller signals to every tab automatically. Useful helpers
on `BaseTab`: `run_in_console(code)`, `request(expressions)`, and the
`session_cwd` / `scrollable` attributes.

### Design constraints worth knowing

These are non-obvious and were each the fix for a real failure:

- **Nothing executes before the kernel signals ready.** qtconsole treats the
  kernel's own `status: starting` message as a crash-restart if it arrives while
  the widget is executing, which produces a spurious "kernel restarted" banner
  and resets the console.
- **The bootstrap is sent on the console's client with `silent=True`**, not via
  `console.execute()` (which would echo hundreds of lines of helper definitions
  and spend prompt `In [1]`), and not via qtconsole's hidden execute (which
  would swallow `stream` and `error` messages, hiding the device-loading log and
  any traceback).
- **The live-plot subscription is appended to the same cell as the bootstrap.**
  Separate sockets give no ordering guarantee, so sending it separately could
  arrive before the import and fail on a missing `RE`.
- **Every tab is wrapped in a `QScrollArea`** unless it sets
  `scrollable = False`; otherwise the tallest page pins the tab widget's minimum
  height and squeezes the console. The even split is applied in `showEvent`,
  because `setSizes()` before the first show is measured against size hints and
  silently overridden.
- **`QtKernelManager.kernel_restarted` fires only for an autorestart**, never for
  a deliberate `restart_kernel()` — so the Restart button drives the bootstrap
  itself.

---

## Run sim plan demo

To run some simulated plans that ensure the installation worked as expected,
run these inside an IPython session, a notebook, or the GUI's console after the
data acquisition session has started:

```py
from id6_b.plans.sim_plans import *
RE(sim_print_plan())
RE(sim_count_plan())
RE(sim_rel_scan_plan())
```

---

## Configuration files

The files that can be configured to adhere to your preferences are:

- `configs/iconfig.yml` — configuration for data collection
- `configs/logging.yml` — configuration for session logging to console and/or files
- `qserver/qs-config.yml` — all configuration of the QS host process. See the
  [documentation](https://blueskyproject.io/bluesky-queueserver/manager_config.html)
  for more details.

---

## queueserver

The queueserver has a host process that manages a RunEngine. Client sessions
interact with that host process.

### Run a queueserver host process

Install screen:

```bash
sudo apt install screen
```

Use the queueserver host management script to start the QS host process. The
`restart` option stops the server (if it is running) and then starts it. This is
the usual way to (re)start the QS host process. Using `restart`, the process
runs in the background.

```bash
./src/YOUR_INSTRUMENT_NAME_qserver/qs_host.sh restart
```

### Run a queueserver client GUI

To run the GUI client for the queueserver:

```bash
queue-monitor &
```

Note this is the **queueserver's** client, and is separate from `id6b-gui`
described above.

### Shell script explained

A [shell script](https://github.com/BCDA-APS/BITS/blob/main/src/apsbits/demo_qserver/qs_host.sh)
(`./src/YOUR_INSTRUMENT_NAME_qserver/qs_host.sh`) starts the QS host process.
Below are all the command options, and what they do.

```bash
(BITS_env) $ ./src/YOUR_INSTRUMENT_NAME_qserver/qs_host.sh help
Usage: qs_host.sh {start|stop|restart|status|checkup|console|run} [NAME]

    COMMANDS
        console   attach to process console if process is running in screen
        checkup   check that process is running, restart if not
        restart   restart process
        run       run process in console (not screen)
        start     start process
        status    report if process is running
        stop      stop process

    OPTIONAL TERMS
        NAME      name of process (default: bluesky_queueserver-)
```

Alternatively, run the QS host's startup command directly within the
`./qserver/` subdirectory:

```bash
cd ./qserver
start-re-manager --config=./qs-config.yml
```
