# /// script
# requires-python = ">=3.12"
# dependencies = ["mpy-cross>=1.27,<1.28"]
# ///
"""
Build a firmware release for uploading to BBMS (admin > Access > Firmware releases > Add), which then
sends it to the readers chosen there. Run it with `uv run make_release.py`.

The release is dist/reader-<version>.zip: every file the reader runs, compiled (build.py), and a
manifest.json giving the version (from app/version.py) and each file's SHA-256. app/main.py, the loader,
is left out: an update never replaces it (see the comment at its top), so a change to it needs
deploy_app.py and a USB cable.
"""

import hashlib
import json
import os
import re
import zipfile

from build import build


def firmware_files():
    """{path on the reader: path here} for every file of the firmware but the loader."""
    return {name: path for name, path in build().items() if name != "main.py"}


def main():
    with open("app/version.py") as f:
        version = re.search(r'^VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)
    manifest = {"version": version, "files": {}}
    os.makedirs("dist", exist_ok=True)
    target = "dist/reader-%s.zip" % version
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, path in firmware_files().items():
            with open(path, "rb") as f:
                data = f.read()
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
            bundle.writestr(name, data)
        bundle.writestr("manifest.json", json.dumps(manifest, indent=1))
    print("%s: version %s, %d files" % (target, version, len(manifest["files"])))


if __name__ == "__main__":
    main()
