"""The 6 built-in views — a port of noddit's ``core/view-defs.ts``
(``BUILTIN_VIEWS``), hand-coded rather than the full ``nod view``
filter-DSL (contract §5's views row; the general ``nod view add --filter``
authoring surface is out of scope for `fm`'s first cut — see the C1052
research spec §8)."""

from __future__ import annotations

from formist_cli.task_model import Node
from formist_cli.task_status import delay_of, is_startable

VIEW_IDS = ("available", "active", "decide", "waiting", "live", "all", "external")


def apply_view(nodes: list[Node], view: str) -> list[Node]:
    if view == "available":
        return [n for n in nodes
                if n.state in ("in_progress", "waiting", "needs_decision", "decided", "not_started")
                and not n.milestone]
    if view == "active":
        return [n for n in nodes if (
            (n.state in ("in_progress", "needs_decision", "decided") and not n.milestone)
            or (n.state == "not_started" and is_startable(n, nodes) and not n.milestone)
        )]
    if view == "decide":
        return [n for n in nodes if (
            (n.state == "needs_decision" and not n.milestone)
            or (bool(n.assignee) and n.assignee != "ai" and n.state in ("in_progress", "waiting", "decided"))
            or (bool(n.assignee) and n.assignee != "ai" and n.state == "not_started" and is_startable(n, nodes))
        )]
    if view == "waiting":
        return [n for n in nodes if n.state == "waiting" or delay_of(n) is not None]
    if view == "live":
        return [n for n in nodes if not n.archived]
    if view == "all":
        return list(nodes)
    if view == "external":
        return [n for n in nodes if "external" in n.tags]
    raise ValueError(f"unknown view: {view!r} (one of {', '.join(VIEW_IDS)})")
