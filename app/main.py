# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# The loader: what the ESP32 runs at power-up. It puts a downloaded firmware update in place, starts the
# reader (start.py), and puts the old firmware back if the new one doesn't work.
#
# This is the one file an over-the-air update never replaces, and it uses nothing else from the firmware,
# so a bad update can't stop it putting things right. Keep it small. It can only be changed over USB.
#
# How an update goes (ota.py does the first step; BBMS's side is in docs/device-api-v2.md there):
#   1. The running reader downloads the files that differ into /ota/new, checks each against its
#      SHA-256, writes /ota/pending.json and restarts.
#   2. The loader moves each old file to /ota/old and each new one into place, and writes
#      /ota/trial.json: the new firmware is now on trial.
#   3. When the new firmware has checked in with BBMS it deletes trial.json and /ota/old (ota.py).
#   4. If instead it fails to start, or has started MAX_TRIAL_STARTS times without reaching BBMS, the
#      loader moves the old files back and writes /ota/failed.json, which the reader reports to BBMS.
# Every step can be cut short by a power cut and carried on at the next start.
#

import json
import os
import sys

OTA = "/ota"
MAX_TRIAL_STARTS = 3


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _make_folders(path):
    """The folders that the file `path` needs."""
    parts = path.split("/")[:-1]
    for count in range(1, len(parts) + 1):
        folder = "/".join(parts[:count])
        if folder:
            try:
                os.mkdir(folder)
            except OSError:
                pass  # it's there already


def _move(source, target):
    _make_folders(target)
    _remove(target)
    os.rename(source, target)


def _write(path, value):
    _make_folders(path)
    with open(path + ".tmp", "w") as f:
        json.dump(value, f)
    _move(path + ".tmp", path)


def apply_pending(root=""):
    """Put a downloaded update in place. True if there was one."""
    ota = root + OTA
    pending = _read(ota + "/pending.json")
    if not pending:
        return False
    for path in pending["replace"]:
        new = ota + "/new/" + path
        if _exists(new):  # otherwise it was moved before a power cut
            current = root + "/" + path
            old = ota + "/old/" + path
            if _exists(current) and not _exists(old):
                _move(current, old)
            _move(new, current)
    for path in pending["remove"]:
        current = root + "/" + path
        if _exists(current):
            _move(current, ota + "/old/" + path)
    # A file with nothing in /ota/old is one the old firmware didn't have: going back means deleting it.
    pending["added"] = [path for path in pending["replace"] if not _exists(ota + "/old/" + path)]
    pending["starts"] = 0
    _write(ota + "/trial.json", pending)
    _remove(ota + "/pending.json")
    return True


def roll_back(root, error):
    """Put the old firmware back and leave a note of why for the reader to send to BBMS."""
    ota = root + OTA
    trial = _read(ota + "/trial.json")
    if not trial:
        return
    for path in trial["replace"] + trial["remove"]:
        old = ota + "/old/" + path
        if _exists(old):
            _move(old, root + "/" + path)
        elif path in trial["added"]:
            _remove(root + "/" + path)
    _write(ota + "/failed.json", {"version": trial["version"], "error": str(error)[:200]})
    _remove(ota + "/trial.json")


def count_trial_start(root=""):
    """Called at each start. True if the firmware on trial has had its chances and was rolled back."""
    path = root + OTA + "/trial.json"
    trial = _read(path)
    if not trial:
        return False
    if trial["starts"] >= MAX_TRIAL_STARTS:
        roll_back(root, "started %d times without reaching BBMS" % trial["starts"])
        return True
    trial["starts"] += 1
    _write(path, trial)
    return False


def boot():
    import time

    import machine

    try:
        if apply_pending():
            print("loader: firmware update put in place")
        if count_trial_start():
            print("loader: the new firmware didn't work; the old one is back")
            machine.reset()
    except Exception as error:  # carry on with whatever is there: stopping here helps nobody
        sys.print_exception(error)

    try:
        import start

        start.run()
    except Exception as error:
        sys.print_exception(error)
        try:
            if _read(OTA + "/trial.json"):
                roll_back("", "failed to start: " + repr(error))
                print("loader: the new firmware failed; the old one is back")
        except Exception as second:
            sys.print_exception(second)
    # start.run() never returns unless something has gone badly wrong: start again from scratch.
    time.sleep(5)
    machine.reset()


if __name__ == "__main__":
    boot()
