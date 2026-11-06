"""``nod check`` — a port of noddit's ``cli/commands/check.ts`` /
``core/repair.ts`` / the per-project half of ``core/project.ts``'s
``configProblems``.

Out of scope for this pass (documented gaps, tracked under C1140):

* multi-project letter-clash checks — ``fm task`` operates on one
  ``--dir`` at a time, there is no registry of projects to clash across;
* source/adapter checks (``sources/<name>/source.md``) — no source
  adapters exist in fm yet, left for whoever picks up C1139;
* ``--fix``/``--dry-run`` auto-repair — every problem here is reported,
  none auto-repaired.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from formist_cli.task_model import Node
from formist_cli.task_parse import OPTIONS_HEADING_RE, parse_decision, parse_options
from formist_cli.task_paths import Layout, list_entry_files
from formist_cli.task_queue import read_queue
from formist_cli.task_title import TITLE_MAX_COLS, title_width

_H2_RE = re.compile(r"^##[ \t]+\S")
_LETTER_RE = re.compile(r"^[A-Z]$")


@dataclass
class Problem:
    level: str  # "error" | "warn"
    file: str
    message: str


@dataclass
class DuplicateOptionsBlock:
    from_line: int
    to_line: int
    keeps: str
    drops: str


@dataclass
class _Block:
    start: int
    end: int
    round: int
    heading: str


def _blocks(lines: list[str]) -> list[_Block]:
    out = []
    for i, line in enumerate(lines):
        m = OPTIONS_HEADING_RE.match(line)
        if not m:
            continue
        end = i + 1
        while end < len(lines) and not _H2_RE.match(lines[end]):
            end += 1
        out.append(_Block(start=i, end=end, round=int(m.group(1) or 1), heading=line))
    return out


def find_duplicate_options(body: str) -> DuplicateOptionsBlock | None:
    """The block an older `nod ask` appended after text that already
    carried its own ``## 选项``: same keys/labels, every body empty."""
    lines = body.split("\n")
    bs = _blocks(lines)
    for i in range(len(bs) - 1, 0, -1):
        cur, prev = bs[i], bs[i - 1]
        if cur.round != prev.round and cur.round != prev.round + 1:
            continue
        a = parse_options("\n".join(lines[prev.start:prev.end]))
        b = parse_options("\n".join(lines[cur.start:cur.end]))
        same = bool(a) and len(a) == len(b) and all(x.key == y.key and x.label == y.label for x, y in zip(a, b))
        if same and all(not o.body.strip() for o in b) and any(o.body.strip() for o in a):
            from_ = cur.start
            while from_ > 0 and not lines[from_ - 1].strip():
                from_ -= 1
            return DuplicateOptionsBlock(from_line=from_ + 1, to_line=cur.end, keeps=prev.heading, drops=cur.heading)
    return None


def find_cycles(nodes: list[Node]) -> list[list[str]]:
    """``after`` dependency cycles — a cycle makes every node in it
    permanently unstartable."""
    from formist_cli.task_ref import format_ref
    from formist_cli.task_status import dependencies_of

    by_key = {n.id: n for n in nodes}
    cycles: list[list[str]] = []
    state: dict[int, str] = {}
    stack: list[int] = []

    def visit(k: int) -> None:
        if state.get(k) == "done":
            return
        if state.get(k) == "visiting":
            start = stack.index(k)
            cycle = [*stack[start:], k]
            cycles.append([format_ref(by_key[x].id, by_key[x].letter) for x in cycle if x in by_key])
            return
        node = by_key.get(k)
        if node is None:
            return
        state[k] = "visiting"
        stack.append(k)
        for dep in dependencies_of(node, nodes):
            if not isinstance(dep, str):
                visit(dep.id)
        stack.pop()
        state[k] = "done"

    for n in nodes:
        visit(n.id)
    return cycles


def config_problems(layout: Layout) -> list[str]:
    """``decs.json`` validation — a port of ``core/project.ts``'s
    ``configProblems``, the single-project half (letter-clash across
    projects is out of scope here, see the module docstring)."""
    path = os.path.join(layout.root, "decs.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        return [f"decs.json is not valid JSON: {exc}"]
    if not isinstance(raw, dict):
        return ["decs.json must be a JSON object"]
    letter = raw.get("letter")
    if letter is not None and not (isinstance(letter, str) and _LETTER_RE.match(letter)):
        return [f"decs.json letter must be one A-Z letter, got {letter!r}"]
    name = raw.get("name")
    if name is not None and not (isinstance(name, str) and name.strip()):
        return ["decs.json name must be a non-empty string"]
    former = raw.get("former_letters")
    if former is not None and not (
        isinstance(former, list) and all(isinstance(x, str) and _LETTER_RE.match(x) for x in former)
    ):
        return ["decs.json former_letters must be a list of A-Z letters"]
    return []


def check_project(layout: Layout, project_key: str, letter: str | None = None) -> tuple[list[Problem], int]:
    """Validates every entry: duplicate ids/keys, a frontmatter/filename id
    mismatch, broken options, missing id reservations, dependency cycles.
    Returns ``(problems, total)`` — ``total`` is the count of distinct ids
    that parsed cleanly, matching ``check.ts``'s own ``seen.size``.

    Parses each file itself (rather than taking an already-parsed node
    list) so that one unreadable file is reported as its own error and
    does not abort the whole sweep — matching ``check.ts``'s own
    per-file try/catch, which a caller like ``all_nodes()`` does not do.

    ``letter`` defaults to the project's own configured letter (never
    ``None``) when omitted — a caller that forgets to pass it still gets
    correct ``after``/``supersedes`` cross-reference resolution; see
    ``LocalStore.letter()``'s docstring for why ``None`` is the wrong
    default."""
    if letter is None:
        from formist_cli import task_project

        letter = task_project.project_letter(layout)

    problems: list[Problem] = []
    seen: dict[int, str] = {}
    nodes: list[Node] = []

    for message in config_problems(layout):
        problems.append(Problem("error", os.path.join(project_key, "decisions", "decs.json"), message))

    entries = list_entry_files(layout)
    queued_ids = set(read_queue(layout))

    for entry in entries:
        rel = os.path.relpath(entry.path, layout.project_root)
        try:
            with open(entry.path, encoding="utf-8") as fh:
                raw = fh.read()
            node = parse_decision(raw, entry.path, entry.archived, project=project_key, queued=entry.id in queued_ids)
            node.letter = letter
        except Exception as exc:  # noqa: BLE001 - deliberate: one bad file must not abort the sweep
            problems.append(Problem("error", rel, str(exc)))
            continue
        nodes.append(node)

        prior = seen.get(node.id)
        if prior:
            problems.append(Problem("error", rel, f"number {node.id} is also used by {prior}"))
        else:
            seen[node.id] = rel

        if node.id != entry.id:
            problems.append(Problem("error", rel, f"front matter says id {node.id}, filename says {entry.id}"))

        width = title_width(node.title)
        if width > TITLE_MAX_COLS:
            problems.append(Problem(
                "warn", rel,
                f'title wider than {TITLE_MAX_COLS} columns — bench truncates it; '
                f'fix with fm task retitle {node.id} "<short title>"',
            ))
        if not node.author:
            problems.append(Problem("warn", rel, "no author — nobody knows who can answer a follow-up"))
        if not node.created:
            problems.append(Problem("warn", rel, "no created timestamp"))
        if not node.body.strip():
            problems.append(Problem("warn", rel, "empty body"))

        dup = find_duplicate_options(raw)
        if dup:
            problems.append(Problem(
                "error", rel,
                f"options block at lines {dup.from_line}-{dup.to_line} repeats the one before it "
                f"with empty bodies — reads as {len(node.options)} options; fm task check --fix drops it",
            ))

        keys = [o.key for o in node.options]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes and not dup:
            problems.append(Problem("error", rel, f"option key repeated: {', '.join(sorted(dupes))}"))
        if node.default and not keys:
            problems.append(Problem(
                "error", rel,
                f'default is "{node.default}" but there are no options at all — '
                f"the options block probably failed to parse",
            ))
        elif node.default and node.default not in keys:
            problems.append(Problem(
                "error", rel, f'default is "{node.default}" but the options are {", ".join(keys)}',
            ))
        if node.state == "needs_decision" and not node.options:
            problems.append(Problem("warn", rel, "no options — nothing for the owner to press"))

        if node.queued and node.archived:
            problems.append(Problem("warn", rel, "archived but still on the delivery queue"))
        if node.archived and not node.answer and node.kind in ("decision", "acceptance") and node.state != "closed":
            problems.append(Problem("warn", rel, "archived without an answer"))

        attached_targets = {a.target for a in node.attachments}
        for ap in node.approvals:
            if ap.target not in attached_targets:
                problems.append(Problem(
                    "warn", rel, f'approval record for "{ap.target}" has no matching attachment anymore',
                ))

        reservation = os.path.join(layout.ids_dir, str(node.id))
        if not os.path.exists(reservation):
            problems.append(Problem("warn", rel, f"no id reservation — a new entry could reuse {node.id}"))

    for cycle in find_cycles(nodes):
        problems.append(Problem("error", "after:", f"node dependency cycle: {' → '.join(cycle)}"))

    return problems, len(seen)
