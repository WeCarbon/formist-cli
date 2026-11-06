"""Node status predicates — a port of noddit's ``core/status.ts``."""

from __future__ import annotations

from datetime import datetime, timezone

from formist_cli.task_model import Node
from formist_cli.task_ref import RefError, parse_ref

STATUS_LABEL_EN = {
    "not_started": "To do",
    "in_progress": "In progress",
    "waiting": "Waiting",
    "needs_decision": "Needs decision",
    "decided": "Answered, for AI",
    "completed": "Done",
    "closed": "Closed",
}

STATUS_LABEL_ZH_HANS = {
    "not_started": "待开始",
    "in_progress": "执行中",
    "waiting": "等待中",
    "needs_decision": "待决策",
    "decided": "已决策待 AI 取走",
    "completed": "已完成",
    "closed": "已关闭",
}


def status_label(state: str, lang: str = "en") -> str:
    table = STATUS_LABEL_ZH_HANS if lang.lower().startswith("zh") else STATUS_LABEL_EN
    return table.get(state, state)


def is_terminal(state: str) -> bool:
    return state in ("completed", "closed")


def awaits_decision(state: str, options: list, answer, round_withdrawn: bool) -> bool:
    return not is_terminal(state) and bool(options) and not answer and not round_withdrawn


def with_open_question(state: str, options: list, answer, round_withdrawn: bool) -> str:
    if state in ("in_progress", "not_started") and awaits_decision(state, options, answer, round_withdrawn):
        return "needs_decision"
    return state


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        v = s.replace("Z", "+00:00")
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def deferral_over(deferred_at: str | None, deferred_until: str | None, now: float | None = None) -> bool:
    if not deferred_at or not deferred_until:
        return False
    until = _parse_iso(deferred_until)
    if until is None:
        return False
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    return until <= now


def is_deferred(node: Node, now: float | None = None) -> bool:
    return bool(node.deferred_at) and not is_terminal(node.state) and not deferral_over(
        node.deferred_at, node.deferred_until, now
    )


def is_wait_due(node: Node, now: float | None = None) -> bool:
    if node.state != "waiting":
        return False
    if not node.waiting_until:
        return True
    until = _parse_iso(node.waiting_until)
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    return until is None or until <= now


def delay_of(node: Node, now: float | None = None) -> dict | None:
    if node.state == "waiting":
        return {
            "kind": "active",
            "on": node.waiting_on,
            "until": node.waiting_until,
            "due": is_wait_due(node, now),
        }
    if is_deferred(node, now):
        return {"kind": "passive", "until": node.deferred_until, "on": None, "due": False}
    return None


def find_ref(raw: str, from_project: str, from_letter: str | None, items: list[Node]) -> Node | None:
    try:
        r = parse_ref(raw)
    except RefError:
        return None
    pool = [i for i in items if i.id == r.id]
    if r.project:
        return next((i for i in pool if i.project == r.project), None)
    if r.letter:
        lettered = [i for i in pool if i.letter == r.letter]
        if lettered:
            same = next((i for i in lettered if i.project == from_project), None)
            return same if same is not None else lettered[0]
        if r.letter != "D":
            return None
    return next((i for i in pool if i.project == from_project), None)


def dependencies_of(node: Node, items: list[Node]) -> list:
    return [find_ref(ref, node.project, node.letter, items) or ref for ref in (node.after or [])]


def is_startable(node: Node, items: list[Node]) -> bool:
    return all(not isinstance(dep, str) and is_terminal(dep.state) for dep in dependencies_of(node, items))


def waiting_on_owner(node: Node, items: list[Node]) -> bool:
    return node.state == "needs_decision" and is_startable(node, items)


def can_decide_early(node: Node, items: list[Node]) -> bool:
    return node.state == "needs_decision" and not is_startable(node, items)
