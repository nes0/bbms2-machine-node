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
# "Away" is time in which the reader looked and nothing had arrived. Time in which it couldn't look (it
# was held up setting up a secure connection, say) doesn't count: the frames sent meanwhile are all read
# together afterwards, and by the clock alone that looks like a fob that went away and came back.
AWAY_MS = 1500
QUIET_STEP_MS = 100  # the most that one look with nothing there adds to the time away
# A fob is believed when two frames running say the same, no further apart than this. The checksum is
# one byte, so interference that looks like a frame gets past it one time in 256, as a fob nobody
# holds: but not twice running with the same number. A real fob sends several frames a second.
CONFIRM_MS = 1000
NO_TAG = "000000000000"  # passes the checksum, and isn't a fob
# A tap nobody has acted on by now is forgotten (the reader was busy; the member will have tried again).
STALE_MS = 5000


class Rdm6300:
    def __init__(self, uart):
        self.uart = uart  # anything with any() and read(n), like machine.UART
        self._frame = None  # the characters since STX, or None when between frames
        self._heard = None  # the tag in the last good frame, believed or not
        self._heard_at = 0
        self._last_tag = None  # the last fob believed
        self._quiet = 0  # how long the reader has looked and heard nothing, in milliseconds
        self._polled = None
        self._tap = None
        self._tap_at = 0

    def poll(self, now):
        """
        Take in whatever has arrived. Call this often (now: milliseconds, see util.now_ms).
        Returns True if that was a new tap.
        """
        tapped = False
        waiting = self.uart.any()
        if waiting:
            data = self.uart.read(waiting)
            if data:
                tapped = self.feed(data, now)
        elif self._polled is not None:
            self._quiet += min(now - self._polled, QUIET_STEP_MS)
        self._polled = now
        return tapped

    def feed(self, data, now):
        tapped = False
        for byte in data:
            if byte == STX:
                self._frame = bytearray()
            elif self._frame is None:
                pass  # noise between frames
            elif byte == ETX:
                tag = self._check(self._frame)
                self._frame = None
                if tag and self._seen(tag, now):
                    tapped = True
            elif len(self._frame) < 12:
                self._frame.append(byte)
            else:
                self._frame = None  # too long to be a frame
        return tapped

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
        """A good frame has arrived. True if it makes a new tap."""
        if tag == NO_TAG:
            return False
        confirmed = tag == self._heard and now - self._heard_at <= CONFIRM_MS
        self._heard = tag
        self._heard_at = now
        if not confirmed:
            return False  # wait for the next frame to say the same
        tapped = tag != self._last_tag or self._quiet > AWAY_MS
        if tapped:
            self._tap = tag
            self._tap_at = now
        self._last_tag = tag
        self._quiet = 0
        return tapped

    def take(self, now):
        """The fob tapped since the last call, or None."""
        tap = self._tap
        self._tap = None
        if tap and now - self._tap_at > STALE_MS:
            return None
        return tap

    def discard(self):
        """Forget a tap nobody has acted on. A fob that is still there has to go away before it counts again."""
        self._tap = None
        self._quiet = 0
