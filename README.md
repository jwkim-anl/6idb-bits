# 6-ID-B BITS

Bluesky BITS instrument repository for APS beamline **6-ID-B**.

This is a fork of [BCDA-APS/6idb-bits](https://github.com/BCDA-APS/6idb-bits).
On top of the upstream BITS starter it adds two things that are not in the
original, alongside the 6-ID-B device, plan and callback modules under
`src/id6_b/`:

- **A Qt graphical interface** — `id6b-gui`. Parameter tabs above a real
  IPython console: scans, macros, live plots, diffractometer control, detector
  configuration.
- **An MCP server** — `id6b-mcp`. Lets an LLM drive the experiment in words —
  the whole HKL setup, plus motion and scans **behind a human-in-the-loop
  approval gate**.

Both are **purely additive**. The GUI runs the same
`from id6_b.startup import *` bootstrap inside a Jupyter kernel and drives it
through a real IPython console, so the plain `ipython`/Jupyter workflow is
untouched — anything that works in a terminal session works in the GUI's
console, and vice versa. The MCP server attaches to a *running GUI session*
rather than starting one of its own, so what it does shows up in the tabs and
is what subsequent scans use.

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
  - [Console transcript](#console-transcript)
  - [Optional 3D diffractometer view](#optional-3d-diffractometer-view)
  - [Saved preferences](#saved-preferences)
  - [Adding a tab](#adding-a-tab)
  - [Design constraints worth knowing](#design-constraints-worth-knowing)
- [MCP server (`id6b-mcp`)](#mcp-server-id6b-mcp)
  - [The invariant](#the-invariant)
  - [What it can do](#what-it-can-do)
  - [Approving a move](#approving-a-move)
  - [Guards](#guards)
  - [Connecting a client](#connecting-a-client)
  - [Running it from another machine](#running-it-from-another-machine)
  - [Architecture](#architecture)
  - [Limits worth knowing](#limits-worth-knowing)
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

### Optional extras

| Extra | Installs | For |
| --- | --- | --- |
| `mcp` | `mcp` | The [`id6b-mcp` server](#mcp-server-id6b-mcp). Only `server.py` imports the SDK, so the GUI and the kernel-side dispatcher work without it. |
| `gui3d` | `vtk<9.4.0`, `pyvista`, `pyvistaqt` | The [3D diffractometer view](#optional-3d-diffractometer-view) in the HKL tab. |

```bash
pip install -e ".[mcp]"     # or ".[gui3d]", or both
```

The GUI itself needs no extra: `qtconsole` and `qtpy` are ordinary
dependencies, so `id6b-gui` works as soon as the package is installed.

> Adding a new console script to an **existing** editable checkout needs
> `pip install -e . --no-deps --no-build-isolation`, otherwise `id6b-mcp` will
> not appear on `PATH`.

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

On start the GUI also writes `.id6b-gui-kernel.json` into the kernel's cwd, and
removes it on a clean shutdown. That pointer file is how
[`id6b-mcp`](#mcp-server-id6b-mcp) finds the session.

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
│ Session │ Agent │ Scan │ Macro │ Scan plot │ HKL │ …     │  parameter tabs
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
with real-world consequence — a scan, a diffractometer move, loading a macro,
an approved LLM request — is executed **through this console**, so it appears in
the session history, in the transcript, and can be interrupted exactly like a
typed command.

### Toolbar

| Control | Behaviour |
| --- | --- |
| **Restart Bluesky** | Full kernel restart: clears the console, waits for the kernel to be ready, then re-runs the bootstrap. Disabled until the kernel is ready. Also clears the Agent tab's auto mode. |
| **Console font** | 6–32 pt spin box. Stays in sync with qtconsole's own <kbd>Ctrl</kbd>+<kbd>=</kbd> / <kbd>Ctrl</kbd>+<kbd>-</kbd>. |
| **Background** | Black (default) or White. Changing it does not disturb the font size. |
| **Kernel state** | Right-hand readout: `kernel: idle` / `busy` / `not running`. |

Font and background are remembered between sessions
(see [Saved preferences](#saved-preferences)).

### Tabs

Eight tabs, in the order they appear. The list is `TABS` in `app.py`.

#### 1. Session (`tabs/status.py`)

Overview of the running session: catalog, user, proposal, beamline, `scan_id`,
scan state and file paths. RunEngine "running" is derived from kernel-busy,
because a running plan holds the shell channel and `RE.state` cannot be polled
mid-scan.

Two groups are controls rather than readouts:

- **Run metadata** sets the two `RE.md` entries the Session group above only
  displays — `login_id` (shown as *User*) and `proposal_id`. Changing either
  otherwise meant typing `RE.md["proposal_id"] = …` in the console. Two fields,
  one Apply, gated on kernel idle; **blank means keep**, so changing one of the
  two is one field. A caption notes that both keys are rewritten at every
  session start from `iconfig.yml` — this group is for the current session.
- **New data file** starts a fresh SPEC file and, with it, a fresh scan counter
  and the sample the data goes under. Two fields (Sample, Base name), one
  button, with a live preview of the resulting Masters path. Both fields in one
  group because the path is `<base_experiment_path>/<sample>/<base>_00001_master.hdf`
  — a sample applied elsewhere would leave the preview describing a folder that
  is no longer in force.

The Session group is the read-back and **lags a few seconds**: `StoredDict`
flushes from a background thread. The status line says so, rather than leaving
the delay to look like a failure.

#### 2. Agent (`tabs/agent.py`)

Where the human sits in the loop for anything an [MCP client](#mcp-server-id6b-mcp)
asks to move. See [Approving a move](#approving-a-move) for how it works.

It is its own tab rather than a banner in the HKL tab because the scope covers
slits, the sample stage and scans as well as the diffractometer — one place to
look beats three. The window **raises this tab automatically when a request
arrives**: a move waiting behind another tab is a move nobody approves.

#### 3. Scan (`tabs/scan.py`)

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

#### 4. Macro (`tabs/macro.py`)

A macro builder. A component chooser on the left inserts code into a Python
editor on the right; the editor's contents are the macro.

A macro is a **Bluesky plan** — one generator function run as a single
`RE(macro())` — so <kbd>Ctrl</kbd>+<kbd>C</kbd>, `RE.pause()` and resume apply
to the whole loop rather than to whichever scan happens to be running.

- **Seven components:** Loop, Set value, Wait, Scan, **Go to peak**, Print,
  Code. *Go to peak* emits `yield from cen()` (or `com`/`maxi`/`mini`), so a
  loop can align on each step.
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

#### 5. Scan plot (`tabs/scanplot.py`)

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

**A peak row under the plot** moves the scanned axis onto the peak after a scan:

```
Peak of [field v]  cen …  com …  max …  min …  fwhm …
[x] Markers  [Go to cen] [Go to com] [Go to max] [Go to min]
```

`cen` is the half-maximum midpoint — what BEC prints, so the button and the
console can never disagree. `fwhm` is shown for reference and gets no button
(it is a width, not a position). The statistics come from the **same arrays the
canvas draws**, so the Mon selection is inherited for free: with a monitor
chosen, the peak is that of `It/I0`, matching what is on screen. Dotted vertical
markers colour-match each statistic's name and its button. A statistic can be
`None` (a flat trace has no half-maximum crossing); that one gets no marker and
its button alone is disabled.

**Pop out** puts the plot in its own window, so it stays in view while another
tab is on top. It is a **move, not a copy** — one canvas, one data set, either
way.

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

#### 6. HKL (`tabs/hkl.py`, `hkl_bridge.py`)

Diffractometer control, backed by `hklpy2`.

- Samples and lattices; a reflection table with editable h/k/l and angles and
  first/second orienting selection; **Compute UB**; mode selection.
- **Fixed angles** — every mode solves some real axes and holds the rest
  constant; which axes *can* be fixed changes with the mode, and the tab
  presents that rather than requiring presets to be set in the console.
- ψ reference vector and a fixed ψ (in a `psi_constant` mode).
- Live current-position readout: h k l, the six angles each in its own column,
  2θ, ψ, ψ reference, and λ/energy.
- An **hkl → angles calculator** with a **Move** button.
- **Save and load the orientation**, and read/write diffractometer
  configuration files (`hkl_config_bridge.py`); the orientation is also saved
  into every run.
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

#### 7. Detectors (`tabs/detectors.py`)

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

**Automatic attenuation** is configured here too (`gui/atten_bridge.py`): the
filters are adjusted one point at a time *inside* the scan, so a peak that
saturates the detector is attenuated without restarting. The setting shows in
this tab and in every run's metadata, and each adjustment prints to the console.

#### 8. Devices (`tabs/devices.py`)

A sortable, filterable table of `oregistry.root_devices`: name, class, prefix,
labels and connected state. It lists *root* devices rather than `all_devices`,
which also contains every sub-component (~1800 entries).

### How the GUI talks to the session

Three separate channels, each chosen for a reason:

| Channel | Used for |
| --- | --- |
| **Console client** | Anything with real-world consequence — scans, moves, macro loads, approved LLM requests. Visible, logged, interruptible. |
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
The Agent tab's pending request rides this same 1 Hz poll — no new reader.

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

Poller signals are fanned out to the tabs **inside a `try`**. These run in Qt
slots, and an exception escaping a slot does not merely fail the update — PyQt
aborts the process, which would take the kernel and the running experiment with
it. The failing tab is named in the status bar and the rest still update.

### Console transcript

`transcript.py` appends the console's traffic to a plain text file. The console
is the record of what was done to the instrument, and none of it survives on its
own: qtconsole trims its scrollback, Restart clears the pane, and closing the
window loses the lot.

The content comes from **iopub, not from the widget**, so the file is unaffected
by the scrollback limit or by a console clear, and it keeps filling **while a
scan is running**. Input becomes `In [n]: …`, stream output goes in verbatim,
results become `Out[n]: …`, and tracebacks are included. ANSI escapes are
stripped. The file is opened append-only with line buffering, so `tail -f`
follows the session and a `kill -9` loses nothing.

This is deliberately **not** IPython's `%logstart`, which records input and
results but not stream output, tracebacks or the device-loading log.

### Optional 3D diffractometer view

`diffract3d.py` + `tabs/diffract_panel.py` draw a 3D model of the 4S+2D
diffractometer on the right of the HKL tab. Radio buttons switch between the
*current* angles and the last *calculated* ones; check boxes toggle the
scattering plane, the ψ reference vector and Q. The reference arrow is
`UB @ (h2, k2, l2)`, taking UB and h2/k2/l2 from the HKL tab, so there is one
source of truth.

It needs the `gui3d` extra (`vtk`, `pyvista`, `pyvistaqt`), which is **not**
installed by default:

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

## MCP server (`id6b-mcp`)

An [MCP](https://modelcontextprotocol.io) server that lets an LLM run the
experiment in words: the **HKL setup** — sample, lattice, reflections, UB,
geometry mode, fixed angles, ψ reference, hkl → angles — plus **motion and scans
behind a human-in-the-loop gate**.

It attaches to a **running GUI session** rather than starting one of its own, so
whatever it does appears in the HKL tab and is what subsequent scans use. Source
is `src/id6_b/mcp_server/`; the entry point is
`[project.scripts] id6b-mcp = "id6_b.mcp_server.server:main"`.

```bash
conda activate 6idb-bits
pip install -e ".[mcp]"
```

**Both diffractometers are reachable, but the real one must be named.** Every
tool takes `device=` and defaults to `psic_sim`, so reaching the real machine is
always an explicit act. The allow-list is `{"psic_sim", "psic"}` and is checked
**in the kernel**, so a bug in the server — or a second client that found the
connection file — is still bounded by it.

### The invariant

> **The MCP layer can only ever *request*. The only process that emits motion is
> the GUI, and only from a human click or an operator-armed auto window.**

Mechanically, the kernel-side dispatcher never calls `RE(...)`. A motion or scan
op *validates* and parks a request; the Agent tab sees it on the existing 1 Hz
poll and, on Approve, executes it through the console — the same path the HKL
tab's Move button uses. **Every LLM-originated motion is therefore an ordinary
console command**: in the history, in the transcript, interruptible with
<kbd>Ctrl</kbd>+<kbd>C</kbd>, on the session RunEngine.

This also sidesteps a practical problem. A running plan holds the kernel's shell
channel, so a *blocking* MCP move would time out and leave every later call
reporting "busy". Requests return in milliseconds instead, and progress is
followed from outside the kernel.

Every mutating operation prints an `[LLM]` line pair into the console, so the
operator can read what was done:

```
[LLM] set_lattice(psic_sim): values(a=5.43)
[LLM]   -> Lattice updated. UB computed from r1 and r2.
```

Reads print nothing, so a model polling status leaves no trace in the console or
the transcript.

### What it can do

31 tools. The descriptions carry the ordering rules that make hklpy2 setup
succeed and are otherwise invisible at the keyboard — mode before the angles it
holds constant, two orienting reflections before a UB, a `psi_constant` mode
before a fixed ψ.

**HKL setup** (not gated — these change *where a later approved move goes*):

| Tool | |
| --- | --- |
| `hkl_get_state` / `hkl_get_position` | current setup; live h k l and angles |
| `hkl_add_sample` / `hkl_select_sample` / `hkl_remove_sample` | sample list |
| `hkl_set_lattice` | lattice constants |
| `hkl_add_reflection` / `hkl_edit_reflection` / `hkl_remove_reflection` | reflection table. With `angles` omitted, `add` uses the **live motor positions** — the human drives to the peak, the model does the bookkeeping. |
| `hkl_set_orienting` | choose r1 and r2 |
| `hkl_compute_ub` / `hkl_restore_ub` | compute UB; **one level of undo** |
| `hkl_set_mode` / `hkl_set_fixed_angles` | geometry mode and the angles it holds |
| `hkl_set_psi_reference` / `hkl_set_psi` | ψ reference vector, fixed ψ |
| `hkl_calc_angles` | hkl → angles, without moving |

**Motion and scans** — every one of these parks a request for approval:

| Tool | |
| --- | --- |
| `move_hkl(h, k, l, device, allow_large_move)` | solves, validates, then requests approval |
| `move_axes(targets, allow_large_move)` | dotted paths → values; one refusal fails all |
| `set_signals(targets, allow_large_move)` | writable non-axis signals — a filter transmission, a programmed voltage, a mode selector |
| `run_scan(plan, axes, points, time, detectors, fixq)` | `count` / `ascan` / `lup` / `grid_scan` / `rel_grid_scan` |
| `get_request_status()` / `cancel_request(reason)` | poll or withdraw the parked request |

**Reads** — invisible in the console:

| Tool | |
| --- | --- |
| `list_axes()` / `read_axes(axes)` | positions and soft limits |
| `list_signals(device_name)` | one device at a time (each value is a channel-access round trip) |
| `get_counters()` | selection and monitor |
| `get_last_scan()` | scan id, plan, motors, and peak statistics — the same numbers the Scan plot tab shows |
| `get_attenuation()` / `set_attenuation(values)` | automatic attenuation |
| `get_session_status()` | **kernel-free**: busy flag, scan id, readbacks over Channel Access |

`get_session_status` is the one to use while a scan is running: the shell channel
is blocked then and every other call is refused by design, so it reads the motor
PVs directly and takes the scan id from `.re_md_dict.yml`, which is written from
a background thread and stays readable mid-scan.

> `set_attenuation` is **mutating but not gated**, deliberately. It moves nothing
> itself — it has the same standing as `set_mode`, which decides where a later
> approved move goes. The filters do move once it is armed, but only inside a
> scan the operator already approved, and each adjustment prints to the console.

**Axes and signals are two separate allow-lists**, disjoint by construction.
`move_axes` covers the ~101 scannable axes; `set_signals` covers everything
writable that is not an axis and does not live under one. Each op refuses the
other's paths by name, with a pointer to the right tool. Widening the axis list
instead would have pushed several hundred `velocity` / `user_offset` entries into
the Scan and Macro tabs' combos, and made a motor's coordinate system settable by
accident.

### Approving a move

A parked request appears on the **Agent tab**:

- **The pending card** — device, what will move, the solved angles for an hkl
  move, the exact command, when it was asked, and whether a guard was
  overridden — with **Approve** / **Reject**.
- **Auto mode** — `Allow LLM moves without asking`, with a duration (30 min
  default, 1–240) and a live countdown. Unchecked at construction and unchecked
  again by a kernel restart, so it can never be inherited from a previous
  session. While live, a request is approved on arrival **through the same
  execution path** — one code path for motion, not two.
- **Refuse all motion requests** — the master off switch, enforced kernel-side,
  so a request is declined with a sentence the model can act on rather than
  sitting unanswered.
- **History** — so "what did it do while I was at lunch" has an answer that does
  not need the transcript.

Only **one request is parked at a time**; a second is refused in words, which is
also what stops a model retrying itself into a queue of moves.

For an hkl move the request **solves first**, so the six angles are known before
anyone is asked to approve — and the parked command moves *those angles*, not
`h/k/l`. Moving the pseudo axes would solve again at execution time, against
whatever the presets and wavelength are by then, which is not what the operator
saw on the banner.

### Guards

Checked at request time and **again** at execution, since minutes may pass at the
banner:

1. **Soft limits** — `positioner.check_value(target)` where it exists, falling
   back to `.limits`. Every axis is checked before anything is parked, and one
   failure refuses the whole request: **no partial move, ever.**
2. **Maximum travel** — 90° for diffractometer angles, plus a unit-free 0.5 of
   the soft-limit span for any axis with finite limits (a single absolute cap
   cannot mean the same thing in degrees, mm, keV and kelvin).

**The travel numbers are deliberately loose.** Driving to a reflection from home
is routinely 40–60° on eta, so a tighter cap would refuse the *first* move of
nearly every experiment and teach a client to pass `allow_large_move` by habit —
which is how a guard stops being one. This one catches a decimal point in the
wrong place; **the approval banner is the real protection.** `allow_large_move`
lifts both caps, and the banner says when it was used.

Before every mutating op the previous UB is stashed, which `hkl_restore_ub`
reads — `compute_ub` on two mistyped reflections otherwise destroys a working
orientation with no way back. One level of undo, **UB only**.

### Connecting a client

The server is **stdio**: the client spawns `id6b-mcp` as a child process and
talks JSON-RPC over its stdin/stdout. There is no port and nothing is listening.

One entry in the client's config — this is a Claude Code `~/.claude.json`,
scoped to the project directory:

```json
"6idb-hkl": {
  "type": "stdio",
  "command": "/home/beams/USER6IDB/.conda/envs/6idb-bits/bin/id6b-mcp",
  "args": [],
  "env": {}
}
```

Two preconditions:

1. **The GUI must be running first.** It writes `.id6b-gui-kernel.json` on start
   and removes it on shutdown; the server has nothing to attach to without it.
2. **The server's cwd must be inside the session's tree**, since discovery walks
   up from it. Otherwise override with `--connection-file <pointer>` or the
   `ID6B_GUI_KERNEL_FILE` environment variable.

A pointer left behind by a GUI that did not shut down cleanly is refused by name
— *"points at PID …, which is no longer running."* Restart `id6b-gui` and it
rewrites itself.

### Running it from another machine

**Run the server over SSH — do not expose the kernel.** Three things the server
reads outside the shell channel — the motor PVs over Channel Access,
`.re_md_dict.yml` for the scan id, and the pointer file — all assume the beamline
host. Carrying the *stdio* leaves every one of them where it works:

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

Pass the **pointer** path, not a connection file: the connection file is a fresh
`/tmp` name on every GUI start, while the pointer path is stable across restarts.

Two ways this fails, both in the transport rather than the server:

- **A password prompt** lands in the protocol stream. Key-based auth only.
- **Anything a login script prints to stdout** corrupts the JSON-RPC framing —
  and the account's shell here is tcsh, so `.cshrc`/`.login` are the risk. Check
  with `ssh -T user6idb@reciprocore true | xxd | head`, which must produce
  nothing at all. `-T` (no TTY) matters for the same reason.

> **Do not tunnel the kernel instead.** It can be made to work, but
> `get_session_status()` then loses both its PV readbacks and its scan id — which
> is precisely the tool that exists for the minutes when a scan is blocking
> everything else. The security argument is stronger: the connection file carries
> the kernel's HMAC key, and anyone who reaches those ports with it executes
> arbitrary Python in the live session. **The approval gate lives in the kernel
> dispatcher; a raw kernel client goes around it entirely.**

Proper remote support would be streamable HTTP plus a bind address, auth and a
reverse proxy — worth it for several people driving one session, where the SSH
wrapper is the right answer for one.

### Architecture

Four layers, split so the middle two are testable before the SDK is installed:

| Module | Role |
| --- | --- |
| `bridge.py` | `MCP_HELPERS_CODE` — the kernel-side dispatcher, installed with the GUI bootstrap. Every operation is one of the existing `_gui_hkl_*` functions; it adds no diffractometer logic. The payload is a **JSON string** sent through `repr()`, so a sample name cannot become code. |
| `motion.py` | `MOTION_HELPERS_CODE` — the request/approve state machine, the guards, and the reads. |
| `session.py` | `HklSession`, a plain `BlockingKernelClient`. **No `mcp` import**, which is what makes everything above testable without the SDK. Handles discovery, the reply channel, and the kernel-free status path. |
| `server.py` | The protocol layer: 31 tools, each one line into `HklSession.call`, plus the `id6b-mcp` entry point. |

Two details that matter:

- **The allow-lists live in the kernel, not in the server.** Enforcing them
  client-side would be a comment, not a control. `op` indexes an explicit dict of
  op → (function, argnames, mutating); nothing is `getattr`'d off the payload,
  and unexpected argument names are refused by name.
- **A request is never queued behind a scan.** A running plan holds the shell
  channel, and a request sent to a busy kernel would be *queued* — running when
  the scan ends, possibly an hour later, silently changing an orientation long
  after anyone asked. The session probes for idle first and refuses within 3 s
  with a sentence the model can act on.

`server.py` accepts **either SDK generation**: `mcp` 2.0 renamed
`mcp.server.fastmcp.FastMCP` to `mcp.server.MCPServer`, and the tool declarations
are the same on both. It is under `[project.scripts]`, **not**
`[project.gui-scripts]`, which on Windows builds a console-less launcher whose
stdout — the protocol channel — goes nowhere.

### Limits worth knowing

- **No collision model.** The soft limits are the IOC's per-axis limits; they say
  nothing about the detector arm meeting the cryostat or the analyzer. The
  approval banner showing the solved angles is the real protection — and auto
  mode gives that up for its window, which is why it is time-boxed, off by
  default, and cleared by a kernel restart.
- **One level of undo for UB, none for motion.** `list_axes()` / `read_axes()`
  before a move is the model's own escape route.
- **The transcript shows the raw dispatcher call** rather than a tidy summary,
  and the operator's next real command reuses that prompt number. Redundant next
  to the `[LLM]` line, but it is the exact bytes that were sent.

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
- `psic_6idb_config.yml` — saved diffractometer orientation for `psic`
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
