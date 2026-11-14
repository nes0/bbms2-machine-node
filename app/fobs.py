# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# The fobs this reader may accept while BBMS can't be reached: BBMS's "allowed-fobs" list, kept in flash.
#
# For a machine it is the members who could start a session if credit and the machine's status are left
# aside (docs/device-api-v2.md in the BBMS repository). The list could run to hundreds of fobs, which is
# more than the ESP32 can hold in memory as JSON, so the reply is downloaded to a file, read through an
# entry at a time, and kept as one line of text for each fob:
#
#     card number <tab> full tag <tab> name
#
# Looking a fob up reads through that file. Nothing is kept in memory.
#

import json
import os

MAX_BYTES = 200000  # a reply longer than this isn't a fob list
MAX_ENTRY = 500


def scan(source, each):
    """
    Read a JSON reply shaped like {"cache_version": "...", "fobs": [{...}, {...}]} from a file opened
    for reading bytes. Calls each(fob) for every entry in turn, and returns the reply with its entries
    taken out. Raises ValueError if it isn't JSON.
    """
    outer = bytearray()
    entry = None  # the fob being read
    depth = 0
    quoted = False
    escaped = False
    while True:
        block = source.read(256)
        if not block:
            break
        for byte in block:
            if entry is not None:
                entry.append(byte)
                if len(entry) > MAX_ENTRY:
                    raise ValueError("an entry is too long")
            elif depth < 2:  # between the entries there are only commas and spaces, which are dropped
                outer.append(byte)
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 0x5C:  # a backslash
                    escaped = True
                elif byte == 0x22:
                    quoted = False
            elif byte == 0x22:  # a double quote
                quoted = True
            elif byte == 0x7B or byte == 0x5B:  # { or [
                depth += 1
                if depth == 3 and byte == 0x7B:
                    entry = bytearray(b"{")
            elif byte == 0x7D or byte == 0x5D:  # } or ]
                depth -= 1
                if depth == 2 and entry is not None:
                    each(json.loads(bytes(entry)))
                    entry = None
                elif depth == 1:
                    outer.append(byte)  # the end of the list
    return json.loads(bytes(outer))


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class FobList:
    def __init__(self, path):
        self.path = path

    def find(self, tag):
        """The name of the member whose fob this is (which may be ""), or None if it isn't on the list."""
        tag = tag.upper().encode()
        try:
            # As BBMS matches them: by the full tag, or by the card number inside it.
            number = ("%010d" % int(tag[2:10], 16)).encode() if len(tag) == 12 else None
        except ValueError:
            number = None
        try:
            with open(self.path, "rb") as f:
                for line in f:
                    parts = line.rstrip(b"\n").split(b"\t")
                    if len(parts) == 3 and (parts[1] == tag or (number and parts[0] == number)):
                        return parts[2].decode()
        except OSError:
            pass  # there's no list
        return None

    def store(self, reply_path):
        """
        Replace the list with BBMS's reply, which has been downloaded to the file `reply_path` (removed
        afterwards). Returns (the list's version, how many fobs). If the reply is no good it raises
        ValueError or OSError and the old list stays.
        """
        count = [0]
        new = self.path + ".new"
        try:
            with open(reply_path, "rb") as source:
                with open(new, "wb") as out:

                    def each(fob):
                        card = fob.get("card") or ""
                        tag = (fob.get("tag") or "").upper()
                        name = " ".join((fob.get("display_name") or "").split())  # no tabs or line ends
                        out.write(("%s\t%s\t%s\n" % (card, tag, name)).encode())
                        count[0] += 1

                    outer = scan(source, each)
            version = outer.get("cache_version") if isinstance(outer, dict) else None
            if not version or not isinstance(version, str) or "fobs" not in outer:
                raise ValueError("not a fob list")
        except Exception:
            _remove(new)
            raise
        finally:
            _remove(reply_path)
        # Swap, so a power cut leaves either the old list or the new one.
        try:
            os.rename(new, self.path)
        except OSError:  # a filesystem that won't rename over a file
            os.remove(self.path)
            os.rename(new, self.path)
        return version, count[0]

    def clear(self):
        _remove(self.path)
