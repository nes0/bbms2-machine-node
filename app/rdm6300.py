# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# RDM6300 125 kHz fob reader (9600 baud, 8N1)
#
# While a fob is in range the module keeps sending 14-byte frames:
#
#     0x02  "700012D687"  "33"  0x03
#           |             └─ checksum: the five data bytes combined with XOR, as 2 hex characters
#           └─ five data bytes as 10 hex characters: vendor prefix, then the card number
#
# The 12 hex characters are exactly BBMS's "full tag", which the v2 API accepts as "card".
#

STX = 0x02
ETX = 0x03

# A fob held on the reader is one tap: the same fob counts again only after it has been away this long.
AWAY_MS = 1500
# A tap nobody has acted on by now is forgotten (the reader was busy; the member will have tried again).
STALE_MS = 5000


class Rdm6300:
    def __init__(self, uart):
        self.uart = uart  # anything with any() and read(n), like machine.UART
        self._frame = None  # the characters since STX, or None when between frames
        self._last_tag = None
        self._last_seen = 0
        self._tap = None
        self._tap_at = 0

    def poll(self, now):
        """Take in whatever has arrived. Call this often (now: milliseconds, see util.now_ms)."""
        waiting = self.uart.any()
        if waiting:
            data = self.uart.read(waiting)
            if data:
                self.feed(data, now)

    def feed(self, data, now):
        for byte in data:
            if byte == STX:
                self._frame = bytearray()
            elif self._frame is None:
                pass  # noise between frames
            elif byte == ETX:
                tag = self._check(self._frame)
                self._frame = None
                if tag:
                    self._seen(tag, now)
            elif len(self._frame) < 12:
                self._frame.append(byte)
            else:
                self._frame = None  # too long to be a frame

    @staticmethod
    def _check(frame):
        """The tag as 12 upper-case hex characters, or None if the frame is damaged."""
        if len(frame) != 12:
            return None
        try:
            text = bytes(frame).decode().upper()
            value = int(text, 16)
        except ValueError:  # includes bytes that aren't text
            return None
        checksum = 0
        for _ in range(6):  # XOR of all six bytes is 0 when the last one is the checksum of the rest
            checksum ^= value & 0xFF
            value >>= 8
        return text if checksum == 0 else None

    def _seen(self, tag, now):
        if tag != self._last_tag or now - self._last_seen > AWAY_MS:
            self._tap = tag
            self._tap_at = now
        self._last_tag = tag
        self._last_seen = now

    def take(self, now):
        """The fob tapped since the last call, or None."""
        tap = self._tap
        self._tap = None
        if tap and now - self._tap_at > STALE_MS:
            return None
        return tap

    def discard(self):
        self._tap = None
