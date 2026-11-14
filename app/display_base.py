# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# Electronic Paper Display abstraction
#


class EPD:
    EPD_WIDTH = 250  # visible
    EPD_HEIGHT = 122
    EPD_STRIDE = 16  # bytes per column (128 pixels)

    ALLSCREEN_BYTES = 4000

    def __init__(self):
        self.buffer = bytearray(self.ALLSCREEN_BYTES).replace(b"\x00", b"\xff")
        self.init()

    def reset(self):
        pass

    def init(self):
        pass

    def clear(self):
        self.buffer = bytearray(self.ALLSCREEN_BYTES).replace(b"\x00", b"\xff")

    def pixel(self, x, y, color=1):
        if not (0 <= y < self.EPD_WIDTH and 0 <= x < self.EPD_HEIGHT):
            return

        idx = x * self.EPD_STRIDE + (y >> 3)
        mask = 0x80 >> (y & 7)

        if color:
            self.buffer[idx] &= ~mask  # black
        else:
            self.buffer[idx] |= mask  # white

    def draw_char(self, font, x, y, ch):
        glyph = font.get(ch, font[" "])
        self.copy_block(x, y, glyph)
        return len(glyph)

    def draw_text(self, font, x, y, text):
        for ch in text:
            x += self.draw_char(font, x, y, ch)

    def display(self):
        pass

    async def display_async(self):
        """Like display(), but other tasks carry on while the panel updates. False if it didn't finish."""
        self.display()
        return True

    def copy_block(self, x, y, data):
        try:
            for i, column in enumerate(data, x):
                for j, b in enumerate(column, y // 8):
                    self.buffer[i * self.EPD_STRIDE + j] = b
        except IndexError:
            pass
