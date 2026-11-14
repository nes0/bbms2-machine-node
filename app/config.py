# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# Settings for one reader. The defaults are below; data/config.json on the reader overrides them
# (start from config.example.json). The Wi-Fi password and the API key live only in that file.
#

import json

from util import MICROPYTHON

DATA_DIR = "data"
CONFIG_FILE = DATA_DIR + "/config.json"

DEFAULTS = {
    "wifi_ssid": "",
    "wifi_password": "",
    # The v2 device API (docs/device-api-v2.md in the BBMS repository), ending in a slash.
    "api_url": "https://bbms.buildbrighton.com/api/devices/v2/",
    # This reader's key, from the device's page in the BBMS admin.
    "api_key": "",
    # The certificate authority that signed BBMS's certificate, as a DER file. https is refused without
    # it, unless "tls_insecure" is true (bench testing only: the key could then be sent to an impostor).
    "ca_file": DATA_DIR + "/ca.der",
    "tls_insecure": False,
    "request_timeout_seconds": 10,
    # While BBMS can't be reached, let members on the fob list it gave earlier use the machine. Their
    # credit can't be checked then; BBMS charges them when it hears of the session. False: nobody can
    # tap in without BBMS.
    "offline_use": True,
    # GPIO numbers. The e-paper's are in epaper213.py (4, 5, 16, 17, 18, 23).
    "pin_rfid_rx": 27,  # from the RDM6300's TX, through a divider: the RDM6300 is a 5 V part
    "pin_rfid_tx": 25,  # not connected, but the UART must be given a pin that isn't used elsewhere
    "pin_relay": 26,
    "relay_active_high": True,
    "pin_stop_button": 32,  # the buttons connect these to ground; the internal pull-ups are used
    "pin_fault_button": 33,
    "pin_current_sensor": 34,  # must be an ADC1 pin (32-39): ADC2 doesn't work while Wi-Fi is on
    # Current sensor (the circuit is in current_sensor/README.md): the pin's voltage rises by
    # current_mv_per_amp for each amp the machine draws, from current_zero_mv with the machine off.
    "current_mv_per_amp": 61.6,
    # Only a starting point: the reader measures the zero itself whenever the relay is off. A measured
    # zero further than current_zero_tolerance_mv from this is ignored and logged as a fault.
    "current_zero_mv": 290,
    "current_zero_tolerance_mv": 150,
    # Restart the reader if its main loop stops for this long (0: no watchdog).
    "watchdog_seconds": 60,
}


def load(path=CONFIG_FILE):
    """The settings, and a list of problems that stop the reader from working ([] if it can start)."""
    settings = dict(DEFAULTS)
    problems = []
    try:
        with open(path) as f:
            stored = json.load(f)
        for name in stored:
            if name in DEFAULTS:
                settings[name] = stored[name]
            else:
                print("config: unknown setting", name)
    except OSError:
        problems.append("No " + path)
    except ValueError:
        problems.append("Bad " + path)
    if not problems:
        if not settings["api_key"]:
            problems.append("No API key")
        if not settings["api_url"].endswith("/") or "://" not in settings["api_url"]:
            problems.append("Bad API address")
        if MICROPYTHON and not settings["wifi_ssid"]:  # the simulation uses the computer's own network
            problems.append("No Wi-Fi name")
    return settings, problems
