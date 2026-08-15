"""MCP server exposing a running GUI session: HKL setup, motion and scans.

The layer above the kernel can only ever *request*.  The only process that
emits motion is the GUI, and only from a human click or an operator-armed auto
window -- see :mod:`~id6_b.mcp_server.motion`.

Four layers, deliberately separable:

``bridge``
    The kernel-side dispatcher, as a code string installed by the GUI's
    bootstrap.  This is where the device allow-list is enforced.
``motion``
    A second kernel-side string: the request/approve state machine and its
    guards, kept apart so ``bridge`` stays readable.
``session``
    A plain kernel client.  No ``mcp`` import, so the whole thing can be
    tested without the SDK installed.
``server``
    The MCP protocol layer -- tool declarations and nothing else.
"""
