# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# What the reader must remember through a power cut: the meter sequence number, meter readings and
# sessions not yet sent, the machine's details and the QR code. Small values are kept together in one
# JSON file; the QR code has its own file, and so does the fob list (fobs.py).
#

import json
import os


class Flash:
    def __init__(self, directory):
        self.directory = directory
        try:
            os.mkdir(directory)
        except OSError:
            pass  # it's there already
        self._path = directory + "/state.json"
        self._values = {}
        try:
            with open(self._path) as f:
                self._values = json.load(f)
        except (OSError, ValueError):
            pass  # first start, or a damaged file: start again

    def get(self, name, default=None):
        return self._values.get(name, default)

    def set(self, name, value):
        self.update({name: value})

    def update(self, values):
        """Change several values with one write to the flash."""
        self._values.update(values)
        self._replace(self._path, json.dumps(self._values).encode())

    def read_file(self, name):
        try:
            with open(self.directory + "/" + name, "rb") as f:
                return f.read()
        except OSError:
            return None

    def write_file(self, name, data):
        self._replace(self.directory + "/" + name, data)

    def _replace(self, path, data):
        # Write beside the old file, then swap, so a power cut mid-write leaves the old file intact.
        with open(path + ".new", "wb") as f:
            f.write(data)
        try:
            os.rename(path + ".new", path)
        except OSError:  # a filesystem that won't rename over a file
            os.remove(path)
            os.rename(path + ".new", path)
