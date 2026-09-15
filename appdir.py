# -*- coding: utf-8 -*-
"""Formant app data folder and exclusive file lock."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


def resolve(here: Path) -> Path:
    folder = Path(os.environ.get("APPDATA", str(here))) / "Formant"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextmanager
def exclusive(path: Path, timeout: float = 8.0):
    """Process lock for a data file. Uses path.name + '.lock' beside it."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() < 1:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.04)
        yield
    finally:
        if locked:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
