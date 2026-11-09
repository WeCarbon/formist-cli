"""Reading ``decisions/*.md`` — a port of noddit's ``core/parse.ts``.

Byte-compatibility with `nod` (contract §6.3) lives here and in
``task_serialize``: these two modules are the inverse of each other, and
every regex/heading shape below is transcribed from the TS source, not
re-derived.
"""

from __future__ import annotations

import os
import re

from formist_cli import task_yaml
from formist_cli.task_format import ATTACHMENT_GROUP_END, NOTE_KIND, OPTION_LABEL, is_answer_kind
from formist_cli.task_model import (
    ITEM_STATUSES,
    Answer,
    Attachment,
    AttachmentApproval,
    Node,
    Note,
    NoteSent,
    Option,
    PartialAnswer,
    PreviousRound,
)
from formist_cli.task_paths import ENTRY_NAME_RE
from formist_cli.task_questions import parse_questions
from formist_cli.task_status import with_open_question

FM_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?")

OPTIONS_HEADING_RE = re.compile(
    r"^##[ \t]+(?:选项|各选项|选项与代价|可选项|Options)(?:[（(]第[ \t]*(\d+)[ \t]*轮[）)])?[ \t　]*[:：]?[ \t]*$"
)
_OPTION_RE = re.compile(r"^###[ \t]+([A-Za-z0-9]{1,4})[ \t]*(?:[·:：．.\-—–]{1,2}[ \t]*(.*))?$")
_H2_RE = re.compile(r"^##[ \t]+\S")
ATTACHMENTS_HEADING_RE = re.compile(r"^##[ \t]+(附件|Attachments)[ \t　]*[:：]?[ \t]*$")
ATTACHMENT_RE = re.compile(r"^[-*][ \t]+(!)?\[([^\]]*)\]\(((?:[^()]|\([^()]*\))+)\)")
#: ``**<group>**`` on its own line — a group heading over the attachments below it.
ATTACHMENT_GROUP_RE = re.compile(r"^\*\*([^*]+)\*\*[ \t]*$")
ATTACHMENT_CAPTION_RE = re.compile(r"^\*([^*].*[^*]|[^*])\*[ \t]*$")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n") if "\r" in text else text


def _normalize_target_slashes(target: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.\-]*://", target, re.IGNORECASE):
        return target
    return target.replace("\\", "/")


def split_front_matter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = task_yaml.load(m.group(1))
    except task_yaml.YamlError as e:
        raise ValueError(f"front matter is not valid: {e}") from e
    return data, text[m.end():]


def options_round_of(body: str) -> int:
    best = 0
    for line in body.split("\n"):
        m = OPTIONS_HEADING_RE.match(line)
        if m:
            best = max(best, int(m.group(1) or 1))
    return best


def parse_options(body: str, round_: int | None = None) -> list[Option]:
    lines = body.split("\n")
    out: list[Option] = []
    cur_round = 0
    in_block = False
    current: Option | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current, buf
        if current is not None:
            current.body = "\n".join(buf).strip()
            out.append(current)
        current = None
        buf = []

    for line in lines:
        head = OPTIONS_HEADING_RE.match(line)
        if head:
            flush()
            r = int(head.group(1) or 1)
            if round_ is None:
                if r > cur_round:
                    cur_round = r
                    out.clear()
                in_block = r == cur_round
            else:
                in_block = r == round_
            continue
        if in_block and _H2_RE.match(line):
            flush()
            in_block = False
            continue
        if not in_block:
            continue
        m = _OPTION_RE.match(line)
        if m:
            flush()
            current = Option(key=m.group(1), label=(m.group(2) or "").strip())
            continue
        if current is not None:
            buf.append(line)
    flush()
    return out


def parse_attachments(body: str) -> list[Attachment]:
    out: list[Attachment] = []
    in_block = False
    group: str | None = None
    last: Attachment | None = None
    for line in body.split("\n"):
        if ATTACHMENTS_HEADING_RE.match(line):
            in_block = True
            group = None
            last = None
            continue
        if in_block and _H2_RE.match(line):
            break
        if not in_block:
            continue
        trimmed = line.strip()
        if not trimmed:
            last = None
            continue
        if trimmed == ATTACHMENT_GROUP_END:
            group = None
            last = None
            continue
        g = ATTACHMENT_GROUP_RE.match(trimmed)
        if g:
            group = g.group(1).strip()
            last = None
            continue
        m = ATTACHMENT_RE.match(trimmed)
        if m:
            a = Attachment(
                image=bool(m.group(1)), label=m.group(2).strip(),
                target=_normalize_target_slashes(m.group(3).strip()), group=group,
            )
            out.append(a)
            last = a
            continue
        c = ATTACHMENT_CAPTION_RE.match(trimmed)
        if c and last:
            last.caption = c.group(1).strip()
            last = None
            continue
        last = None
    return out


def parse_approvals(data: dict) -> list[AttachmentApproval]:
    raw_list = data.get("approvals")
    if not isinstance(raw_list, list):
        return []
    out: list[AttachmentApproval] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        target = _str(raw.get("target"))
        hash_ = _str(raw.get("hash"))
        if not target or not hash_:
            continue
        approve_on = raw.get("approve_on")
        out.append(AttachmentApproval(
            target=target, hash=hash_,
            approved_hash=_str(raw.get("approved_hash")),
            approved_at=_str(raw.get("approved_at")),
            approved_by=_str(raw.get("approved_by")),
            approve_on=[str(x) for x in approve_on] if isinstance(approve_on, list) else None,
            declined=_str(raw.get("declined")),
        ))
    return out


_NOTE_HEADING_RE = re.compile(r"^##[ \t]+([^\n（(]+?)[ \t]*[（(]([^\n]+)[）)][ \t]*$")
_NOTE_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?$")
_ROLES = ("human", "agent")


class NoteHeading:
    __slots__ = ("kind", "role", "author", "at", "precision_ms")

    def __init__(self, kind: str, role: str, author: str, at: str, precision_ms: int):
        self.kind = kind
        self.role = role
        self.author = author
        self.at = at
        self.precision_ms = precision_ms


def _heading_time(date: str, hm: str, sec: str | None) -> str:
    """Local wall-clock time from a heading, stamped with THIS machine's
    current UTC offset — matches `nod`'s own lossy-across-timezones
    behavior exactly (see the C1052 research spec §3.2), not a "fix"."""
    import time

    offset_seconds = -time.timezone if (time.localtime().tm_isdst == 0) else -time.altzone
    sign = "+" if offset_seconds >= 0 else "-"
    off = abs(offset_seconds)
    oh, om = divmod(off // 60, 60)
    return f"{date}T{hm}{sec or ':00'}{sign}{oh:02d}:{om:02d}"


def parse_note_heading(line: str) -> NoteHeading | None:
    m = _NOTE_HEADING_RE.match(line)
    if not m:
        return None
    parts = [p.strip() for p in re.split(r"[ \t]*·[ \t]*", m.group(2))]
    t = _NOTE_TIME_RE.match(parts[-1]) if parts else None
    if not t or len(parts) < 2:
        return None
    who = parts[:-1]
    if who[0] in _ROLES:
        role = who[0]
        who = who[1:]
    elif who[0] == "owner":
        role = "human"
    else:
        role = "agent"
    author = " · ".join(who) or ("owner" if role == "human" else "")
    return NoteHeading(
        kind=m.group(1).strip(), role=role, author=author,
        at=_heading_time(t.group(1), t.group(2), t.group(3)),
        precision_ms=1000 if t.group(3) else 60_000,
    )


_WRITER_MARKER_RE = re.compile(r"\n?<!--[ \t]*nod:by=([^\n]*?)[ \t]*-->[ \t]*$")
_SUMMARY_MARKER_RE = re.compile(r"\n?<!--[ \t]*nod:summary=([^\n]*?)[ \t]*-->[ \t]*$")
_RELAYED_MARKER_RE = re.compile(r"\n?<!--[ \t]*nod:relayed[ \t]*-->[ \t]*$")
_SENT_MARKER_RE = re.compile(r"\n?<!--[ \t]*nod:sent[ \t]+([^\n]*?)[ \t]*-->[ \t]*$")
_SENT_ATTR_RE = re.compile(r'([a-z-]+)="((?:\\.|[^"\\])*)"')


def _parse_sent_attrs(raw: str) -> NoteSent:
    attrs: dict[str, str] = {}
    for m in _SENT_ATTR_RE.finditer(raw):
        attrs[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    return NoteSent(
        channel=attrs.get("channel", ""),
        to_name=attrs.get("to-name"),
        to_id=attrs.get("to-id"),
        at=attrs.get("at"),
        message_id=attrs.get("message-id"),
    )


_LEGACY_SENT_SUMMARY_RE = re.compile(r"^(feishu sent|mail sent|mail reply sent):", re.IGNORECASE)
_LEGACY_SENT_FIELD_RE = re.compile(r"^channel:[ \t]*(.+)$", re.MULTILINE)


def _parse_legacy_sent(summary: str | None, text: str) -> NoteSent | None:
    m = _LEGACY_SENT_FIELD_RE.search(text)
    channel_line = m.group(1) if m else None
    if not channel_line and not (summary and _LEGACY_SENT_SUMMARY_RE.match(summary)):
        return None
    probe = channel_line or summary or ""
    channel = "feishu" if re.search("feishu", probe, re.IGNORECASE) else (
        "mail" if re.search("email|mail", probe, re.IGNORECASE) else (channel_line or "unknown")
    )
    to_m = re.search(r"^to:[ \t]*(.+)$", text, re.MULTILINE)
    at_m = re.search(r"^sent:[ \t]*(.+)$", text, re.MULTILINE)
    mid_m = re.search(r"^message id:[ \t]*(.+)$", text, re.MULTILINE)
    return NoteSent(
        channel=channel, to_id=to_m.group(1) if to_m else None,
        at=at_m.group(1) if at_m else None, message_id=mid_m.group(1) if mid_m else None,
        legacy=True,
    )


_RELAYED_SIGNATURE_RE = re.compile(
    r"转记|轉記|对话里说|對話裡說|said in chat|relayed by|logged by|\brelayed\b|"
    r"dit dans la conversation|consigné par|قاله في المحادثة|نقله|سجّله",
    re.IGNORECASE,
)


def is_relayed_note(role: str, author: str, relayed: bool = False) -> bool:
    return role == "human" and (relayed or bool(_RELAYED_SIGNATURE_RE.search(author)))


def _section_attachments(body: str) -> tuple[list[Attachment], list[list[Attachment]]]:
    lines = body.split("\n")
    note_heading_at = {i for i, l in enumerate(lines) if parse_note_heading(l)}
    note_count = len(note_heading_at)
    buckets: list[list[str]] = [[] for _ in range(note_count + 1)]
    owner = 0
    note_seen = 0
    in_attachments = False
    for i, line in enumerate(lines):
        if i in note_heading_at:
            note_seen += 1
            owner = note_seen
            in_attachments = False
            continue
        if _H2_RE.match(line):
            in_attachments = bool(ATTACHMENTS_HEADING_RE.match(line))
            if in_attachments:
                buckets[owner].append(line)
            continue
        if in_attachments:
            buckets[owner].append(line)
    body_lines, *note_lines = buckets
    return parse_attachments("\n".join(body_lines)), [parse_attachments("\n".join(b)) for b in note_lines]


_SUB_ANSWER_LINE_RE = re.compile(r"^-[ \t]+(\S+)[ \t]*=[ \t]*(\S+)[ \t]*——")
_REPLY_KINDS = (NOTE_KIND["reply"], NOTE_KIND["follow_up"])


def parse_notes(body: str) -> list[Note]:
    lines = body.split("\n")
    _, note_attachments = _section_attachments(body)
    out: list[Note] = []
    note_index = 0
    answered_this_round = False
    for i, line in enumerate(lines):
        if OPTIONS_HEADING_RE.match(line):
            answered_this_round = False
        h = parse_note_heading(line)
        if not h:
            continue
        revises = h.kind == NOTE_KIND["answered"] and answered_this_round
        if h.kind == NOTE_KIND["answered"]:
            answered_this_round = True
        end = i + 1
        while end < len(lines) and not _H2_RE.match(lines[end]):
            end += 1
        text = "\n".join(lines[i + 1:end]).strip()
        m = _WRITER_MARKER_RE.search(text)
        writer = m.group(1) if m else None
        if m:
            text = text[:m.start()].strip()
        rm = _RELAYED_MARKER_RE.search(text)
        if rm:
            text = text[:rm.start()].strip()
        relayed = is_relayed_note(h.role, h.author, bool(rm))
        sm = _SUMMARY_MARKER_RE.search(text)
        summary = sm.group(1) if sm else None
        if sm:
            text = text[:sm.start()].strip()
        sent_m = _SENT_MARKER_RE.search(text)
        if sent_m:
            text = text[:sent_m.start()].strip()
        sent = _parse_sent_attrs(sent_m.group(1)) if sent_m else _parse_legacy_sent(summary, text)
        out.append(Note(
            kind=h.kind, role=h.role, author=h.author, at=h.at, text=text,
            heading=re.sub(r"^##[ \t]+", "", lines[i]),
            writer=writer, summary=summary, revises=revises, relayed=relayed, sent=sent,
            attachments=note_attachments[note_index] if note_index < len(note_attachments) else [],
        ))
        note_index += 1
    return out


def round_withdrawn(body: str) -> bool:
    lines = body.split("\n")
    start = -1
    for i, l in enumerate(lines):
        if OPTIONS_HEADING_RE.match(l):
            start = i
    if start == -1:
        return False
    for i in range(start + 1, len(lines)):
        h = parse_note_heading(lines[i])
        if not h or h.role != "human" or h.kind not in _REPLY_KINDS:
            continue
        end = i + 1
        while end < len(lines) and not _H2_RE.match(lines[end]):
            end += 1
        marked = any(_RELAYED_MARKER_RE.search(l) for l in lines[i + 1:end])
        if not is_relayed_note(h.role, h.author, marked):
            return True
    return False


def _parse_partial_answer(raw, round_: int) -> PartialAnswer | None:
    if not isinstance(raw, dict):
        return None
    answers = raw.get("answers")
    if not isinstance(answers, dict) or not answers:
        return None
    if int(raw.get("round", 1) or 1) != round_:
        return None
    answer_notes = raw.get("answer_notes")
    return PartialAnswer(
        round=round_, answers={str(k): str(v) for k, v in answers.items()},
        answer_notes={str(k): str(v) for k, v in answer_notes.items()} if isinstance(answer_notes, dict) and answer_notes else None,
        by=_str(raw.get("by")) or "", at=_str(raw.get("at")) or "",
    )


def parse_previous_rounds(body: str) -> list[PreviousRound]:
    current_round = options_round_of(body)
    if current_round < 2:
        return []
    lines = body.split("\n")
    starts: dict[int, int] = {}
    for i, l in enumerate(lines):
        m = OPTIONS_HEADING_RE.match(l)
        if m:
            starts[int(m.group(1) or 1)] = i
    out: list[PreviousRound] = []
    for r in range(1, current_round):
        start = starts.get(r)
        if start is None:
            continue
        later = [s for s in starts.values() if s > start]
        limit = min([len(lines)] + later)
        answer_at = -1
        heading: NoteHeading | None = None
        for i in range(start + 1, limit):
            h = parse_note_heading(lines[i])
            if h and is_answer_kind(h.kind):
                heading = h
                answer_at = i
        headline = None
        sub_answers: dict[str, str] = {}
        if answer_at != -1:
            end = answer_at + 1
            while end < limit and not _H2_RE.match(lines[end]):
                end += 1
            text = "\n".join(lines[answer_at + 1:end]).strip()
            wm = _WRITER_MARKER_RE.search(text)
            if wm:
                text = text[:wm.start()].strip()
            for line in text.split("\n"):
                sm = _SUB_ANSWER_LINE_RE.match(line.strip())
                if sm:
                    sub_answers[sm.group(1)] = sm.group(2)
                elif not headline and line.strip():
                    headline = line.strip()
        out.append(PreviousRound(
            round=r, options=parse_options(body, r), questions=parse_questions(body, {}, r),
            answer=headline, sub_answers=sub_answers or None,
            by=heading.author if heading else None, at=heading.at if heading else None,
        ))
    return out


def _str(v) -> str | None:
    return None if v is None else str(v)


def _kind_of(raw, options: list[Option]) -> str:
    if raw == "fyi":
        return "fyi"
    if not options:
        return "task"
    if len(options) == 1 and options[0].label == OPTION_LABEL["accept"]:
        return "acceptance"
    return "decision"


def _state_of(raw, archived: bool, answered: bool, queued: bool | None) -> str:
    if isinstance(raw, str) and raw in ITEM_STATUSES:
        return raw
    if archived:
        return "completed"
    answered = raw == "answered" or (raw != "open" and answered)
    if not answered:
        return "needs_decision"
    return "decided" if queued else "in_progress"


_KNOWN_KEYS = {
    "id", "title", "author", "status", "created", "updated_at", "default", "order", "pin", "pinned_at",
    "urgency", "due", "remind_at", "milestone", "if_no_answer", "reversible", "kind", "session",
    "superseded_by", "supersedes", "close_reason", "waiting_on", "waiting_until", "deferred_at",
    "deferred_by", "deferred_until", "assignee", "answer", "answered_by", "answered_at", "archived_at",
    "reopened_at", "tags", "partial_answer", "question_defaults", "progress", "progress_msg", "after",
    "est", "every", "when", "stale_after", "steps", "owner_agent", "approvals", "run", "intake",
    "claimed_by", "claimed_at",
}


def _order_of(data: dict) -> int | None:
    raw = data.get("order")
    try:
        n = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        n = None
    if raw is not None and n is not None and n != 0:
        return int(n)
    pin = data.get("pin")
    return -1 if pin == "top" else (1 if pin == "bottom" else None)


def _str_list(v) -> list[str] | None:
    if isinstance(v, list):
        return [str(x) for x in v]
    return None if v is None else [str(v)]


def _str_map(v) -> dict[str, str] | None:
    if not isinstance(v, dict):
        return None
    return {str(k): str(x) for k, x in v.items()}


def parse_decision(text: str, file: str, archived: bool, project: str = "", queued: bool | None = None) -> Node:
    data, body = split_front_matter(normalize_newlines(text))
    name = os.path.basename(file)
    from_name = ENTRY_NAME_RE.match(name)

    raw_id = data.get("id") if data.get("id") is not None else (from_name.group(1) if from_name else None)
    if raw_id is None:
        raise ValueError(f"{name}: cannot determine id")
    try:
        id_ = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: cannot determine id") from None

    raw_answer = data.get("answer")
    has_answer = isinstance(raw_answer, str) or (isinstance(raw_answer, dict) and len(raw_answer) > 0)

    options = parse_options(body)
    withdrawn = round_withdrawn(body)
    state = with_open_question(
        _state_of(data.get("status"), archived, bool(has_answer), queued), options, has_answer, withdrawn
    )
    notes = parse_notes(body)
    body_attachments, note_attachments = _section_attachments(body)
    extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    answer: Answer | None = None
    partial_answer: PartialAnswer | None = None
    if has_answer:
        if isinstance(raw_answer, str):
            answer = Answer(choice=raw_answer, by=_str(data.get("answered_by")) or "owner",
                             at=_str(data.get("answered_at")) or "")
        else:
            answer = Answer(
                choice=_str(raw_answer.get("choice")),
                answers=_str_map(raw_answer.get("answers")) or None,
                answer_notes=_str_map(raw_answer.get("answer_notes")) or None,
                note=_str(raw_answer.get("note")),
                by=_str(raw_answer.get("by")) or "owner", at=_str(raw_answer.get("at")) or "",
            )
    else:
        partial_answer = _parse_partial_answer(data.get("partial_answer"), max(1, options_round_of(body)))

    urgency = data.get("urgency") if data.get("urgency") in ("high", "low") else "normal"
    progress = data.get("progress")
    try:
        progress_val = int(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress_val = None

    superseded_by = data.get("superseded_by")
    try:
        superseded_by_val = int(superseded_by) if superseded_by is not None else None
    except (TypeError, ValueError):
        superseded_by_val = None

    supersedes_raw = data.get("supersedes")
    supersedes = []
    if isinstance(supersedes_raw, list):
        for x in supersedes_raw:
            try:
                supersedes.append(int(x))
            except (TypeError, ValueError):
                pass

    node = Node(
        id=id_,
        title=_str(data.get("title")) or str(id_),
        author=_str(data.get("author")) or "",
        state=state,
        close_reason=_str(data.get("close_reason")),
        waiting_on=_str(data.get("waiting_on")),
        waiting_until=_str(data.get("waiting_until")),
        deferred_at=_str(data.get("deferred_at")),
        deferred_by=_str(data.get("deferred_by")) if data.get("deferred_at") else None,
        deferred_until=_str(data.get("deferred_until")) if data.get("deferred_at") else None,
        assignee=(lambda v: (None if not v or str(v).strip().lower() == "ai" else str(v).strip()))(data.get("assignee")),
        created=_str(data.get("created")) or "",
        updated_at=_str(data.get("updated_at")),
        progress=progress_val,
        progress_msg=_str(data.get("progress_msg")),
        after=_str_list(data.get("after")) or [],
        est=_str(data.get("est")),
        every=_str(data.get("every")),
        when=_str(data.get("when")),
        stale_after=_str(data.get("stale_after")),
        steps=_parse_steps(data.get("steps")),
        owner_agent=_str(data.get("owner_agent")),
        intake=data.get("intake") is True,
        claimed_by=_str(data.get("claimed_by")),
        claimed_at=_str(data.get("claimed_at")),
        extra=extra,
        default=_str(data.get("default")),
        order=_order_of(data),
        urgency=urgency,
        due=_str(data.get("due")),
        remind_at=_str(data.get("remind_at")),
        milestone=data.get("milestone") is True,
        if_no_answer=_str(data.get("if_no_answer")),
        reversible=data.get("reversible") if isinstance(data.get("reversible"), bool) else None,
        kind=_kind_of(data.get("kind"), options),
        session=_str(data.get("session")),
        superseded_by=superseded_by_val,
        supersedes=supersedes,
        answer=answer,
        partial_answer=partial_answer,
        archived_at=_str(data.get("archived_at")),
        reopened_at=_str(data.get("reopened_at")),
        tags=_str_list(data.get("tags")) or [],
        body=body,
        options=options,
        questions=parse_questions(body, _str_map(data.get("question_defaults")) or {}, max(1, options_round_of(body))),
        previous_rounds=parse_previous_rounds(body),
        round_withdrawn=withdrawn,
        attachments=body_attachments + [a for notes_a in note_attachments for a in notes_a],
        body_attachments=body_attachments,
        notes=notes,
        approvals=parse_approvals(data),
        run=data.get("run") if isinstance(data.get("run"), dict) else None,
        file=file,
        slug=(from_name.group(2) or "") if from_name else "",
        archived=archived,
        project=project,
        queued=bool(queued),
    )
    return node


def _parse_steps(raw):
    from formist_cli.task_model import Step

    if not isinstance(raw, list):
        return None
    out = []
    for s in raw:
        if isinstance(s, str):
            out.append(Step(label=s, done=False))
        else:
            o = s if isinstance(s, dict) else {}
            out.append(Step(label=str(o.get("label")) if o.get("label") is not None else "", done=bool(o.get("done"))))
    return out
