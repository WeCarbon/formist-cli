"""The shared C1120 workflow corpus used by local, server and Noddit parity tests."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib.resources import files
from typing import Any

CORPUS_SHA256 = "4c44181cd69b92788ee2f876510fde464712c35adfd207ee8aaec9b5e5278cfe"


def load() -> dict[str, Any]:
    """Read the packaged golden corpus, never a test-local reconstruction."""
    raw = files("formist_cli").joinpath("workflow_golden.json").read_bytes()
    if sha256(raw).hexdigest() != CORPUS_SHA256:
        raise ValueError("workflow_golden.json drifted from the shared C1120 corpus")
    return json.loads(raw)


def render(line: str, *, ref: str, cli: str) -> str:
    return line.replace("{{ref}}", ref).replace("{{cli}}", cli)


def without_cursor(line: str) -> str:
    """Drop Noddit's dynamic cursor suffix while keeping the whole contract line."""
    return line.split("  since=", 1)[0].rstrip()
