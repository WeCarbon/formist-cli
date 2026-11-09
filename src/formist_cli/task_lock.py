"""File locking and atomic writes — a port of noddit's ``core/lock.ts``.

POSIX ``O_EXCL`` create is a direct primitive on macOS/Linux (this
project's own dev targets — see the C1052 research spec §9.5); the
Windows ``EPERM``/``EBUSY`` rename-retry dance in the TS source has no
equivalent need here.
"""

from __future__ import annotations

import os
import time


def write_file_atomic(path: str, data: str) -> None:
    tmp = f"{os.path.dirname(path)}/.{os.path.basename(path)}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)


def create_file_atomic(path: str, data: str) -> None:
    """Creates a file that must not exist yet, atomically."""
    tmp = f"{os.path.dirname(path)}/.{os.path.basename(path)}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.link(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def with_file_lock(lock_path: str, fn, stale_seconds: float = 10.0, wait_seconds: float = 30.0):
    """Runs ``fn()`` while holding an O_EXCL lockfile, shared across processes.
    A lock older than ``stale_seconds`` is assumed abandoned and taken over."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - os.stat(lock_path).st_mtime
            except OSError:
                continue
            if age > stale_seconds or time.monotonic() > deadline:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                continue
            time.sleep(0.01 + (os.getpid() % 20) / 1000)
    try:
        return fn()
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
