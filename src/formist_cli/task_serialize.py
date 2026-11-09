"""Writing ``decisions/*.md`` — a port of noddit's ``core/serialize.ts``.

The inverse of ``task_parse``; see that module's docstring for the
byte-compatibility rationale.
"""

from __future__ import annotations

import os
import re

from formist_cli import task_yaml
from formist_cli.task_format import ATTACHMENT_GROUP_END, HEADING, options_heading
from formist_cli.task_identity import current_identity
from formist_cli.task_model import Attachment, Node, NoteSent, Option
from formist_cli.task_paths import to_posix_path

KEY_ORDER = [
    "id", "title", "author", "status", "close_reason", "waiting_on", "waiting_until",
    "deferred_at", "deferred_by", "deferred_until", "assignee", "created", "updated_at",
    "after", "progress", "progress_msg", "est", "every", "when", "stale_after", "steps",
    "owner_agent", "intake", "claimed_by", "claimed_at",
    "kind", "urgency", "due", "remind_at", "milestone", "default", "question_defaults",
    "if_no_answer", "reversible",
    "order", "tags", "session", "supersedes", "superseded_by", "answer", "partial_answer",
    "approvals", "run",
    "archived_at", "reopened_at",
]


def front_matter_of(d: Node) -> dict:
    fm: dict = {"id": d.id, "title": d.title, "author": d.author, "status": d.state, "created": d.created}
    if d.close_reason:
        fm["close_reason"] = d.close_reason
    if d.waiting_on:
        fm["waiting_on"] = d.waiting_on
    if d.state == "waiting" and d.waiting_until:
        fm["waiting_until"] = d.waiting_until
    if d.deferred_at:
        fm["deferred_at"] = d.deferred_at
    if d.deferred_at and d.deferred_by:
        fm["deferred_by"] = d.deferred_by
    if d.deferred_at and d.deferred_until:
        fm["deferred_until"] = d.deferred_until
    if d.assignee:
        fm["assignee"] = d.assignee
    if d.updated_at:
        fm["updated_at"] = d.updated_at
    if d.after:
        fm["after"] = list(d.after)
    if d.progress is not None:
        fm["progress"] = d.progress
    if d.progress_msg:
        fm["progress_msg"] = d.progress_msg
    if d.est:
        fm["est"] = d.est
    if d.every:
        fm["every"] = d.every
    if d.when:
        fm["when"] = d.when
    if d.stale_after:
        fm["stale_after"] = d.stale_after
    if d.steps is not None:
        fm["steps"] = [{"label": s.label, "done": s.done} for s in d.steps]
    if d.owner_agent:
        fm["owner_agent"] = d.owner_agent
    if d.intake:
        fm["intake"] = True
    if d.claimed_by:
        fm["claimed_by"] = d.claimed_by
    if d.claimed_at:
        fm["claimed_at"] = d.claimed_at
    if d.default:
        fm["default"] = d.default
    qd = {q.key: q.default for q in d.questions if q.default}
    if qd:
        fm["question_defaults"] = qd
    if d.kind == "fyi":
        fm["kind"] = "fyi"
    if d.urgency and d.urgency != "normal":
        fm["urgency"] = d.urgency
    if d.due:
        fm["due"] = d.due
    if d.remind_at:
        fm["remind_at"] = d.remind_at
    if d.milestone:
        fm["milestone"] = True
    if d.if_no_answer:
        fm["if_no_answer"] = d.if_no_answer
    if d.reversible is not None:
        fm["reversible"] = d.reversible
    if d.session:
        fm["session"] = d.session
    if d.supersedes:
        fm["supersedes"] = list(d.supersedes)
    if d.superseded_by is not None:
        fm["superseded_by"] = d.superseded_by
    if d.order:
        fm["order"] = d.order
    if d.tags:
        fm["tags"] = list(d.tags)
    if d.archived_at:
        fm["archived_at"] = d.archived_at
    if d.reopened_at:
        fm["reopened_at"] = d.reopened_at
    if d.answer:
        a: dict = {}
        if d.answer.choice:
            a["choice"] = d.answer.choice
        if d.answer.answers:
            a["answers"] = d.answer.answers
        if d.answer.answer_notes:
            a["answer_notes"] = d.answer.answer_notes
        if d.answer.note:
            a["note"] = d.answer.note
        a["by"] = d.answer.by
        a["at"] = d.answer.at
        fm["answer"] = a
    if d.partial_answer:
        p = d.partial_answer
        pa = {"round": p.round, "answers": p.answers}
        if p.answer_notes:
            pa["answer_notes"] = p.answer_notes
        pa["by"] = p.by
        pa["at"] = p.at
        fm["partial_answer"] = pa
    if d.approvals:
        apps = []
        for ap in d.approvals:
            a = {"target": ap.target, "hash": ap.hash}
            if ap.approved_hash:
                a["approved_hash"] = ap.approved_hash
            if ap.approved_at:
                a["approved_at"] = ap.approved_at
            if ap.approved_by:
                a["approved_by"] = ap.approved_by
            if ap.approve_on:
                a["approve_on"] = list(ap.approve_on)
            if ap.declined:
                a["declined"] = ap.declined
            apps.append(a)
        fm["approvals"] = apps
    if d.run:
        fm["run"] = d.run
    for k, v in (d.extra or {}).items():
        if k not in fm:
            fm[k] = v
    return fm


def serialize_node(d: Node) -> str:
    fm = front_matter_of(d)
    ordered: dict = {}
    for k in KEY_ORDER:
        if k in fm:
            ordered[k] = fm[k]
    for k in fm:
        if k not in ordered:
            ordered[k] = fm[k]
    yaml_text = task_yaml.dump(ordered)
    body = re.sub(r"^\n+", "", d.body)
    if not body.endswith("\n"):
        body += "\n"
    return f"---\n{yaml_text}---\n\n{body}"


def render_options_block(options: list[Option], round_: int = 1) -> str:
    if not options:
        return ""
    parts = [f"## {options_heading(round_)}", ""]
    for o in options:
        parts.append(f"### {o.key}{f' · {o.label}' if o.label else ''}")
        if o.body.strip():
            parts.append("")
            parts.append(o.body.strip())
        parts.append("")
    return "\n".join(parts)


_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|bmp|heic)$", re.IGNORECASE)


def is_image_path(p: str) -> bool:
    return bool(_IMAGE_EXT_RE.search(re.split(r"[?#]", p)[0]))


def render_attachments_block(items: list[Attachment]) -> str:
    if not items:
        return ""
    lines = [f"## {HEADING['attachments']}", ""]
    group: str | None = None
    for a in items:
        if a.group != group:
            group = a.group
            if len(lines) > 2:
                lines.append("")
            lines.append(f"**{group}**" if group else ATTACHMENT_GROUP_END)
            lines.append("")
        lines.append(f"- {'!' if a.image else ''}[{a.label}]({a.target})")
        if a.caption:
            lines.append(f"  *{a.caption}*")
    lines.append("")
    return "\n".join(lines)


_NEXT_H2 = re.compile(r"^##[ \t]+\S")


def upsert_section(body: str, heading: str, block: str) -> str:
    re_heading = re.compile(rf"^##[ \t]+{re.escape(heading)}[ \t　]*[:：]?[ \t]*$")
    lines = body.split("\n")
    start = next((i for i, l in enumerate(lines) if re_heading.match(l)), -1)

    if start == -1:
        if not block.strip():
            return body
        return re.sub(r"\s+$", "", body) + f"\n\n{block.strip()}\n"

    end = start + 1
    while end < len(lines) and not _NEXT_H2.match(lines[end]):
        end += 1

    head = re.sub(r"\s+$", "", "\n".join(lines[:start]))
    tail = re.sub(r"^\s+", "", "\n".join(lines[end:]))
    middle = ["", block.strip(), ""] if block.strip() else [""]
    joined = "\n".join([head, *middle, tail])
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return re.sub(r"\s+$", "", joined) + "\n"


def upsert_section_scoped(
    body: str, anchor: str | None, stop_before: str | None, heading: str, block: str, nth: int = 0,
) -> str:
    lines = body.split("\n")
    start = 0
    if anchor is not None:
        hi = -1
        seen = 0
        for i, l in enumerate(lines):
            if l != f"## {anchor}":
                continue
            if seen == nth:
                hi = i
                break
            seen += 1
        if hi == -1:
            raise ValueError(f"no such section: {anchor}")
        start = hi + 1
    end = len(lines)
    if stop_before is not None:
        for i in range(start, len(lines)):
            if lines[i] == f"## {stop_before}":
                end = i
                break
    span = upsert_section("\n".join(lines[start:end]), heading, block)
    return "\n".join([*lines[:start], *span.split("\n"), *lines[end:]])


def note_heading(kind: str, role: str, author: str, at: str) -> str:
    when = at[:19].replace("T", " ")
    clean_author = re.sub(r"[（）()]", " ", author).strip() or role
    return f"{kind}（{role} · {clean_author} · {when}）"


def relink_target(target: str, from_dir: str, to_dir: str, rename=lambda p: p) -> str:
    if re.match(r"^[a-z][a-z0-9+.\-]*:", target, re.IGNORECASE) or target.startswith("#") or os.path.isabs(target):
        return target
    abs_path = rename(os.path.normpath(os.path.join(from_dir, target)))
    rel = to_posix_path(os.path.relpath(abs_path, to_dir))
    return rel if rel != target else target


_LINK_RE = re.compile(r"(\]\()((?:[^()\s]|\([^()\s]*\))+)(\))")


def relink_body(body: str, from_dir: str, to_dir: str, rename=lambda p: p) -> str:
    return _LINK_RE.sub(lambda m: f"{m.group(1)}{relink_target(m.group(2), from_dir, to_dir, rename)}{m.group(3)}", body)


_FM_PREFIX_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---[ \t]*(\r?\n|$)")
_TARGET_LINE_RE = re.compile(r"^(\s*(?:-\s+)?target:\s*)(['\"]?)([^'\"\r\n]+)\2[ \t]*$", re.MULTILINE)


def relink_file(text: str, from_dir: str, to_dir: str, rename=lambda p: p) -> str:
    m = _FM_PREFIX_RE.match(text)
    if not m:
        return relink_body(text, from_dir, to_dir, rename)
    fm = _TARGET_LINE_RE.sub(
        lambda mm: f"{mm.group(1)}{mm.group(2)}{relink_target(mm.group(3).strip(), from_dir, to_dir, rename)}{mm.group(2)}",
        text[:m.end()],
    )
    return fm + relink_body(text[m.end():], from_dir, to_dir, rename)


def writer_marker() -> str:
    identity = current_identity()
    return f"<!-- nod:by={identity} -->" if identity else ""


def summary_marker(summary: str) -> str:
    return f"<!-- nod:summary={summary} -->"


RELAYED_MARKER = "<!-- nod:relayed -->"


def _esc_marker_attr(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def sent_marker(sent: NoteSent) -> str:
    attrs = [
        ("channel", sent.channel), ("to-name", sent.to_name), ("to-id", sent.to_id),
        ("at", sent.at), ("message-id", sent.message_id),
    ]
    rendered = " ".join(f'{k}="{_esc_marker_attr(v)}"' for k, v in attrs if v)
    return f"<!-- nod:sent {rendered} -->"


def append_section(
    body: str, heading: str, text: str, summary: str | None = None,
    relayed: bool = False, sent: NoteSent | None = None,
) -> str:
    base = re.sub(r"\s+$", "", body)
    marker = writer_marker()
    content = text.strip()
    if sent:
        content += f"\n{sent_marker(sent)}"
    if summary:
        content += f"\n{summary_marker(summary)}"
    if relayed:
        content += f"\n{RELAYED_MARKER}"
    if marker:
        content += f"\n{marker}"
    return f"{base}\n\n## {heading}\n\n{content}\n"


_SLUG_STRIP_RE = re.compile(r"[^0-9a-zA-Z一-鿿぀-ヿ]+")


def slugify(title: str, max_: int = 20) -> str:
    s = _SLUG_STRIP_RE.sub("-", title.lower()).strip("-")
    s = s[:max_].rstrip("-")
    return s or "entry"
