# /// script
# requires-python = ">=3.13"
# dependencies = ["pyserial", "mpy-cross>=1.27,<1.28"]
# ///
"""
Upload the firmware to a reader over USB: `uv run deploy_app.py`. The files are compiled first (build.py),
and anything on the reader that isn't part of the firmware is deleted, except its own /data folder.
"""

import glob
import hashlib
import os

from build import BUILD, build
from pyboard import Pyboard


def mkdir_p(board, path):
    board.exec_(f"""
import os
try:
    os.mkdir('{path}')
except OSError:
    pass
""")


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def remote_file_info(board, path):
    out = (
        board.exec_(f"""
import os, hashlib
try:
    st = os.stat('{path}')
    h = hashlib.sha1()
    with open('{path}', 'rb') as f:
        while True:
            b = f.read(1024)
            if not b:
                break
            h.update(b)
    print(st[6], ''.join('{{:02x}}'.format(x) for x in h.digest()))
except OSError:
    print("MISSING")
""")
        .decode()
        .strip()
    )

    if out == "MISSING":
        return None

    size, digest = out.split()
    return int(size), digest


def put_file_if_changed(board, local_path, remote_path):
    local_size = os.path.getsize(local_path)
    local_hash = sha1_file(local_path)

    info = remote_file_info(board, remote_path)
    if info and info == (local_size, local_hash):
        return False  # unchanged

    with open(local_path, "rb") as f:
        data = f.read()

    # A block at a time: the reader hasn't the memory to be sent a whole file in one command.
    board.fs_writefile(remote_path, data)
    return True


def list_remote_files(board, root):
    out = board.exec_(f"""
import os

files = []

def walk(path):
    for entry in os.ilistdir(path):
        name = entry[0]
        typ = entry[1]
        full = path + '/' + name if path != '/' else '/' + name

        if typ == 0x4000:   # directory
            walk(full)
        else:
            files.append(full)

walk('{root}')
for f in files:
    print(f)
""")

    return {p.rstrip("/") for p in out.decode().splitlines() if p.strip()}


# What the reader keeps for itself (its settings, meter readings not yet sent, the QR code): never deleted.
KEEP_PREFIX = "/data/"

# Uploaded to the reader's data folder when they exist here. The reader's own files there are left alone.
DATA_FILES = ["data/config.json", "data/ca.der"]


def delete_unmatched(board, remote_root, expected_files):
    actual = list_remote_files(board, remote_root)

    for path in sorted(actual - expected_files):
        if path.startswith(KEEP_PREFIX):
            continue
        print("deleting:", path)
        board.exec_(f"""
import os
try:
    os.remove('{path}')
except OSError:
    pass
""")


def remove_empty_dirs(board, root):
    board.exec_(f"""
import os

def prune(path):
    try:
        entries = os.listdir(path)
    except OSError:
        return False

    for e in entries:
        full = path + '/' + e
        try:
            if os.stat(full)[0] & 0x4000:
                prune(full)
        except OSError:
            pass

    try:
        if not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass

prune('{root}')
""")


def upload_globs(
    board,
    patterns,
    local_root,
    remote_root,
    delete_unmatched_files=False,
):
    files = set()
    for pattern in patterns:
        files.update(p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p))

    local_root = os.path.abspath(local_root)
    mkdir_p(board, remote_root)

    expected_remote_files = set()

    # Create directories
    for f in files:
        rel = os.path.relpath(f, local_root)
        d = os.path.dirname(rel)
        if d:
            mkdir_p(board, remote_root + "/" + d.replace(os.sep, "/"))

    # Upload files
    for f in sorted(files):
        rel = os.path.relpath(f, local_root)
        remote_path = remote_root.rstrip("/") + "/" + rel.replace(os.sep, "/")
        expected_remote_files.add(remote_path.rstrip("/"))

        changed = put_file_if_changed(board, f, remote_path)
        if changed:
            print("uploaded:", rel)
        else:
            print("unchanged:", rel)

    if delete_unmatched_files:
        delete_unmatched(board, remote_root, expected_remote_files)
        remove_empty_dirs(board, remote_root)


PORT = "/dev/ttyUSB0"

PATTERNS = sorted(build().values())  # compiled into build/app: .mpy files, and main.py as it is

board = Pyboard(PORT)
board.enter_raw_repl()

upload_globs(
    board,
    patterns=PATTERNS,
    local_root=BUILD,
    remote_root="",
    delete_unmatched_files=True,
)

mkdir_p(board, "/data")
for path in DATA_FILES:
    if os.path.isfile(path):
        changed = put_file_if_changed(board, path, "/" + path)
        print("uploaded:" if changed else "unchanged:", path)
    else:
        print("not here, so not uploaded:", path)

board.exit_raw_repl()
board.close()
