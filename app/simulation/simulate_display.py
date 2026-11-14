# SPDX-License-Identifier: GPL-2.0-or-later

#
# Copyright 2026 Neil Stainton <nes0@eml.cc>
#
# RFID tag machine access
#
# Display simulation (terminal mode Textual widget)
#

import math

import display_base
from textual.reactive import reactive
from textual.widget import Widget

WIDTH = 250
HEIGHT = 122
BYTES_PER_COL = (HEIGHT + 7) // 8
EXPECTED_SIZE = WIDTH * BYTES_PER_COL


# Braille dot bit positions
# (dx, dy) -> dot number
BRAILLE_MAP = {
    (0, 0): 1,
    (0, 1): 2,
    (0, 2): 3,
    (0, 3): 7,
    (1, 0): 4,
    (1, 1): 5,
    (1, 2): 6,
    (1, 3): 8,
}


def get_pixel(buf, x, y):
    """Return 0 or 1 for pixel (x,y)."""
    if x < 0 or x >= WIDTH or y < 0 or y >= HEIGHT:
        return 1
    byte_index = x * BYTES_PER_COL + (y // 8)
    bit_index = 7 - (y % 8)  # LSB = top pixel
    return (buf[byte_index] >> bit_index) & 1


def framebuffer_to_braille(buf):
    braille_rows = (HEIGHT + 3) // 4
    braille_cols = (WIDTH + 1) // 2

    lines = []

    for by in range(braille_rows):
        row = []
        for bx in range(braille_cols):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    px = bx * 2 + dx
                    py = by * 4 + dy
                    if not get_pixel(buf, px, py):
                        dot = BRAILLE_MAP[(dx, dy)]
                        bits |= 1 << (dot - 1)
            row.append(chr(0x2800 + bits))
        lines.append("".join(row))

    return lines


class BrailleDisplay(Widget):
    framebuffer: bytes = reactive(b"")

    def render(self) -> str:
        if not self.framebuffer:
            return "No framebuffer"

        lines = framebuffer_to_braille(self.framebuffer)
        width = len(lines[0])
        return "\n".join(lines)


class EPD(display_base.EPD):
    def __init__(self):
        super().__init__()
        self.widget = BrailleDisplay()

    def reset(self):
        pass

    def init(self):
        pass

    def display(self):
        self.widget.framebuffer = bytes(self.buffer)  # a copy: the widget redraws only when given a new value
