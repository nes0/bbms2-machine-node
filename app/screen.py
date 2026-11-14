# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# What the reader shows: drawing into the e-paper's buffer
#
# The buffer (display_base.py) is 250 columns of 16 bytes, left to right. In each column the first byte
# is the top 8 pixels, its most significant bit the topmost; a 0 bit is black. Only the top 122 pixels
# of each column are on the glass.
#
# The reader (reader.py) describes each screen as a tuple of plain values, a "view"; render() draws it.
#

from font import packed_ari_w9500_condensed_bold, packed_ari_w9500_condensed_display

W = 250
H = 122
STRIDE = 16  # bytes per column

LINE = 19  # from the top of one line of text to the top of the next
MARGIN = 4

# Characters the fonts don't have, and what to draw instead. Anything else becomes "?".
_STAND_INS = {}
for _group, _letter in (
    ("àáâãäå", "a"), ("ÀÁÂÃÄÅ", "A"), ("èéêë", "e"), ("ÈÉÊË", "E"), ("ìíîï", "i"), ("ÌÍÎÏ", "I"),
    ("òóôõöø", "o"), ("ÒÓÔÕÖØ", "O"), ("ùúûü", "u"), ("ÙÚÛÜ", "U"), ("ýÿ", "y"), ("Ý", "Y"),
    ("ñ", "n"), ("Ñ", "N"), ("ç", "c"), ("Ç", "C"), ("‘’", "'"), ("“”", '"'), ("–—", "-"),
):  # fmt: skip
    for _character in _group:
        _STAND_INS[_character] = _letter


class Font:
    """A packed font from process_fonts.py: ASCII 32 to 126, then the pound sign."""

    def __init__(self, module):
        self.rows = module.ROWS  # bytes per column of a glyph
        self.index = module.INDEX
        self.data = module.DATA

    def span(self, character):
        """The glyph's first column in the data, and the column after its last."""
        code = ord(character)
        if code == 0xA3:  # pound sign
            slot = 95
        elif 32 <= code <= 126:
            slot = code - 32
        else:
            slot = ord(_STAND_INS.get(character, "?")) - 32
        index = self.index
        return index[2 * slot] | index[2 * slot + 1] << 8, index[2 * slot + 2] | index[2 * slot + 3] << 8

    def width(self, text):
        total = 0
        for character in text:
            start, end = self.span(character)
            total += end - start
        return total

    def wrap(self, text, max_width, max_lines):
        """Split at spaces into lines that fit. Anything beyond max_lines is dropped."""
        lines = []
        line = ""
        for word in text.split():
            candidate = line + " " + word if line else word
            if line and self.width(candidate) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines[:max_lines]


class Screen:
    def __init__(self, epd):
        self.epd = epd
        self.bold = Font(packed_ari_w9500_condensed_display)
        self.plain = Font(packed_ari_w9500_condensed_bold)

    # ---- Drawing --------------------------------------------------------------------------------

    def clear(self):
        buffer = self.epd.buffer
        for i in range(len(buffer)):
            buffer[i] = 0xFF

    def fill(self, x, y, width, height, black=True):
        """A filled rectangle; the parts off the screen are left out."""
        x1 = min(x + width, W)
        y1 = min(y + height, H)
        x = max(x, 0)
        y = max(y, 0)
        if x >= x1 or y >= y1:
            return
        buffer = self.epd.buffer
        for row in range(y >> 3, ((y1 - 1) >> 3) + 1):
            top = row * 8
            mask = 0xFF
            if y > top:
                mask &= 0xFF >> (y - top)
            if y1 < top + 8:
                mask &= 0xFF << (top + 8 - y1)
            for i in range(x * STRIDE + row, x1 * STRIDE + row, STRIDE):
                if black:
                    buffer[i] &= mask ^ 0xFF
                else:
                    buffer[i] |= mask

    def text(self, font, x, y, text, white=False, right=W):
        """Draw text with its top-left corner at (x, y), stopping at column `right`. Returns where it ended."""
        buffer = self.epd.buffer
        data = font.data
        rows = font.rows
        first = y >> 3
        shift = 8 - (y & 7)
        for character in text:
            start, end = font.span(character)
            for column in range(start * rows, end * rows, rows):
                if x >= right:
                    return x
                if x >= 0:
                    base = x * STRIDE + first
                    for j in range(rows):
                        # The glyph's byte, as ink, moved down to straddle two of the column's bytes.
                        ink = (data[column + j] ^ 0xFF) << shift
                        if ink:
                            row = first + j
                            if 0 <= row < STRIDE and ink >> 8:
                                if white:
                                    buffer[base + j] |= ink >> 8
                                else:
                                    buffer[base + j] &= (ink >> 8) ^ 0xFF
                            if 0 <= row + 1 < STRIDE and ink & 0xFF:
                                if white:
                                    buffer[base + j + 1] |= ink & 0xFF
                                else:
                                    buffer[base + j + 1] &= (ink & 0xFF) ^ 0xFF
                x += 1
        return x

    def centred(self, font, centre, y, text, white=False):
        self.text(font, centre - font.width(text) // 2, y, text, white)

    def qr(self, code, x, y, scale):
        """A QR code in BBMS's raw format: the size, then rows of bits, most significant bit leftmost."""
        size = code[0]
        row_bytes = (size + 7) // 8
        for row in range(size):
            start = 1 + row * row_bytes
            for column in range(size):
                if code[start + (column >> 3)] & (0x80 >> (column & 7)):
                    self.fill(x + column * scale, y + row * scale, scale, scale)

    # ---- The screens ----------------------------------------------------------------------------

    def render(self, view, qr=None):
        """Draw a view into the buffer (the caller then sends the buffer to the display)."""
        self.clear()
        kind = view[0]
        if kind == "message":
            self._message(view[1])
        elif kind == "session":
            self._session(*view[1:])
        elif kind == "current":
            self._current(*view[1:])
        else:
            self._idle(kind, view[1:], qr)

    def _message(self, lines):
        pitch = LINE + 3
        y = (H - LINE - pitch * (len(lines) - 1)) // 2
        for i, line in enumerate(lines):
            self.centred(self.bold if i == 0 else self.plain, W // 2, y + i * pitch, line)

    def _session(self, machine, badge, member, time, charge, balance, overdrawn, offline):
        right = W - MARGIN
        if badge:
            width = self.plain.width(badge) + 8
            right = W - 2 - width
            self.fill(right, 1, width, LINE)
            self.text(self.plain, right + 4, 2, badge, white=True)
            right -= 4
        self.text(self.plain, MARGIN, 2, machine, right=right)
        self.fill(0, 21, W, 1)
        self.text(self.bold, MARGIN, 25, member, right=W - MARGIN)
        self.text(self.bold, MARGIN, 45, time)
        self.text(self.plain, MARGIN, 65, charge)
        if offline:
            self.text(self.plain, W - MARGIN - self.plain.width("offline"), 65, "offline")
        self.text(self.bold if overdrawn else self.plain, MARGIN, 84, balance)
        self.text(self.plain, MARGIN, 104, "Tap fob or press Stop to end")

    def _current(self, machine, amps, threshold, hint):
        """Live current, while an admin chooses the threshold: the reading, big, against the threshold."""
        badge = "LIVE CURRENT"
        width = self.plain.width(badge) + 8
        self.fill(W - 2 - width, 1, width, LINE)
        self.text(self.plain, W - width + 2, 2, badge, white=True)
        self.text(self.plain, MARGIN, 2, machine, right=W - 6 - width)
        self.fill(0, 21, W, 1)
        self.centred(self.bold, W // 2, 34, amps)
        self.centred(self.plain, W // 2, 62, threshold)
        self.centred(self.plain, W // 2, 100, hint)

    def _idle(self, kind, details, qr):
        # The QR code, if there is one, sits on the right, with "offline" beneath it.
        right = W - MARGIN
        offline_centre = W - 30
        if qr:
            scale = 3 if qr[0] * 3 <= 96 else 2
            size = qr[0] * scale
            x = W - 8 - size
            self.qr(qr, x, max(4, min((H - size) // 2, 103 - size)), scale)
            right = x - 4 * scale
            offline_centre = x + size // 2
        width = right - MARGIN
        plain = self.plain
        bold = self.bold

        offline = details[-1]
        if offline:
            self.centred(plain, offline_centre, H - LINE + 2, "offline")

        if kind == "notice":  # (title, detail, QR version, offline)
            self.text(bold, MARGIN, 2, details[0], right=right)
            for i, line in enumerate(plain.wrap(details[1], width, 4)):
                self.text(plain, MARGIN, 26 + i * LINE, line, right=right)
            return

        # Every other idle screen starts with the machine's name.
        for i, line in enumerate(bold.wrap(details[0], width, 2)):
            self.text(bold, MARGIN, 2 + i * LINE, line, right=right)
        if kind == "ready":  # (name, rate, minimum, QR version, offline)
            self.text(plain, MARGIN, 46, details[1], right=right)
            self.text(plain, MARGIN, 65, details[2], right=right)
            self.text(plain, MARGIN, 103, "Tap fob to start", right=right)
        elif kind == "broken":  # (name, note, QR version, offline)
            self.fill(MARGIN, 43, width, LINE + 1)
            self.centred(bold, MARGIN + width // 2, 45, "OUT OF ORDER", white=True)
            for i, line in enumerate(plain.wrap(details[1], width, 2)):
                self.text(plain, MARGIN, 66 + i * LINE, line, right=right)
        elif kind == "orphan":  # (name, member, QR version, offline)
            self.text(plain, MARGIN, 44, "Session open:", right=right)
            self.text(bold, MARGIN, 63, details[1], right=right)
            for i, line in enumerate(plain.wrap("Tap to resume, or press Stop", width, 2)):
                self.text(plain, MARGIN, 84 + i * LINE, line, right=right)
