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
import time

import config
from util import log, print_exception


def run():
    settings, problems = config.load()

    import hardware

    hw = hardware.Hardware(settings)  # the relay is off from here
    gc.collect()

    from screen import Screen

    screen = Screen(hw.epd)
    if problems:
        log("can't start:", problems)
        screen.render(("message", ("Not set up",) + tuple(problems)))
        hw.epd.display()
        while True:  # until someone uploads a config.json and resets the reader
            hw.feed_watchdog()
            time.sleep(1)

    from bbms_api import Api
    from flash import Flash
    from ota import Updater
    from reader import Reader

    api = Api(
        settings["api_url"],
        settings["api_key"],
        ca_file=settings["ca_file"],
        tls_insecure=settings["tls_insecure"],
        timeout=settings["request_timeout_seconds"],
        link_up=hw.link_up,
    )
    reader = Reader(hw, api, Flash(config.DATA_DIR), screen, Updater(), settings["offline_use"])
    gc.collect()
    try:
        asyncio.run(reader.run())
    except Exception as error:
        print_exception(error)
    finally:
        hw.set_relay(False)
    # reader.run() never returns, so something has gone badly wrong: start again from scratch.
    time.sleep(5)
    hw.reboot()
