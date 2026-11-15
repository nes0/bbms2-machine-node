"""The check made before a release (release_check.py). Run with `python -m unittest discover tests`."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from release_check import problem  # noqa: E402


class ReleaseCheck(unittest.TestCase):
    def test_the_first_release(self):
        self.assertIsNone(problem("0.2.0", []))

    def test_a_higher_version_passes(self):
        self.assertIsNone(problem("0.10.0", ["v0.2.0", "v0.9.1"]))  # compared as numbers, not as text
        self.assertIsNone(problem("1.0", ["v0.9.1", "not-a-release", "vague"]))

    def test_the_same_version_again_is_refused(self):
        self.assertIn("released already", problem("0.2.0", ["v0.1.0", "v0.2.0"]))

    def test_a_lower_version_is_refused(self):
        self.assertIn("not higher than 0.10.0", problem("0.9.0", ["v0.10.0", "v0.2.0"]))

    def test_a_release_from_a_tag(self):
        self.assertIsNone(problem("0.3.0", ["v0.2.0", "v0.3.0"], tag="v0.3.0"))
        self.assertIn("should be v0.3.0", problem("0.3.0", ["v0.2.0", "v0.4.0"], tag="v0.4.0"))
        self.assertIn("not higher", problem("0.3.0", ["v0.5.0", "v0.3.0"], tag="v0.3.0"))

    def test_a_version_that_cant_be_compared_is_refused(self):
        self.assertIn("numbers and dots", problem("0.3-beta", []))


if __name__ == "__main__":
    unittest.main()
