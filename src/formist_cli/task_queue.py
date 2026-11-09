"""The delivery queue — a port of noddit's ``core/queue.ts``.

``decisions/queue.json``: ``{"queue": [id, ...]}``, FIFO, guarded by its
own O_EXCL lockfile distinct from the per-node lock ``task_lock`` uses for
``mutate``.
"""

from __future__ import annotations

import json
import os

from formist_cli.task_paths import Layout


def _queue_path(layout: Layout) -> str:
    return os.path.join(layout.root, "queue.json")


def _lock_path(layout: Layout) -> str:
    return os.path.join(layout.root, ".queue.lock")


def read_queue(layout: Layout) -> list[int]:
    try:
        with open(_queue_path(layout), encoding="utf-8") as fh:
            raw = json.load(fh)
        ids = raw.get("queue")
        if not isinstance(ids, list):
            return []
        return [n for n in ids if isinstance(n, int)]
    except (OSError, ValueError):
        return []


def _write_queue(layout: Layout, ids: list[int]) -> None:
    os.makedirs(layout.root, exist_ok=True)
    tmp = f"{_queue_path(layout)}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"queue": ids}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, _queue_path(layout))


def with_queue(layout: Layout, mutate) -> list[int]:
    import time

    os.makedirs(layout.root, exist_ok=True)
    lock = _lock_path(layout)
    deadline = time.monotonic() + 5.0
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                try:
                    os.remove(lock)
                except OSError:
                    pass
                continue
            time.sleep(0.02)
    try:
        next_ids = mutate(read_queue(layout))
        deduped = list(dict.fromkeys(next_ids))
        _write_queue(layout, deduped)
        return deduped
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def enqueue(layout: Layout, id_: int) -> None:
    with_queue(layout, lambda ids: ids if id_ in ids else [*ids, id_])


def dequeue(layout: Layout, id_: int) -> None:
    with_queue(layout, lambda ids: [n for n in ids if n != id_])


def queue_exists(layout: Layout) -> bool:
    return os.path.exists(_queue_path(layout))
