# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# Pretend hardware for running the reader program on a computer: the stand-in for hardware.py
#


from rdm6300 import ETX, STX, Rdm6300


class SimUart:
    """What the RDM6300 has sent and nobody has read yet."""

    def __init__(self):
        self.waiting = b""

    def any(self):
        return len(self.waiting)

    def read(self, count):
        data, self.waiting = self.waiting[:count], self.waiting[count:]
        return data


def frame(card):
    """What an RDM6300 sends for a fob, given its 12-hex tag or the decimal card number printed on it."""
    card = card.strip().upper()
    if len(card) != 12:  # a card number: make it a tag with vendor prefix 70
        number = int(card)
        data = bytes([0x70]) + number.to_bytes(4, "big")
        checksum = 0
        for byte in data:
            checksum ^= byte
        card = "".join("%02X" % byte for byte in data + bytes([checksum]))
    return bytes([STX]) + card.encode() + bytes([ETX])


class SimHardware:
    def __init__(self, epd, on_relay=None):
        self.epd = epd
        self.on_relay = on_relay  # called with True or False when the relay changes
        self.relay = False
        self.current = 0.0  # what the machine would draw if it had power
        self.network = True
        self.rebooted = False
        self._uart = SimUart()
        self._rfid = Rdm6300(self._uart)
        self._stop = False
        self._fault = False

    # ---- The bench: what a person does to the reader --------------------------------------------

    def tap(self, card):
        self._uart.waiting += frame(card)

    def press_stop(self):
        self._stop = True

    def press_fault(self):
        self._fault = True

    # ---- What reader.py uses (the same as hardware.Hardware) --------------------------------------

    def set_relay(self, on):
        if on != self.relay:
            self.relay = on
            if self.on_relay:
                self.on_relay(on)

    def amps(self):
        return self.current if self.relay else 0.0

    def poll(self, now):
        self._rfid.poll(now)

    def take_card(self, now):
        return self._rfid.take(now)

    def take_stop(self):
        pressed, self._stop = self._stop, False
        return pressed

    def take_fault(self):
        pressed, self._fault = self._fault, False
        return pressed

    def discard_inputs(self):
        self._rfid.discard()
        self._stop = False
        self._fault = False

    def link_up(self):
        return self.network

    def feed_watchdog(self):
        pass

    def reboot(self):
        self.set_relay(False)
        self.rebooted = True
        raise SystemExit("the reader was told to reboot")
