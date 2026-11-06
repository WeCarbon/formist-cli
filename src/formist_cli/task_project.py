"""``decisions/decs.json`` — a port of noddit's ``core/project.ts``."""

from __future__ import annotations

import json
import os
import re

from formist_cli.task_lock import write_file_atomic
from formist_cli.task_paths import Layout, list_entry_files

DEFAULT_LETTER = "X"
_LETTER_RE = re.compile(r"^[A-Z]$")


def _config_path(layout: Layout) -> str:
    return os.path.join(layout.root, "decs.json")


def read_project_config(layout: Layout) -> dict:
    try:
        with open(_config_path(layout), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    letter = raw.get("letter")
    letter = letter if isinstance(letter, str) and _LETTER_RE.match(letter) else None
    name = raw.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    former = raw.get("former_letters")
    former = [x for x in former if isinstance(x, str) and _LETTER_RE.match(x)] if isinstance(former, list) else None
    out = dict(raw)
    out["letter"] = letter
    out["name"] = name
    out["former_letters"] = former or None
    return out


def write_project_config(layout: Layout, **patch) -> dict:
    letter = patch.get("letter")
    if letter is not None and not _LETTER_RE.match(letter):
        raise ValueError(f"decs.json letter must be one A-Z letter, got {letter!r}")
    before = read_project_config(layout)
    next_cfg = {**before, **patch}
    if letter is not None:
        had_entries = bool(list_entry_files(layout))
        old_letter = before.get("letter") or (DEFAULT_LETTER if had_entries else None)
        former = [x for x in (before.get("former_letters") or []) if x not in (old_letter, letter)]
        if old_letter and old_letter != letter:
            next_cfg["former_letters"] = [*former, old_letter]
        else:
            next_cfg["former_letters"] = former or None
    next_cfg = {k: v for k, v in next_cfg.items() if v not in (None, [], "")}
    os.makedirs(layout.root, exist_ok=True)
    write_file_atomic(_config_path(layout), json.dumps(next_cfg, indent=2, ensure_ascii=False) + "\n")
    return read_project_config(layout)


def project_letter(layout: Layout) -> str:
    return read_project_config(layout).get("letter") or DEFAULT_LETTER


def project_display_name(layout: Layout) -> str:
    return read_project_config(layout).get("name") or os.path.basename(layout.project_root)
