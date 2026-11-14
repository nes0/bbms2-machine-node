"""
Firmware updates, tested on a computer: the loader (app/main.py) and the downloader (app/ota.py) working
on a folder that stands in for the ESP32's flash. Run with `python -m unittest discover tests`.
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main as loader  # noqa: E402
import ota  # noqa: E402
from bbms_api import Result  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeBbms:
    """Stands in for bbms_api.Api: serves one release."""

    def __init__(self, version, files):
        self.version = version
        self.files = files  # {path: bytes}
        self.downloads = []
        self.damage = None  # a path to serve with a byte changed

    def manifest(self):
        return {
            "version": self.version,
            "files": [{"path": p, "size": len(d), "sha256": sha(d)} for p, d in sorted(self.files.items())],
        }

    async def request(self, method, path, body=None, etag=None, binary=False):
        assert (method, path) == ("GET", "firmware/manifest")
        return Result(200, self.manifest())

    async def download(self, path, save_as, timeout):
        wanted = path.rsplit("/", 1)[1]
        for name, data in self.files.items():
            if sha(data) == wanted:
                self.downloads.append(name)
                if name == self.damage:
                    data = b"X" + data[1:]
                with open(save_as, "wb") as f:
                    f.write(data)
                return Result(200, (len(data), sha(data)))
        return Result(404)


class OtaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = self.folder.name
        self.put("main.py", b"the loader")
        self.put("reader.py", b"reader 1")
        self.put("version.py", b'VERSION = "1"')
        self.put("font/a.py", b"font a")
        self.put("data/state.json", b"{}")
        self.release = {
            "reader.py": b"reader 2",
            "version.py": b'VERSION = "2"',
            "font/a.py": b"font a",
            "extra.py": b"new",
        }

    def tearDown(self):
        self.folder.cleanup()

    def put(self, path, data):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def get(self, path):
        try:
            with open(os.path.join(self.root, path), "rb") as f:
                return f.read()
        except OSError:
            return None

    def fetch(self, bbms, version="2"):
        asyncio.run(ota.Updater(self.root).fetch(bbms, version))

    def install(self, release=None, version="2"):
        """Download, restart into the loader, and have the new firmware check in."""
        self.fetch(FakeBbms(version, release or self.release), version)
        self.assertTrue(loader.apply_pending(self.root))
        loader.count_trial_start(self.root)
        updater = ota.Updater(self.root)
        self.assertTrue(updater.on_trial)
        updater.confirm()

    def test_only_changed_files_are_downloaded(self):
        bbms = FakeBbms("2", self.release)
        self.fetch(bbms)
        self.assertEqual(sorted(bbms.downloads), ["extra.py", "reader.py", "version.py"])
        self.assertEqual(self.get("reader.py"), b"reader 1")  # nothing changes until the restart

    def test_a_good_update_is_kept(self):
        self.install()
        self.assertEqual(self.get("reader.py"), b"reader 2")
        self.assertEqual(self.get("extra.py"), b"new")
        self.assertEqual(self.get("main.py"), b"the loader")
        self.assertEqual(self.get("data/state.json"), b"{}")
        self.assertFalse(os.path.exists(os.path.join(self.root, "ota/old")))
        self.assertFalse(ota.Updater(self.root).on_trial)
        self.assertFalse(loader.count_trial_start(self.root))

    def test_a_file_dropped_from_the_next_release_is_removed(self):
        self.install()
        third = {"reader.py": b"reader 3", "version.py": b'VERSION = "3"', "font/a.py": b"font a"}
        self.install(third, "3")
        self.assertIsNone(self.get("extra.py"))
        self.assertEqual(self.get("reader.py"), b"reader 3")

    def test_a_firmware_that_never_checks_in_is_rolled_back(self):
        self.fetch(FakeBbms("2", self.release))
        loader.apply_pending(self.root)
        self.assertEqual(self.get("reader.py"), b"reader 2")
        for _ in range(loader.MAX_TRIAL_STARTS):
            self.assertFalse(loader.count_trial_start(self.root))
        self.assertTrue(loader.count_trial_start(self.root))
        self.assertEqual(self.get("reader.py"), b"reader 1")
        self.assertEqual(self.get("version.py"), b'VERSION = "1"')
        self.assertIsNone(self.get("extra.py"))
        updater = ota.Updater(self.root)
        self.assertFalse(updater.on_trial)
        self.assertEqual(updater.failure()["version"], "2")

    def test_rolling_back_restores_a_removed_file(self):
        self.install()
        third = {"reader.py": b"reader 3", "version.py": b'VERSION = "3"', "font/a.py": b"font a"}
        self.fetch(FakeBbms("3", third), "3")
        loader.apply_pending(self.root)
        self.assertIsNone(self.get("extra.py"))
        loader.roll_back(self.root, "it broke")
        self.assertEqual(self.get("extra.py"), b"new")
        self.assertEqual(self.get("reader.py"), b"reader 2")
        self.assertEqual(ota.Updater(self.root).failure(), {"version": "3", "error": "it broke"})

    def test_a_damaged_download_changes_nothing(self):
        bbms = FakeBbms("2", self.release)
        bbms.damage = "reader.py"
        with self.assertRaises(ota.UpdateError):
            self.fetch(bbms)
        self.assertFalse(loader.apply_pending(self.root))
        self.assertEqual(self.get("reader.py"), b"reader 1")

    def test_a_manifest_naming_the_loader_or_a_path_outside_is_refused(self):
        for path in ("main.py", "boot.py", "data/config.py", "ota/x.py", "../x.py", "/x.py", "a b.py", "x.txt"):
            with self.subTest(path=path), self.assertRaises(ota.UpdateError):
                self.fetch(FakeBbms("2", {**self.release, path: b"bad"}))
        self.assertEqual(self.get("main.py"), b"the loader")

    def test_a_manifest_for_another_version_is_refused(self):
        with self.assertRaises(ota.UpdateError):
            self.fetch(FakeBbms("9", self.release), "2")

    def test_a_power_cut_while_files_are_being_moved(self):
        """Stop the loader after each file it moves; starting again must always finish the job."""
        moves = 0
        while True:
            folder = tempfile.TemporaryDirectory()
            self.root = folder.name
            for path, data in (("reader.py", b"reader 1"), ("version.py", b'VERSION = "1"'), ("font/a.py", b"font a")):
                self.put(path, data)
            self.fetch(FakeBbms("2", self.release))
            real_rename, done = os.rename, [0]

            def rename(source, target):
                if done[0] == moves:
                    raise KeyboardInterrupt  # the power goes
                done[0] += 1
                real_rename(source, target)

            os.rename = rename
            try:
                loader.apply_pending(self.root)
                finished = True
            except KeyboardInterrupt:
                finished = False
            finally:
                os.rename = real_rename
            if not finished:
                loader.apply_pending(self.root)  # power back: the loader runs again
            self.assertEqual(self.get("reader.py"), b"reader 2", "after %d moves" % moves)
            self.assertEqual(self.get("extra.py"), b"new")
            self.assertIsNotNone(json.loads(self.get("ota/trial.json")))
            # ...and the old firmware can still be put back.
            loader.roll_back(self.root, "x")
            self.assertEqual(self.get("reader.py"), b"reader 1", "rolled back after %d moves" % moves)
            self.assertEqual(self.get("version.py"), b'VERSION = "1"')
            self.assertIsNone(self.get("extra.py"))
            folder.cleanup()
            if finished:
                break
            moves += 1
        self.assertGreater(moves, 5)


if __name__ == "__main__":
    unittest.main()
