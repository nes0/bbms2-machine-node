# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# Small things that differ between MicroPython (the ESP32) and CPython (the simulation and the tests)
#

import sys
import time

MICROPYTHON = sys.implementation.name == "micropython"

if MICROPYTHON:
    # time.ticks_ms() wraps round after about 12 days, so add up the steps instead. This is called many times
    # a second, so no step is ever long enough to be misread.
    _last_ticks = time.ticks_ms()
    _total_ms = 0

    def now_ms():
        """Milliseconds since start-up. Never goes backwards and never wraps."""
        global _last_ticks, _total_ms
        ticks = time.ticks_ms()
        _total_ms += time.ticks_diff(ticks, _last_ticks)
        _last_ticks = ticks
        return _total_ms

    def print_exception(error):
        sys.print_exception(error)

else:
    _started = time.monotonic()

    def now_ms():
        """Milliseconds since start-up. Never goes backwards and never wraps."""
        return int((time.monotonic() - _started) * 1000)

    def print_exception(error):
        import traceback

        traceback.print_exception(type(error), error, error.__traceback__)


def log(*args):
    """A line on the serial console, with the time since start-up in seconds."""
    print("[%7d]" % (now_ms() // 1000), *args)
