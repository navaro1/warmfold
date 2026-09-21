"""Append-only event log with size-based rotation.

One line per event: ``<iso-utc> <event> <msg>``. The log rotates by
truncation: past 2 MB the file keeps only its last 1 MB.
"""

import os
import time

MAX_BYTES = 2 * 1024 * 1024
KEEP_BYTES = 1 * 1024 * 1024


def _iso_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _one_line(msg):
    text = str(msg)
    return text.replace("\r", " ").replace("\n", " ")


def log_path(data_dir):
    return os.path.join(data_dir, "log", "warmfold.log")


def _rotate(path):
    size = os.path.getsize(path)
    if size <= MAX_BYTES:
        return
    with open(path, "rb") as handle:
        handle.seek(-KEEP_BYTES, os.SEEK_END)
        tail = handle.read()
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(tail)
    os.replace(tmp, path)


def append(data_dir, event, msg):
    """Append one line. Never raises; logging must not break a hook."""
    try:
        directory = os.path.join(data_dir, "log")
        os.makedirs(directory, exist_ok=True)
        path = log_path(data_dir)
        try:
            _rotate(path)
        except OSError:
            pass
        line = "%s %s %s\n" % (_iso_utc(), event, _one_line(msg))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
