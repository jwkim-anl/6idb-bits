"""
Shutters
"""

from time import sleep

from apstools.devices import ApsPssShutterWithStatus


class id6Shutter(ApsPssShutterWithStatus):
    """APS PSS shutter with optional auto-reopen logic on beam loss."""

    sleep_time = 5

    def _auto_shutter_subs(self, value, **kwargs):
        if value == 0:
            while True:
                if self.pss_state.get() == 0:
                    self.open_signal.set(1)
                    sleep(self.sleep_time)
                else:
                    break

    def start_auto_shutter(self):
        """
        Subscribe to pss_state so the shutter reopens automatically on beam
        loss.
        """
        self.pss_state.subscribe(self._auto_shutter_subs)

    def stop_auto_shutter(self):
        """
        Unsubscribe all pss_state callbacks to disable automatic reopening.
        """
        self.pss_state.unsubscribe_all()
