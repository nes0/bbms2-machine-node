"""
The fob reader's frames (app/rdm6300.py): what counts as one tap. Run with `python -m unittest discover tests`.

The fob is the made-up one from BBMS's documents: tag 700012D68733.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from rdm6300 import Rdm6300  # noqa: E402

KIM = b"\x02700012D68733\x03"
SAM = b"\x0270000003E89B\x03"


class FakeUart:
    def __init__(self):
        self.waiting = b""

    def any(self):
        return len(self.waiting)

    def read(self, count):
        data, self.waiting = self.waiting[:count], self.waiting[count:]
        return data


class Taps(unittest.TestCase):
    def setUp(self):
        self.uart = FakeUart()
        self.rfid = Rdm6300(self.uart)
        self.now = 0

    def run_for(self, ms, frame=None, every=30):
        """Look every 30 ms, as the reader does. With a frame: a fob is there, sending it each time."""
        taps = 0
        for _ in range(ms // every):
            self.now += every
            if frame:
                self.uart.waiting += frame
            taps += self.rfid.poll(self.now)
        return taps

    def test_a_fob_held_there_is_one_tap(self):
        self.assertEqual(self.run_for(6000, KIM), 1)
        self.assertEqual(self.rfid.take(self.now), None)  # and that was 6 seconds ago: too old to act on

    def test_the_tap_is_handed_over_once(self):
        self.run_for(300, KIM)
        self.assertEqual(self.rfid.take(self.now), "700012D68733")
        self.assertEqual(self.rfid.take(self.now), None)

    def test_the_same_fob_counts_again_after_going_away(self):
        self.assertEqual(self.run_for(300, KIM), 1)
        self.assertEqual(self.run_for(1000), 0)
        self.assertEqual(self.run_for(300, KIM), 0)  # a second away isn't long enough
        self.assertEqual(self.run_for(2000), 0)
        self.assertEqual(self.run_for(300, KIM), 1)

    def test_another_fob_counts_straight_away(self):
        self.assertEqual(self.run_for(300, KIM), 1)
        self.assertEqual(self.run_for(300, SAM), 1)

    def test_a_reader_that_was_held_up_doesnt_see_a_second_tap(self):
        self.assertEqual(self.run_for(300, KIM), 1)
        self.rfid.take(self.now)
        # Three seconds without looking, the fob there throughout: its frames are all read at the end.
        self.now += 3000
        self.uart.waiting += KIM * 18
        self.assertFalse(self.rfid.poll(self.now))
        self.assertEqual(self.run_for(300, KIM), 0)
        self.assertEqual(self.rfid.take(self.now), None)

    def test_a_fob_still_there_after_a_discard_isn_t_a_new_tap(self):
        self.run_for(300, KIM)
        self.assertEqual(self.run_for(3000), 0)  # nothing heard: the screen changing can drown the fob out
        self.rfid.discard()
        self.assertEqual(self.run_for(300, KIM), 0)

    def test_damaged_frames_are_ignored(self):
        self.uart.waiting += (b"\x02700012D68734\x03" + b"\x02700012D6\x03" + b"junk") * 2
        self.assertFalse(self.rfid.poll(10))
        self.assertEqual(self.rfid.take(10), None)

    def test_one_frame_alone_isnt_believed(self):
        # Interference that happens to pass the checksum: once, or twice with different numbers.
        self.uart.waiting += SAM
        self.assertFalse(self.rfid.poll(10))
        self.assertEqual(self.run_for(3000), 0)
        self.uart.waiting += SAM  # the same again, but long after
        self.assertFalse(self.rfid.poll(self.now))
        self.uart.waiting += KIM
        self.assertFalse(self.rfid.poll(self.now))
        self.assertEqual(self.rfid.take(self.now), None)

    def test_two_frames_running_are_a_tap(self):
        self.uart.waiting += KIM
        self.assertFalse(self.rfid.poll(10))
        self.uart.waiting += KIM
        self.assertTrue(self.rfid.poll(80))
        self.assertEqual(self.rfid.take(80), "700012D68733")

    def test_a_stray_frame_doesnt_make_a_held_fob_tap_again(self):
        self.assertEqual(self.run_for(300, KIM), 1)
        self.uart.waiting += SAM
        self.assertEqual(self.run_for(300, KIM), 0)

    def test_all_zeros_is_never_a_fob(self):
        self.assertEqual(self.run_for(300, b"\x02000000000000\x03"), 0)


if __name__ == "__main__":
    unittest.main()
