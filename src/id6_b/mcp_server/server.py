"""MCP tools for setting up and driving the 6-ID-B diffractometer.

Run as ``id6b-mcp`` over stdio, next to a running ``id6b-gui``.  Every tool is
one call into :meth:`id6_b.mcp_server.session.HklSession.call`; the work is in
the descriptions, which carry the ordering rules that make hklpy2 setup
succeed -- a mode before the angles it holds constant, two orienting
reflections before a UB, a ``psi_constant`` mode before a fixed psi.

**Motion is requested, never performed.**  A move tool validates, then parks a
proposal for the operator to approve in the GUI's Agent tab, and returns
*before* anything happens.  The kernel-side dispatcher has no path to ``RE``
at all (:mod:`id6_b.mcp_server.motion`), so this is a property of the code
rather than a promise in a docstring.

Orientation tools take ``device``, defaulting to ``psic_sim``: the real
``psic`` has to be named, every time.  The allow-list and the per-op scope are
checked again in the kernel, so a bug here cannot address an op to a device it
was not written for.

Every tool returns ``{"ok", "message", "data"}``.  ``message`` is written to be
read: when hklpy2 declines something -- a preset for an axis this mode does not
hold constant, say -- it says which and why, so the next call can be corrected
without a round trip to the human.

Needs the ``mcp`` SDK::

    conda activate 6idb-bits && pip install mcp

Nothing else in this package imports it, so the rest works without it.  Both
SDK generations are accepted: 2.0 calls the decorator-style server
``mcp.server.MCPServer``, 1.x called it ``mcp.server.fastmcp.FastMCP``, and
``@server.tool()`` and ``run()`` behave the same on either.
"""

import argparse
import sys

from .bridge import SESSION_DEVICE
from .session import HklSession

_INSTRUCTIONS = """\
Drive the 6-ID-B diffractometer in a live Bluesky session: set up an
orientation, propose moves, run scans, read what came back.

Two devices. `psic_sim` is a simulator and is the default -- use it to try
something out. `psic` is the real instrument and must be named explicitly on
every call.

**You cannot change anything by yourself.** move_hkl, move_axes, set_signals
and run_scan validate the request and then park it for the human operator, who
approves or rejects it in the GUI. They return immediately, saying "awaiting
approval"; that is success, not completion. After one:

  - Poll get_request_status. Do NOT send the request again -- a second one
    while the first is pending is refused, and re-asking is how a queue of
    unwanted moves gets built.
  - The operator may have armed an auto-approve window, in which case it runs
    without a click. You cannot tell in advance, and should not assume it.
  - If a guard refuses (a soft limit, or too far in one move), that is a
    reason to tell the operator what you wanted and why, not to retry with
    allow_large_move. Only use allow_large_move when the human has said the
    long move is intended.

Orientation setup, in this order -- each step depends on the one before:

1. hkl_get_state -- always start here. It reports the current sample, lattice,
   reflections, mode, constant axes and UB, so nothing below is guesswork.
2. hkl_add_sample + hkl_select_sample, or hkl_set_lattice on the current one.
3. hkl_add_reflection twice. Omit `angles` to use the diffractometer's live
   position -- that is the normal case: the operator drives to the peak, you
   record it.
4. hkl_set_orienting (or hkl_compute_ub) to get an orientation matrix.
5. hkl_set_mode, then hkl_set_fixed_angles for the axes that mode holds
   constant. The order matters: which axes can be fixed depends on the mode.
6. hkl_calc_angles to solve an hkl into angles. Nothing moves.

Two kinds of thing can be changed, and they do not overlap:

  - Axes are *moved*: list_axes, then move_axes (or move_hkl, or run_scan).
  - Everything else writable is *set*: list_signals, then set_signals. That is
    where a filter transmission, a source-meter voltage, a temperature
    setpoint or a mode enum lives. Ask list_signals with no argument for the
    devices that have any, then again with device_name for one device's
    signals and what each accepts.

Reading is free and leaves no trace in the operator's console: list_axes,
read_axes, list_signals, get_counters, get_last_scan, get_session_status.

If a call reports the session is busy, a scan or an approved move is running.
get_session_status still answers -- it reads the motors over Channel Access
rather than through the session -- so use it to follow progress, and wait.
"""


def _add_tools(mcp, session):
    """Declare every tool against *session*."""

    def call(op, **args):
        return session.call(op, **args)

    def session_call(op, **args):
        """An op that acts on the session rather than on a diffractometer."""
        return session.call(op, device=SESSION_DEVICE, **args)

    # -- reading -----------------------------------------------------------

    @mcp.tool()
    def hkl_get_state(device: str = "psic_sim") -> dict:
        """Report the full orientation state of a diffractometer.

        *device* is "psic_sim" (the simulator, default) or "psic" (the real
        instrument).  Returns the sample list and their lattices, the current
        sample, its reflections (with keys, hkl, angles, and which two are
        orienting), the UB matrix, the current mode and the modes available,
        the axes this mode holds constant, the fixed angles (presets) in force,
        psi and the psi reference vector.

        Call this first, and again after anything unexpected: it is the only
        way to know which reflection keys and mode names exist.  It also
        teaches this server the axes' PV names, which is what lets
        get_session_status follow a move while the session is busy.
        """
        return call("get_state", device=device)

    @mcp.tool()
    def hkl_get_position(device: str = "psic_sim") -> dict:
        """Report where a diffractometer is now: hkl, the six angles, 2theta, psi.

        Also the wavelength and energy the solver is using.  This is a
        position, not a setting -- use hkl_calc_angles to find the angles for
        an hkl without going there.
        """
        return call("get_position", device=device)

    # -- sample and lattice ------------------------------------------------

    @mcp.tool()
    def hkl_add_sample(
        name: str,
        a: float,
        b: float,
        c: float,
        alpha: float = 90.0,
        beta: float = 90.0,
        gamma: float = 90.0,
        device: str = "psic_sim",
    ) -> dict:
        """Add a sample with a lattice, replacing one of the same name.

        Lengths in angstroms, angles in degrees.  Adding does not select:
        follow with hkl_select_sample.  A new sample has no reflections and no
        UB of its own.
        """
        return call(
            "add_sample",
            device=device,
            sample=name,
            a=a,
            b=b,
            c=c,
            alpha=alpha,
            beta=beta,
            gamma=gamma,
        )

    @mcp.tool()
    def hkl_select_sample(name: str, device: str = "psic_sim") -> dict:
        """Make *name* the current sample.

        Reflections, UB and lattice all belong to the current sample, so every
        other tool acts on whichever one this selected.
        """
        return call("select_sample", device=device, sample=name)

    @mcp.tool()
    def hkl_set_lattice(values: dict, device: str = "psic_sim") -> dict:
        """Change lattice parameters of the current sample.

        *values* maps parameter name to number, e.g. ``{"a": 5.431}``; only the
        ones given change.  The valid names are in ``lattice_names`` from
        hkl_get_state -- a cubic sample exposes only ``a``.

        UB is recomputed afterwards if two orienting reflections exist, so the
        orientation follows the lattice rather than going quietly stale.
        """
        return call("set_lattice", device=device, values=values)

    @mcp.tool()
    def hkl_remove_sample(name: str, device: str = "psic_sim") -> dict:
        """Delete a sample.  The current one cannot be removed; select another
        first."""
        return call("remove_sample", device=device, sample=name)

    # -- reflections and UB -------------------------------------------------

    @mcp.tool()
    def hkl_add_reflection(
        h: float,
        k: float,
        l: float,  # noqa: E741 - the Miller index is called l
        angles: dict = None,
        device: str = "psic_sim",
    ) -> dict:
        """Record a reflection: an hkl observed at a set of angles.

        Omit *angles* to use the diffractometer's **live position**, which is
        the usual case -- the operator drives to the peak and you record it.
        Given explicitly, it must name every real axis (see ``real_fields``
        from hkl_get_state), in degrees.

        Two reflections are needed for a UB.  The first two added become the
        orienting pair automatically; hkl_set_orienting changes which.
        """
        return call(
            "add_reflection",
            device=device,
            pseudos={"h": h, "k": k, "l": l},
            reals=angles,
        )

    @mcp.tool()
    def hkl_edit_reflection(
        key: str,
        hkl: dict = None,
        angles: dict = None,
        device: str = "psic_sim",
    ) -> dict:
        """Change an existing reflection in place.

        *key* is from hkl_get_state.  Pass *hkl* (``{"h":…,"k":…,"l":…}``),
        *angles*, or both; what is left out is kept.  Editing one of the two
        orienting reflections recomputes UB.
        """
        return call(
            "edit_reflection", device=device, key=key, pseudos=hkl, reals=angles
        )

    @mcp.tool()
    def hkl_remove_reflection(key: str, device: str = "psic_sim") -> dict:
        """Delete a reflection.

        An orienting reflection is refused -- point hkl_set_orienting at a
        different pair first, or the sample would be left without an
        orientation.
        """
        return call("remove_reflection", device=device, key=key)

    @mcp.tool()
    def hkl_set_orienting(first: str, second: str, device: str = "psic_sim") -> dict:
        """Choose which two reflections define the orientation, and compute UB.

        Both keys come from hkl_get_state and must differ.  They should be
        non-parallel: two reflections along the same direction cannot fix a
        rotation about it, and the UB that comes back will be unusable.
        """
        return call("set_orienting", device=device, first=first, second=second)

    @mcp.tool()
    def hkl_compute_ub(device: str = "psic_sim") -> dict:
        """Recompute UB from the current orienting pair.

        Needed after changing a reflection's angles by other means; the tools
        above already do it themselves.
        """
        return call("compute_ub", device=device)

    @mcp.tool()
    def hkl_restore_ub(device: str = "psic_sim") -> dict:
        """Put back the UB from before the last change.

        One level of undo, for UB only -- a computation on a mistyped
        reflection would otherwise destroy a working orientation.  Lattice,
        mode and reflections are *not* restored.
        """
        return call("restore_ub", device=device)

    # -- mode, fixed angles, psi -------------------------------------------

    @mcp.tool()
    def hkl_set_mode(mode: str, device: str = "psic_sim") -> dict:
        """Choose the geometry mode, i.e. how the solver picks among solutions.

        Valid names are in ``modes`` from hkl_get_state.  Each mode solves some
        axes and holds the rest constant, so **set the mode before fixing
        angles** -- which axes can be fixed changes with it.

        A 'vertical' mode also defaults the unused horizontal detector angle to
        0, and vice versa, unless it already has a fixed value in this mode.
        """
        return call("set_mode", device=device, mode=mode)

    @mcp.tool()
    def hkl_set_fixed_angles(values: dict, device: str = "psic_sim") -> dict:
        """Fix (preset) the angles the current mode holds constant.

        *values* maps axis name to degrees, e.g. ``{"phi": 30}``.  An axis left
        out has no preset, so the solver uses that motor's live position
        instead -- omitting is not the same as passing its current value.

        Only axes in ``constant_axes`` from hkl_get_state can be fixed; hklpy2
        silently drops the others, so any that were ignored are named in the
        reply.  This changes *computed* solutions only.  Nothing moves.
        """
        return call("set_fixed_angles", device=device, values=values)

    @mcp.tool()
    def hkl_set_psi_reference(
        h2: float, k2: float, l2: float, device: str = "psic_sim"
    ) -> dict:
        """Set the reciprocal-space vector psi is measured against.

        Works from any mode -- it switches into a psi_constant mode to write
        the value and restores the mode afterwards, because that is the only
        mode in which hklpy2 keeps the reference.
        """
        return call("set_psi_reference", device=device, h2=h2, k2=k2, l2=l2)

    @mcp.tool()
    def hkl_set_psi(psi: float, device: str = "psic_sim") -> dict:
        """Fix the azimuthal angle psi, in degrees.

        Only meaningful in a psi_constant mode; call hkl_set_mode first, and
        hkl_set_psi_reference to say which vector psi is measured against.
        """
        return call("set_psi", device=device, psi=psi)

    # -- solving -------------------------------------------------------------

    @mcp.tool()
    def hkl_calc_angles(
        h: float,
        k: float,
        l: float,  # noqa: E741 - the Miller index is called l
        psi: float = None,
        fixed_angles: dict = None,
        device: str = "psic_sim",
    ) -> dict:
        """Compute the angles that would reach a given hkl.  Nothing moves.

        *fixed_angles* and *psi*, if given, are applied first and persist
        afterwards, exactly as the separate tools would leave them.

        A failure here usually means the reflection is unreachable in this mode
        with these fixed angles, not that the hkl is wrong: try relaxing a
        fixed angle or changing mode.  Use move_hkl to ask to go there.
        """
        return call(
            "calc_angles",
            device=device,
            h=h,
            k=k,
            l=l,
            psi=psi,
            presets=fixed_angles,
        )

    # -- motion: requested here, approved by the operator -------------------

    @mcp.tool()
    def move_hkl(
        h: float,
        k: float,
        l: float,  # noqa: E741 - the Miller index is called l
        device: str = "psic_sim",
        allow_large_move: bool = False,
    ) -> dict:
        """Ask the operator to move a diffractometer to an hkl.

        **Returns before anything moves.**  The hkl is solved into six angles
        first, so the request the operator sees is a list of angles, not three
        Miller indices; then the angles are checked against the soft limits and
        the per-move travel cap, and the request is parked for approval.

        A success here means *requested*.  Poll get_request_status; do not send
        it again while one is pending.

        The solution depends on the mode and the fixed angles in force, so set
        those first -- the same hkl in two modes is two different sets of
        angles, and only one of them may be the one wanted.

        *allow_large_move* lifts the travel cap only.  Soft limits are never
        lifted.  Use it when the human has said a long move is intended, not to
        get past a refusal on your own initiative.
        """
        return call(
            "request_hkl",
            device=device,
            h=h,
            k=k,
            l=l,
            allow_large_move=allow_large_move,
        )

    @mcp.tool()
    def move_axes(targets: dict, allow_large_move: bool = False) -> dict:
        """Ask the operator to move one or more axes to absolute positions.

        *targets* maps a dotted axis path to a number, e.g.
        ``{"sl1.hcen": 0.0, "sl1.hsize": 0.5}``.  Use list_axes for the paths;
        anything not in that list is refused by name.  Positions are absolute
        and in each axis's own units -- degrees, mm, keV.

        **Returns before anything moves**, exactly like move_hkl.  Every axis
        is checked before anything is parked, and one bad axis refuses the
        whole request, so a partial move is not possible.
        """
        return session_call(
            "request_axes", targets=targets, allow_large_move=allow_large_move
        )

    @mcp.tool()
    def set_signals(targets: dict, allow_large_move: bool = False) -> dict:
        """Ask the operator to set writable EPICS signals to absolute values.

        This is the counterpart of move_axes for the things that are *set*
        rather than moved -- a filter transmission, a source-meter voltage, a
        temperature setpoint, an enum that picks a mode.  *targets* maps a
        dotted signal path to a value, e.g.
        ``{"filters.transmission": 0.1}``.  Use list_signals for the paths and
        for what each one accepts; anything not in that list is refused by
        name, and a path that is really a motor is refused with a pointer to
        move_axes.

        A signal with named settings takes the name, not the number:
        ``{"filters.energy_select": "Local"}``.  A name that is not one of its
        choices comes back with the choices listed.

        **Returns before anything is written**, exactly like move_hkl -- a
        filter that goes in changes what the next scan measures as surely as a
        motor does, so it goes through the same approval gate.  Every target is
        checked before anything is parked, and one bad target refuses the whole
        request.
        """
        return session_call(
            "request_signals", targets=targets, allow_large_move=allow_large_move
        )

    @mcp.tool()
    def run_scan(
        plan: str,
        points: int,
        time: float,
        axes: list = None,
        detectors: list = None,
        fixq: bool = False,
        allow_large_move: bool = False,
    ) -> dict:
        """Ask the operator to run a scan.

        *plan* is one of: ``count`` (no axes -- just repeat readings),
        ``ascan`` (absolute), ``lup`` (relative to where the axes are now),
        ``grid_scan`` and ``rel_grid_scan`` (a mesh).

        *axes* is a list of ``{"axis": path, "start": number, "stop": number}``,
        with a per-axis ``"points"`` for the two grid plans; the trajectory
        plans share the single *points* value across their axes.  *time* is
        seconds per point; a negative value means monitor counts instead.

        Omit *detectors* to use whatever the operator has selected in the GUI
        -- that is the normal case and the one that keeps their counters
        configuration intact.

        *fixq* holds hkl constant during the scan, which is what makes an
        energy scan at a fixed reflection possible.

        **Returns before anything runs.**  Both ends of every axis are checked
        against the soft limits, since a scan that starts inside them and ends
        outside is a scan that stops half way.
        """
        return session_call(
            "request_scan",
            plan=plan,
            axes=axes,
            points=points,
            time=time,
            detectors=detectors,
            fixq=fixq,
            allow_large_move=allow_large_move,
        )

    @mcp.tool()
    def get_request_status() -> dict:
        """Report the pending move or scan request, and recent outcomes.

        ``pending`` is the request waiting for the operator, or null.  ``last``
        is what became of the previous one: approved and done, rejected,
        refused by a guard, or failed.  ``blocked`` means the operator has
        switched motion requests off entirely.

        This is the tool to poll after a move request.  It reads only, so it
        leaves nothing in the operator's console.  If the session reports busy,
        the approved move or scan is running: use get_session_status instead,
        which answers anyway.
        """
        return session_call("get_request")

    @mcp.tool()
    def cancel_request(reason: str = "withdrawn by the client") -> dict:
        """Withdraw the pending request.

        Use this when what was asked for is no longer wanted -- a change of
        plan, a correction after reading the state again.  It does not stop a
        move that has already been approved; only Ctrl-C in the session or the
        operator can do that.
        """
        return session_call("cancel_request", reason=reason)

    # -- reading the session ------------------------------------------------

    @mcp.tool()
    def list_axes() -> dict:
        """List every axis that can be moved, with its position and soft limits.

        The dotted paths here are exactly what move_axes and run_scan accept.
        Limits are ``null`` for an axis that has none configured, which means
        the travel cap is the only guard on it.
        """
        return session_call("list_axes")

    @mcp.tool()
    def read_axes(axes: list = None) -> dict:
        """Read named axes.  With no argument, reads the diffractometer angles.

        Cheaper and quieter than list_axes when the positions are all that is
        wanted.
        """
        return session_call("read_axes", axes=axes)

    @mcp.tool()
    def list_signals(device_name: str = None) -> dict:
        """List the writable EPICS signals that set_signals can set.

        Everything that is *set* rather than moved: a filter transmission, a
        source-meter voltage, a temperature setpoint, an enum that picks a
        mode.  Motor-like axes are deliberately absent -- they are in
        list_axes, and are moved with move_axes.

        With no argument, the devices that have any and how many, which is
        short.  With *device_name* -- ``"filters"``, ``"keithley2400"`` -- that
        device's signals in full: the dotted path set_signals accepts, the
        current value, the soft limits where the IOC publishes them, the units,
        and the ``choices`` of a signal that is set by name.  A device at a
        time, because reading each value is a channel-access round trip.
        """
        return session_call("list_signals", name=device_name)

    @mcp.tool()
    def get_counters() -> dict:
        """Report what the next scan will count.

        The selected detectors, their channels, the monitor channel and any
        extra devices recorded at each point.  This is the operator's
        selection; changing it is not something this server can do.
        """
        return session_call("get_counters")

    @mcp.tool()
    def get_attenuation() -> dict:
        """Report the automatic-attenuation settings.

        Automatic attenuation watches one detector channel at every scan
        point and changes the filter transmission until the reading is inside
        an accept window, retaking the point each time.  Readings outside the
        window are discarded, so only the accepted one is recorded.

        Returns whether it is armed (``ready``), the watched ``signal``, the
        ``low``/``high`` window, the step ``factor``, the current filter
        ``transmission``, and ``channels`` -- the data keys of the selected
        detectors, which is the list ``signal`` and ``counter_signal`` must be
        chosen from.

        Call this before set_attenuation: the channel names depend on which
        detectors the operator has selected, so they cannot be guessed.
        """
        return session_call("get_attenuation")

    @mcp.tool()
    def set_attenuation(values: dict) -> dict:
        """Configure automatic attenuation.  Nothing moves now.

        *values* maps setting name to value; anything left out keeps what it
        has.  Unlike a move or a scan this is **not** parked for approval --
        it changes a setting, the way set_mode does, and the filters only move
        later, inside a scan the operator has approved.  Tell the operator
        what you armed and why; they can see and change it in the GUI's
        Detectors tab.

        Settings:

        - ``signal`` -- data key to watch, from ``channels`` in
          get_attenuation.  For the Lambda 250K,
          ``"lambda250k_stats5_max_value"`` is the brightest pixel on the
          whole detector (stats5 is fed the full frame; stats1-4 are per-ROI).
        - ``low``, ``high`` -- the accept window, in the detector's units.
          Above *high* the filters close a step, below *low* they open one,
          and the point is retaken until it is inside or the tries run out.
        - ``factor`` -- the transmission step.  A number divides/multiplies
          the transmission by exactly that, so 10 walks 1 -> 0.1 -> 0.01.
          ``"auto"`` works each step out from how far off the reading is and
          usually lands inside the window in one step; prefer it unless the
          detector response is not linear in transmission.
        - ``max_tries`` -- adjustments allowed per point before the point is
          accepted as it stands.  Raise it for a small *factor* over a wide
          intensity range.
        - ``counter_signal`` -- an array counter confirming the detector
          plugin processed the frame just taken, e.g.
          ``"lambda250k_stats5_array_counter"``.  Set it for an area
          detector: its plugin callbacks are asynchronous, and a stale
          reading drives the adjustment the wrong way.
        - ``move_timeout`` -- how long to wait for the transmission readback
          to move before concluding the filter bank cannot step further.
          Raise it for a slow bank; too small a value ends the retake loop
          after one adjustment.
        - ``settle``, ``min_transmission``, ``max_transmission``, ``enabled``.

        A point that cannot be brought into range -- already wide open and
        still too dim, already at the minimum and still too bright, or the
        filters exhausted -- is accepted as it stands and the scan moves on.

        To switch it off without losing the settings, pass
        ``{"enabled": false}``.
        """
        return session_call("set_attenuation", values=values)

    @mcp.tool()
    def get_last_scan() -> dict:
        """Report the last scan and the peak of each of its detectors.

        Scan id, plan, the axis scanned, and for every hinted detector the
        centre, centre of mass, maximum, minimum and FWHM -- read out of the
        catalog, so they are the same numbers the GUI's Scan plot shows and the
        same ones an alignment plan would move to.

        Use this to decide the next move after a scan: read the peak, then ask
        for a move to it.
        """
        return session_call("get_last_scan")

    @mcp.tool()
    def get_session_status() -> dict:
        """Report motor positions and scan id **while the session is busy**.

        Every other tool is refused during a scan or an approved move, because
        a request sent to a busy session would queue and run much later.  This
        one does not go through the session at all: it reads the motors over
        Channel Access and the scan id from the RunEngine's metadata file, so
        it keeps answering throughout.

        ``busy`` says whether the session is running something.  ``positions``
        covers the axes whose PV names this server has learnt -- call
        hkl_get_state once on the device of interest first, or it has none to
        read.
        """
        return session.status()


def _server_class():
    """The SDK's decorator-style server class, under either of its names.

    ``mcp`` 2.0 renamed ``mcp.server.fastmcp.FastMCP`` to
    ``mcp.server.MCPServer``.  Everything used here -- ``@server.tool()``,
    ``run()`` -- is the same on both, so which one is installed does not reach
    the rest of this module.
    """
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build(connection_file=None):
    """Build the MCP server and the session it talks to."""
    import inspect

    server_class = _server_class()
    options = {"instructions": _INSTRUCTIONS}
    # ``version`` reaches ``serverInfo``, which clients display.  Only 2.0 takes
    # it as a constructor argument; on 1.x an unknown keyword lands in the
    # settings object and raises.
    if "version" in inspect.signature(server_class.__init__).parameters:
        options["version"] = _package_version()

    session = HklSession(connection_file=connection_file)
    mcp = server_class("6-ID-B HKL", **options)
    _add_tools(mcp, session)
    return mcp, session


def _package_version():
    """The installed version of this package, or an empty string."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version

    try:
        return version("polar-bits")
    except PackageNotFoundError:
        return ""


def main(argv=None):
    """Entry point for the ``id6b-mcp`` console script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--connection-file",
        help=(
            "Kernel connection file, or the GUI's .id6b-gui-kernel.json. "
            "Found automatically when this runs inside the session's "
            "directory tree."
        ),
    )
    options = parser.parse_args(argv)

    try:
        mcp, _ = build(options.connection_file)
    except ImportError:
        # stderr, not stdout: stdout is the protocol channel.
        print(
            "The MCP SDK is not installed. Run:\n"
            "    conda activate 6idb-bits && pip install mcp",
            file=sys.stderr,
        )
        return 1
    # Not connecting here: a GUI started after this server is still usable,
    # because HklSession.call attaches on first use and reports in words when
    # there is nothing to attach to.
    mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
