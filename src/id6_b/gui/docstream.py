"""Stream Bluesky documents from the kernel into the GUI process.

Live plotting cannot be done inside the kernel on this beamline:

* ``%matplotlib qt`` *before* ``id6_b.startup`` breaks ``import gi``, which
  ``hklpy2``/``libhkl`` needs (Qt and PyGObject fight over shared libraries);
* ``%matplotlib qt`` *after* startup leaves the RunEngine without the Qt
  teleporter it would have built at construction time, and the next scan dies
  with ``QObject::setParent: Cannot set parent, new parent is in a different
  thread`` -- in testing it hung the RunEngine outright.

So the kernel stays Qt-free and simply publishes documents over ZMQ.  The GUI
process, which already owns a Qt event loop, receives them and draws.  Nothing
here can wedge a scan: if this side dies, the kernel just publishes into a
socket nobody reads.

Topology, all on localhost::

    kernel: Publisher -> in_port -> Proxy -> out_port -> RemoteDispatcher: GUI
"""

import logging
import socket
import threading

from bluesky.callbacks.zmq import Proxy
from bluesky.callbacks.zmq import RemoteDispatcher
from qtpy.QtCore import QObject
from qtpy.QtCore import Signal

logger = logging.getLogger(__name__)

#: Run in the kernel once the RunEngine exists.  Kept tolerant: a failure here
#: must never stop the session, it only costs the live plot.
PUBLISHER_CODE = """\
try:
    from bluesky.callbacks.zmq import Publisher as _GuiPublisher
    _gui_publisher = _GuiPublisher(("127.0.0.1", {port}))
    _gui_publisher_token = RE.subscribe(_gui_publisher)
except Exception as _gui_exc:
    print("Live plot publisher unavailable:", _gui_exc)
"""


def free_port():
    """Return a currently-unused localhost TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DocumentStream(QObject):
    """Receive documents published by the kernel and re-emit them in Qt.

    ``document`` is emitted on the GUI thread: the dispatcher runs in its own
    thread, and Qt queues cross-thread signal emissions automatically.
    """

    document = Signal(str, dict)

    def __init__(self, parent=None):
        """Allocate ports for the proxy but do not start it yet."""
        super().__init__(parent)
        self.in_port = free_port()
        self.out_port = free_port()
        self._proxy = None
        self._dispatcher = None
        self._threads = []

    def start(self):
        """Start the proxy and dispatcher in daemon threads."""
        self._proxy = Proxy(self.in_port, self.out_port)
        self._spawn(self._proxy.start, "bluesky-zmq-proxy")

        self._dispatcher = RemoteDispatcher(("127.0.0.1", self.out_port))
        self._dispatcher.subscribe(self._on_document)
        self._spawn(self._dispatcher.start, "bluesky-zmq-dispatcher")
        logger.info("Live plot stream on ports %d -> %d.", self.in_port, self.out_port)

    def _spawn(self, target, name):
        def runner():
            try:
                target()
            except Exception:  # noqa: BLE001 - a dead stream must not kill the GUI
                logger.exception("%s stopped.", name)

        thread = threading.Thread(target=runner, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _on_document(self, name, doc):
        # Runs on the dispatcher thread; the signal hops to the GUI thread.
        try:
            self.document.emit(name, dict(doc))
        except Exception:  # noqa: BLE001 - never propagate into the dispatcher
            logger.debug("Could not forward %s document.", name, exc_info=True)

    def publisher_code(self):
        """Return the snippet that subscribes the kernel's RunEngine."""
        return PUBLISHER_CODE.format(port=self.in_port)

    def stop(self):
        """Stop the dispatcher.  Threads are daemons, so the proxy just ends."""
        if self._dispatcher is not None:
            try:
                self._dispatcher.stop()
            except Exception:  # noqa: BLE001 - best effort during teardown
                logger.debug("Dispatcher stop failed.", exc_info=True)
