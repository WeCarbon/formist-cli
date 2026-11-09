"""Free-form durations — a port of noddit's ``core/duration.ts`` and
``core/estimate.ts``."""

from __future__ import annotations

import re

_BARE_NUM_RE = re.compile(r"^\d+(\.\d+)?$")
_DURATION_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_DURATION_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])")


class DurationError(ValueError):
    pass


def parse_duration(raw: str) -> int:
    """``90s``, ``30m``, ``1h``, ``2d``, ``1h30m`` -> milliseconds. A bare
    number is MINUTES (the unit a person types for ``--stale 30``)."""
    s = raw.strip().lower()
    if _BARE_NUM_RE.match(s):
        return int(float(s) * 60_000)
    total = 0.0
    rest = s
    while rest:
        m = _DURATION_TOKEN_RE.match(rest)
        if not m:
            raise DurationError(f"not a duration: {raw!r}")
        total += float(m.group(1)) * _DURATION_UNIT_MS[m.group(2)]
        rest = rest[m.end():].strip()
    return int(total)


_EST_UNIT_MIN = {
    "m": 1, "min": 1, "h": 60, "hr": 60, "d": 8 * 60,
    "人日": 8 * 60, "天": 8 * 60, "小时": 60, "分钟": 1, "w": 5 * 8 * 60,
}
_EST_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(人日|小时|分钟|天|min|hr|[mhdw])")


def est_minutes(est: str | None) -> int | None:
    """``2h``, ``30m``, ``1h30m``, ``1.5h``, ``0.5 人日``, ``1d`` (8h workday),
    a bare ``45`` (minutes); ``None`` when it can't be fully consumed."""
    s = (est or "").strip().lower()
    if not s:
        return None
    if _BARE_NUM_RE.match(s):
        return int(float(s))
    total = 0.0
    at = 0
    for m in _EST_TOKEN_RE.finditer(s):
        if m.start() != at:
            break
        total += float(m.group(1)) * _EST_UNIT_MIN[m.group(2)]
        at = m.end()
        while at < len(s) and s[at] == " ":
            at += 1
    return round(total) if at == len(s) and total > 0 else None


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(round(minutes), 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def est_who(assignee: str | None) -> str:
    from formist_cli.task_assignee import is_human_task

    return "human" if is_human_task(assignee) else "ai"
