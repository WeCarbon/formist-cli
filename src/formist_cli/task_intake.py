"""派活 (briefs): a port of noddit's ``core/intake.ts``."""

from __future__ import annotations

import re

from formist_cli.task_assignee import is_human_task
from formist_cli.task_format import NOTE_KIND
from formist_cli.task_model import Node
from formist_cli.task_status import is_deferred, is_terminal


def brief_state(node: Node) -> str | None:
    if not node.intake or is_terminal(node.state):
        return None
    return "claimed" if node.claimed_by else "unclaimed"


def is_unclaimed_brief(node: Node) -> bool:
    return brief_state(node) == "unclaimed" and not is_deferred(node) and not is_human_task(node.assignee)


def original_words(node: Node) -> str | None:
    for n in node.notes:
        if n.kind == NOTE_KIND["original"]:
            return n.text
    return None


def _original_line(lines: list[str]) -> int:
    from formist_cli.task_parse import parse_note_heading

    for i, line in enumerate(lines):
        h = parse_note_heading(line)
        if h and h.kind == NOTE_KIND["original"]:
            return i
    return -1


_H2_RE = re.compile(r"^##[ \t]+\S")


def with_description(body: str, description: str, original_heading: str) -> str:
    lines = body.split("\n")
    at = _original_line(lines)
    if at == -1:
        first_h2 = next((i for i, l in enumerate(lines) if _H2_RE.match(l)), -1)
        head_lines = lines if first_h2 == -1 else lines[:first_h2]
        head = "\n".join(head_lines).strip()
        tail = "" if first_h2 == -1 else "\n".join(lines[first_h2:]).strip()
        rest = "\n\n".join(x for x in [f"## {original_heading}\n\n{head}" if head else "", tail] if x)
    else:
        rest = "\n".join(lines[at:]).strip()
    desc = description.strip()
    return "\n\n".join(x for x in [desc, rest] if x) + "\n"
