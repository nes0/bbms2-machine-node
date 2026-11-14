"""
Compile the firmware for the reader: used by deploy_app.py (USB) and make_release.py (over the network).

The ESP32 hasn't the memory to compile a file the size of app/reader.py itself, so every file is compiled
here with MicroPython's cross-compiler, mpy-cross, and the reader is sent the .mpy files. The exception is
main.py, the loader: MicroPython only runs a main.py at start-up, never a main.mpy, and it is small.

mpy-cross must match the MicroPython on the reader (1.27: see the README). The scripts that use this
ask for the right one, so run them with `uv run`.
"""

import glob
import os
import shutil
import subprocess

import mpy_cross

BUILD = "build/app"
# Not firmware: what only the simulation on a computer uses.
NOT_FIRMWARE = {"__main__.py"}
AS_SOURCE = {"main.py"}


def build():
    """Fill build/app with what goes on the reader. Returns {path on the reader: path here}."""
    shutil.rmtree(BUILD, ignore_errors=True)
    files = {}
    for pattern in ("app/*.py", "app/font/*.py"):
        for source in sorted(glob.glob(pattern)):
            name = os.path.relpath(source, "app").replace(os.sep, "/")
            if name in NOT_FIRMWARE:
                continue
            if name not in AS_SOURCE:
                name = name[:-3] + ".mpy"
            target = os.path.join(BUILD, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if name in AS_SOURCE:
                shutil.copyfile(source, target)
            else:
                # -s: the name an error's traceback gives, in place of the path on this computer.
                done = mpy_cross.run("-s", os.path.basename(source), "-o", target, source, stderr=subprocess.PIPE)
                _, errors = done.communicate()
                if done.returncode != 0:
                    raise SystemExit("mpy-cross failed on %s:\n%s" % (source, errors.decode()))
            files[name] = target
    return files
