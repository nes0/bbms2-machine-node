# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# Firmware updates over the network: the running reader's part. It downloads the firmware BBMS wants
# this reader to run into /ota/new and leaves the rest to the loader (main.py, which explains the whole
# sequence) at the next start.
#
# This file must not be called ota.py. MicroPython, asked to import "ota", finds the folder /ota before
# it finds ota.mpy and imports the folder, which has nothing in it: once a first update had made the
# folder, no firmware could start. The same goes for any file named after a folder on the reader.
#

import binascii
import hashlib
import json
import os

from util import log

OTA = "/ota"
MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
SPARE_BYTES = 64 * 1024  # room to leave free for the reader's own files
DOWNLOAD_SECONDS = 30
# Never replaced: the loader, MicroPython's own start-up file, and the folders that aren't firmware.
PROTECTED = ("main.py", "boot.py", "data/", "ota/")
PATH_CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_/."


class UpdateError(Exception):
    pass


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _is_folder(path):
    try:
        return os.stat(path)[0] & 0x4000 != 0
    except OSError:
        return False


def _remove_tree(path):
    if _is_folder(path):
        for name in os.listdir(path):
            _remove_tree(path + "/" + name)
        os.rmdir(path)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def _make_folders(path):
    parts = path.split("/")[:-1]
    for count in range(1, len(parts) + 1):
        folder = "/".join(parts[:count])
        if folder:
            try:
                os.mkdir(folder)
            except OSError:
                pass


def file_sha256(path):
    """The file's SHA-256 in hex, or None if there's no such file."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(512)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return binascii.hexlify(digest.digest()).decode()


def safe_path(path):
    if not isinstance(path, str) or not path or len(path) > 100:
        return False
    if path.startswith("/") or ".." in path or "//" in path:
        return False
    if not (path.endswith(".py") or path.endswith(".mpy")):
        return False
    for character in path:
        if character not in PATH_CHARACTERS:
            return False
    for prefix in PROTECTED:
        if path.startswith(prefix):
            return False
    return True


class Updater:
    def __init__(self, root=""):
        self.root = root  # "" on the ESP32; a folder when this is being tested on a computer
        self.ota = root + OTA
        self.on_trial = _read_json(self.ota + "/trial.json") is not None

    # ---- What happened to the last update -------------------------------------------------------

    def failure(self):
        """{"version", "error"} for a firmware the loader tried and took off again, or None."""
        return _read_json(self.ota + "/failed.json")

    def forget_failure(self):
        _remove_tree(self.ota + "/failed.json")

    def confirm(self):
        """The firmware on trial has reached BBMS: keep it."""
        trial = _read_json(self.ota + "/trial.json")
        if trial:
            # The list of what's installed tells the next update which files to take away.
            self._write_json("installed.json", {"version": trial["version"], "files": trial["files"]})
            _remove_tree(self.ota + "/trial.json")
            _remove_tree(self.ota + "/old")
            _remove_tree(self.ota + "/failed.json")
            log("firmware", trial["version"], "confirmed")
        self.on_trial = False

    # ---- Downloading an update ------------------------------------------------------------------

    async def fetch(self, api, version, keep_alive=None):
        """
        Download firmware `version` ready for the loader. Returns when it's all there and checked;
        the caller then restarts the reader. Raises UpdateError, having changed nothing that matters.
        """
        if self.on_trial:
            raise UpdateError("the last update is still on trial")
        result = await api.request("GET", "firmware/manifest")
        if not result.ok:
            raise UpdateError("no manifest: %s" % (result.status or result.error))
        manifest = result.data
        files = manifest.get("files")
        if manifest.get("version") != version or not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
            raise UpdateError("bad manifest")
        for entry in files:
            if not safe_path(entry.get("path")) or not 0 <= entry.get("size", -1) <= MAX_FILE_BYTES:
                raise UpdateError("bad file in manifest: %s" % entry.get("path"))

        wanted = []
        for entry in files:
            if file_sha256(self.root + "/" + entry["path"]) != entry["sha256"]:
                wanted.append(entry)
        installed = _read_json(self.ota + "/installed.json") or {}
        paths = [entry["path"] for entry in files]
        remove = [path for path in installed.get("files", ()) if path not in paths and safe_path(path)]

        _remove_tree(self.ota + "/pending.json")
        _remove_tree(self.ota + "/new")
        _remove_tree(self.ota + "/old")
        needed = sum(entry["size"] for entry in wanted) + SPARE_BYTES
        space = os.statvfs(self.root or "/")
        if space[1] * space[4] < needed:  # fragment size x fragments free
            raise UpdateError("not enough room: %d bytes needed" % needed)

        for entry in wanted:
            if keep_alive:
                keep_alive()
            target = self.ota + "/new/" + entry["path"]
            _make_folders(target)
            result = await api.download("firmware/files/" + entry["sha256"], target, DOWNLOAD_SECONDS)
            if result.status != 200:
                raise UpdateError("%s: %s" % (entry["path"], result.status or result.error))
            if result.data != (entry["size"], entry["sha256"]):
                raise UpdateError("%s arrived damaged" % entry["path"])
        self._write_json(
            "pending.json",
            {"version": version, "replace": [entry["path"] for entry in wanted], "remove": remove, "files": paths},
        )
        log("firmware", version, "downloaded: %d files to replace, %d to remove" % (len(wanted), len(remove)))

    def _write_json(self, name, value):
        path = self.ota + "/" + name
        _make_folders(path)
        with open(path + ".tmp", "w") as f:
            json.dump(value, f)
        _remove_tree(path)
        os.rename(path + ".tmp", path)
