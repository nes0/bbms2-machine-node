# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# Starting the reader on the ESP32. main.py, the loader, calls run() once it has dealt with any
# firmware update. This file can itself be replaced by an update; main.py can't.
#

import asyncio
import gc
import sys
import time

import config
from util import log, print_exception


# Memory. The ESP32 has about 240 KB, in two pools. MicroPython's holds this program and starts at 56 KB.
# When that isn't enough it takes more from the system's pool: as much again as it already has, or the
# largest free piece if that's smaller, and it never gives any back. The system's pool is what Wi-Fi
# uses (about 100 KB with everything else) and what a secure connection needs: about 40 KB, of which
# 16 KB must be in one piece.
#
# This program needs about 55 KB, a little more than MicroPython starts with. Left alone, MicroPython
# takes another 56 KB to hold the few extra, which leaves the system 27 KB, and no secure connection can
# be made (measured on a reader: MBEDTLS_ERR_MPI_ALLOC_FAILED on every request).
#
# So, while the program loads, the memory for secure connections is held out of MicroPython's reach by
# setting up secure sockets that are never connected: each holds 20 KB. MicroPython then grows by what
# is left, which is enough, and the sockets are closed once everything is loaded.
RESERVE_MAX = 2
GROWTH_ROOM = 30000  # hold memory back while a free piece is larger than this...
LEAVE_FREE = 50000  # ...but leave this much in all, for MicroPython to grow into and for Wi-Fi to connect
ONE_CONNECTION = 21000


def _system_memory():
    """The system's pool: (bytes free, the largest free piece)."""
    import esp32

    regions = esp32.idf_heap_info(esp32.HEAP_DATA)
    return sum([region[1] for region in regions]), max([region[2] for region in regions])


def _log_memory(when):
    free, largest = _system_memory()
    used = gc.mem_alloc()
    log("memory %s: MicroPython %d of %d used; system %d free, largest piece %d"
        % (when, used, used + gc.mem_free(), free, largest))  # fmt: skip


def reserve():
    """Hold back the memory secure connections will need. Returns what to pass to release()."""
    held = []
    try:
        import socket
        import ssl

        _log_memory("before holding any back")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        while len(held) < RESERVE_MAX:
            free, largest = _system_memory()
            if largest <= GROWTH_ROOM or free - ONE_CONNECTION < LEAVE_FREE:
                break
            held.append(context.wrap_socket(socket.socket(), do_handshake_on_connect=False))
    except Exception as error:  # nothing more to hold: carry on with what there is
        log("reserving memory:", repr(error))
    _log_memory("with %d secure connections' worth held back" % len(held))
    return held


def _load():
    """Import the rest of the program, which is most of it."""
    from bbms_api import Api
    from flash import Flash

    gc.collect()
    from ota import Updater

    gc.collect()
    from reader import Reader

    return Api, Flash, Updater, Reader


def release(held):
    while held:
        try:
            held.pop().close()
        except Exception as error:
            print_exception(error)
    gc.collect()


def run():
    settings, problems = config.load()

    import hardware

    hw = hardware.Hardware(settings)  # the relay is off from here
    gc.collect()
    # Tidy up often. MicroPython's memory grows when it can't find room, by taking memory that Wi-Fi and
    # the secure connection to BBMS need, and it never gives any back: so don't let rubbish build up.
    gc.threshold(8192)

    from screen import Screen

    screen = Screen(hw.epd)
    if problems:
        log("can't start:", problems)
        screen.render(("message", ("Not set up",) + tuple(problems)))
        hw.epd.display()
        while True:  # until someone uploads a config.json and resets the reader
            hw.feed_watchdog()
            time.sleep(1)

    gc.collect()
    held = reserve()
    try:
        Api, Flash, Updater, Reader = _load()
    except MemoryError:
        # Too much was held back. Let go of it and load again: better short of memory for secure
        # connections than not running at all.
        log("not enough memory to load with that held back: loading again without")
        release(held)
        for name in ("bbms_api", "flash", "ota", "reader", "fobs", "offline", "version"):
            sys.modules.pop(name, None)
        gc.collect()
        Api, Flash, Updater, Reader = _load()
    _log_memory("with the program loaded")

    api = Api(
        settings["api_url"],
        settings["api_key"],
        ca_file=settings["ca_file"],
        tls_insecure=settings["tls_insecure"],
        timeout=settings["request_timeout_seconds"],
        link_up=hw.link_up,
    )
    reader = Reader(hw, api, Flash(config.DATA_DIR), screen, Updater(), settings["offline_use"])
    release(held)
    _log_memory("from here on")
    try:
        asyncio.run(reader.run())
    except Exception as error:
        print_exception(error)
    finally:
        hw.set_relay(False)
    # reader.run() never returns, so something has gone badly wrong: start again from scratch.
    time.sleep(5)
    hw.reboot()
