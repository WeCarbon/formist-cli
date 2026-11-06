"""The ``--json`` wire shape — a port of noddit's ``core/render.ts``'s
``toJSON``. ``fm task <verb> --json`` must print this same shape (contract
§6.2: "same names, flags and ``--json`` shape").
"""

from __future__ import annotations

from formist_cli.task_assignee import is_human_task
from formist_cli.task_duration import est_minutes, est_who, fmt_minutes  # noqa: F401 (fmt_minutes kept for parity)
from formist_cli.task_intake import brief_state, original_words
from formist_cli.task_model import Attachment, Node
from formist_cli.task_ref import format_ref
from formist_cli.task_status import delay_of, is_wait_due, status_label

#: 30m default, matching `nod show --stale`'s documented default
#: (`checkinThresholdMs`'s `checkin-after` setting). Settings-file override
#: is not ported — this is a cosmetic JSON field, not core behavior.
_DEFAULT_CHECKIN_AFTER_MS = 30 * 60_000


def _parse_iso_ms(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def staleness_of(node: Node, now_ms: float | None = None) -> str:
    if node.state != "in_progress":
        return "fresh"
    from formist_cli.task_status import is_deferred

    if is_deferred(node):
        return "fresh"
    if is_human_task(node.assignee):
        return "fresh"
    from datetime import datetime, timezone

    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    t = _parse_iso_ms(node.active_at or node.updated_at or node.created)
    if t is None:
        return "fresh"
    threshold = _DEFAULT_CHECKIN_AFTER_MS
    if node.stale_after:
        from formist_cli.task_duration import parse_duration

        try:
            threshold = parse_duration(node.stale_after)
        except ValueError:
            pass
    return "stale" if now_ms - t >= threshold else "fresh"


def deferred_by_agent(node: Node) -> bool:
    if not node.deferred_at:
        return False
    from formist_cli.task_format import NOTE_KIND

    last_deferred = next((n for n in reversed(node.notes) if n.kind == NOTE_KIND["deferred"]), None)
    return bool(last_deferred and last_deferred.role == "agent")


def reopened_after_archive(node: Node) -> bool:
    return bool(node.reopened_at and node.archived_at and node.reopened_at > node.archived_at)


def last_human_at(node: Node) -> str | None:
    humans = [n.at for n in node.notes if n.role == "human"]
    return max(humans) if humans else None


def _attachment_json(a: Attachment) -> dict:
    return {"label": a.label, "target": a.target, "image": a.image, "group": a.group, "caption": a.caption}


def _answer_json(answer) -> dict:
    """The raw ``DecisionAnswer`` object's own fields — matches JS's
    ``JSON.stringify`` dropping an `undefined` property entirely (as
    opposed to the outer ``?? null`` fields, which stay present as
    ``null``): only ``by``/``at`` are required and always present."""
    out: dict = {}
    if answer.choice is not None:
        out["choice"] = answer.choice
    if answer.answers:
        out["answers"] = answer.answers
    if answer.answer_notes:
        out["answer_notes"] = answer.answer_notes
    if answer.note is not None:
        out["note"] = answer.note
    out["by"] = answer.by
    out["at"] = answer.at
    return out


def _delay_json(node: Node) -> dict | None:
    d = delay_of(node)
    if not d:
        return None
    return {"kind": d["kind"], "until": d.get("until"), "on": d.get("on"), "due": d.get("due", False)}


def to_json(d: Node, lang: str = "en") -> dict:
    return {
        "id": d.id,
        "ref": format_ref(d.id, d.letter),
        "project": d.project,
        "project_name": d.project_name or d.project,
        "title": d.title,
        "author": d.author,
        "status": d.state,
        "status_label": status_label(d.state, lang),
        "close_reason": d.close_reason,
        "waiting_on": d.waiting_on,
        "waiting_until": d.waiting_until if d.state == "waiting" else None,
        "wait_due": is_wait_due(d),
        "deferred_at": d.deferred_at,
        "deferred_by": d.deferred_by,
        "deferred_by_ai": deferred_by_agent(d),
        "deferred_until": d.deferred_until if d.deferred_at else None,
        "delay": _delay_json(d),
        "assignee": d.assignee or "ai",
        "human_task": is_human_task(d.assignee),
        "created": d.created,
        "updated_at": d.updated_at,
        "after": d.after or [],
        "progress": d.progress,
        "progress_msg": d.progress_msg,
        "est": d.est,
        "est_minutes": est_minutes(d.est),
        "est_who": est_who(d.assignee),
        "every": d.every,
        "when": d.when,
        "stale_after": d.stale_after,
        "staleness": staleness_of(d),
        "checkin": ({"asked": d.checkin.asked, "last_asked_at": d.checkin.last_asked_at, "unanswered": d.checkin.unanswered} if d.checkin else None),
        "steps": [{"label": s.label, "done": s.done} for s in d.steps] if d.steps is not None else None,
        "owner_agent": d.owner_agent or d.author,
        "intake": bool(d.intake),
        "brief": brief_state(d),
        "claimed_by": d.claimed_by,
        "claimed_at": d.claimed_at,
        "original": original_words(d),
        "parent": d.parent,
        "parent_ref": format_ref(d.parent, d.letter) if d.parent is not None else None,
        "children": d.children or [],
        "default": d.default,
        "order": d.order,
        "kind": d.kind,
        "urgency": d.urgency or "normal",
        "due": d.due,
        "remind_at": d.remind_at,
        "milestone": bool(d.milestone),
        "important": d.urgency == "high",
        "if_no_answer": d.if_no_answer,
        "reversible": d.reversible,
        "session": d.session,
        "supersedes": d.supersedes or [],
        "superseded_by": d.superseded_by,
        "options": [{"key": o.key, "label": o.label, "body": o.body} for o in d.options],
        "answer": (_answer_json(d.answer) if d.answer else None),
        "questions": [
            {"key": q.key, "label": q.label, "default": q.default,
             "options": [{"key": o.key, "label": o.label, "body": o.body} for o in q.options]}
            for q in d.questions
        ],
        "answers": d.answer.answers if d.answer else None,
        "answer_notes": d.answer.answer_notes if d.answer else None,
        "partial_answer": (
            {"answers": d.partial_answer.answers, "answer_notes": d.partial_answer.answer_notes,
             "by": d.partial_answer.by, "at": d.partial_answer.at}
            if d.partial_answer else None
        ),
        "previous_rounds": [
            {
                "round": r.round,
                "options": [{"key": o.key, "label": o.label, "body": o.body} for o in r.options],
                "questions": [
                    {"key": q.key, "label": q.label, "default": q.default,
                     "options": [{"key": o.key, "label": o.label, "body": o.body} for o in q.options]}
                    for q in r.questions
                ],
                "answer": r.answer, "sub_answers": r.sub_answers, "by": r.by, "at": r.at,
            }
            for r in d.previous_rounds
        ],
        "notes": [
            {
                "kind": n.kind, "role": n.role, "author": n.author, "at": n.at, "text": n.text,
                "summary": n.summary,
                **({"relayed": True} if n.relayed else {}),
                **({"sent": {
                    "channel": n.sent.channel, "to_name": n.sent.to_name, "to_id": n.sent.to_id,
                    "at": n.sent.at, "message_id": n.sent.message_id,
                }} if n.sent else {}),
                "attachments": [_attachment_json(a) for a in n.attachments],
            }
            for n in d.notes
        ],
        "queued": bool(d.queued),
        "archived_at": d.archived_at,
        "reopened_at": d.reopened_at,
        "reopened_after_archive": reopened_after_archive(d),
        "tags": d.tags,
        "attachments": [_attachment_json(a) for a in d.attachments],
        "body_attachments": [_attachment_json(a) for a in d.body_attachments],
        "approvals": [
            {
                "target": ap.target, "hash": ap.hash, "approve_on": ap.approve_on,
                "approved_hash": ap.approved_hash, "approved_by": ap.approved_by,
                "approved_at": ap.approved_at, "declined": ap.declined,
            }
            for ap in d.approvals
        ],
        "run": d.run,
        "body": d.body,
        "file": d.file,
        "archived": d.archived,
    }
