"""Append the console's traffic to a plain text file.

Everything the session does passes through the IPython console -- typed
commands, the device-loading log, BEC's scan tables, tracebacks, and every
command a tab runs through :meth:`~id6_b.gui.tabs.base.BaseTab.run_in_console`
-- and none of it survives on its own: qtconsole trims its scrollback, the
Restart button clears the pane, and closing the window loses the lot.

The content comes from the kernel's **iopub** channel rather than from the
widget.  iopub is a broadcast, so the poll client sees every message the
console renders even though the console is on a different client
(:meth:`~id6_b.gui.kernel.StatusPoller._drain_iopub` was already reading it to
track busy/idle).  Taking it from there means the file is unaffected by the
scrollback limit or by clearing the console, and it keeps filling **while a
scan is running**, which is when it matters most.

Not IPython's ``%logstart``: that records input and results but not stream
output, tracebacks or the device-loading log, which is most of what is wanted
here.  ``apsbits``' ``logging_setup`` already starts one, into
``<cwd>/.logs/ipython_log.py``; this is the complement to it, not a duplicate.

Deliberately Qt-free, so it can be tested without a ``QApplication``.
"""

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: CSI escape sequences.  IPython colours its tracebacks and ophyd colours some
#: warnings; left in, they make the file unreadable in an editor.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Prompt for the first line of an input cell, and for the ones after it.
#: Matches what IPython itself shows, so a logged cell can be pasted back.
_IN_PROMPT = "In [{count}]: "
_CONTINUATION = "   ...: "

#: Marks the start of a session and the end of one.  Comment syntax, so a log
#: of a Python session stays valid Python if anyone feeds it back in.
_HEADER = "# ======== 6-ID-B console — session started {when} ========"
_FOOTER = "# ======== logging stopped {when} ========"
_NOTE = "# -------- {text} — {when} --------"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _clean(text):
    """Strip ANSI escapes from *text*."""
    return _ANSI.sub("", text or "")


def _now():
    return time.strftime(_TIME_FORMAT)


class ConsoleTranscript:
    """Write the console's input and output to a file, as the session runs.

    Feed :meth:`handle` every iopub message; call :meth:`start` and
    :meth:`stop` to open and close the file.  Writing never raises: an
    :class:`OSError` stops logging and is reported through :attr:`error`,
    because this is driven from a timer that must keep running.
    """

    def __init__(self):
        """Create an inactive transcript."""
        self._file = None
        self.path = None
        self.started_at = None
        self.bytes_written = 0
        self.error = None

    @property
    def active(self):
        """Whether a file is currently open for writing."""
        return self._file is not None

    def start(self, path):
        """Open *path* for appending and write the session header.

        Appends rather than truncates: one file can then hold a whole
        experiment, and pressing Start by mistake cannot destroy the earlier
        transcript.  Returns True on success; on failure :attr:`error` says
        why.
        """
        self.stop()
        self.error = None
        path = Path(path).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.exists() and path.stat().st_size > 0
            # Line buffered, so the file is current without explicit flushing:
            # `tail -f` follows the session, and nothing is lost if the GUI is
            # killed.  Only valid in text mode, which is what we want anyway.
            self._file = open(  # noqa: SIM115 - closed by stop()
                path, "a", encoding="utf-8", errors="replace", buffering=1
            )
        except OSError as exc:
            self._file = None
            self.error = str(exc)
            logger.warning("Could not open console log %s: %s", path, exc)
            return False

        self.path = path
        self.started_at = time.time()
        self.bytes_written = 0
        if existing:
            self._write("\n")
        self._write(_HEADER.format(when=_now()) + "\n\n")
        return self.active

    def stop(self):
        """Write the footer and close the file, if one is open."""
        if self._file is None:
            return
        self._write("\n" + _FOOTER.format(when=_now()) + "\n")
        try:
            self._file.close()
        except OSError:  # pragma: no cover - closing a broken handle
            logger.debug("Could not close %s.", self.path, exc_info=True)
        self._file = None

    def note(self, text):
        """Record *text* as a marker line.

        Used for events the kernel cannot report itself -- a deliberate
        restart, say, which clears the console but leaves the file intact.
        """
        if self._file is None:
            return
        self._write("\n" + _NOTE.format(text=text, when=_now()) + "\n\n")

    def handle(self, msg):
        """Write one iopub message.

        Unknown message types are ignored, so ``status``, ``clear_output``,
        comm traffic and anything a future kernel adds cost nothing.
        """
        if self._file is None:
            return
        content = msg.get("content") or {}
        handler = getattr(self, f"_on_{msg.get('msg_type')}", None)
        if handler is not None:
            handler(content)

    # -- one method per message type ---------------------------------------

    def _on_execute_input(self, content):
        code = _clean(content.get("code", "")).rstrip("\n")
        if not code:
            return
        prompt = _IN_PROMPT.format(count=content.get("execution_count", " "))
        lines = code.split("\n")
        body = "\n".join([prompt + lines[0]] + [_CONTINUATION + ln for ln in lines[1:]])
        self._write("\n" + body + "\n")

    def _on_stream(self, content):
        self._write(_clean(content.get("text", "")))

    def _on_execute_result(self, content):
        text = _clean((content.get("data") or {}).get("text/plain", ""))
        if text:
            self._write(f"Out[{content.get('execution_count', ' ')}]: {text}\n")

    def _on_display_data(self, content):
        # Only the text form: an image has no place in a text transcript, and
        # the plain-text alternative is what qtconsole falls back to anyway.
        text = _clean((content.get("data") or {}).get("text/plain", ""))
        if text:
            self._write(text + "\n")

    def _on_error(self, content):
        traceback = content.get("traceback") or []
        if traceback:
            self._write(_clean("\n".join(traceback)) + "\n")
            return
        name = content.get("ename", "Error")
        self._write(f"{name}: {content.get('evalue', '')}\n")

    # -- writing ------------------------------------------------------------

    def _write(self, text):
        """Write *text*, stopping the log rather than raising on failure."""
        if self._file is None or not text:
            return
        try:
            self._file.write(text)
        except OSError as exc:
            # A full disk or a vanished mount must not take the poll timer --
            # and with it the whole status display -- down with it.
            self.error = str(exc)
            logger.warning("Console log write failed, stopping: %s", exc)
            handle, self._file = self._file, None
            try:
                handle.close()
            except OSError:
                logger.debug("Could not close %s.", self.path, exc_info=True)
            return
        self.bytes_written += len(text.encode("utf-8", "replace"))
