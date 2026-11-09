"""Sub-questions — a port of noddit's ``core/questions.ts``."""

from __future__ import annotations

import re

from formist_cli.task_format import (
    DEFAULT_MARK,
    OPTION_LABEL,
    all_defaults_label,
    item_by_item_label,
    questions_heading,
)
from formist_cli.task_model import Option, Question

QUESTION_KEY_RE = re.compile(r"^[A-Z]\d{1,3}$")
OTHER_KEY = "*"

QUESTIONS_HEADING_RE = re.compile(r"^##[ \t]+(子问题|Questions)(?:[（(]第[ \t]*(\d+)[ \t]*轮[）)])?[ \t　]*[:：]?[ \t]*$")
_QUESTION_RE = re.compile(r"^###[ \t]+([A-Z]\d{1,3})[ \t]*(?:[·:：．.\-—–]{1,2}[ \t]*(.*))?$")
_SUB_OPTION_RE = re.compile(r"^####[ \t]+([A-Za-z0-9]{1,4})[ \t]*(?:[·:：．.\-—–]{1,2}[ \t]*(.*))?$")
_H2_RE = re.compile(r"^##[ \t]+\S")

REVIEW_OPTIONS = [
    Option(key="1", label=OPTION_LABEL["review_pass"]),
    Option(key="2", label=OPTION_LABEL["review_fail"]),
    Option(key="3", label=OPTION_LABEL["review_change"]),
]
REVIEW_DEFAULT = "2"


class QuestionsError(ValueError):
    pass


def parse_questions(body: str, defaults: dict[str, str] | None = None, current_round: int = 1) -> list[Question]:
    defaults = defaults or {}
    out: list[Question] = []
    in_section = False
    collecting = False
    q: Question | None = None
    opt: Option | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, opt, q
        text = "\n".join(buf).strip()
        if opt is not None:
            opt.body = text
        elif q is not None:
            q.body = text
        buf = []

    for line in body.split("\n"):
        hm = QUESTIONS_HEADING_RE.match(line)
        if hm:
            flush()
            in_section = True
            collecting = int(hm.group(2) or 1) == current_round
            q = opt = None
            continue
        if in_section and _H2_RE.match(line):
            flush()
            in_section = False
            collecting = False
            q = opt = None
            continue
        if not collecting:
            continue
        qm = _QUESTION_RE.match(line)
        if qm:
            flush()
            q = Question(key=qm.group(1), label=(qm.group(2) or "").strip(), default=defaults.get(qm.group(1)))
            opt = None
            out.append(q)
            continue
        om = _SUB_OPTION_RE.match(line) if q else None
        if om and q:
            flush()
            opt = Option(key=om.group(1), label=(om.group(2) or "").strip())
            q.options.append(opt)
            continue
        buf.append(line)
    flush()
    return out


def render_questions_block(qs: list[Question], round_: int = 1) -> str:
    if not qs:
        return ""
    parts = [f"## {questions_heading(round_)}", ""]
    for q in qs:
        parts.append(f"### {q.key}{f' · {q.label}' if q.label else ''}")
        parts.append("")
        if q.body.strip():
            parts.append(q.body.strip())
            parts.append("")
        for o in q.options:
            parts.append(f"#### {o.key}{f' · {o.label}' if o.label else ''}")
            if o.body.strip():
                parts.append("")
                parts.append(o.body.strip())
            parts.append("")
    return "\n".join(parts)


def question_options(qs: list[Question]) -> list[Option]:
    defaults = " ".join(f"{q.key}={q.default}" for q in qs)
    keys = " ".join(f"{q.key}=…" for q in qs)
    return [
        Option(key="1", label=all_defaults_label(defaults)),
        Option(key="2", label=item_by_item_label(keys)),
    ]


def check_questions(qs: list[Question]) -> None:
    seen: set[str] = set()
    for q in qs:
        if not QUESTION_KEY_RE.match(q.key):
            raise QuestionsError(f"bad sub-question key: {q.key!r}")
        if q.key in seen:
            raise QuestionsError(f"sub-question {q.key} given twice")
        seen.add(q.key)
        if not q.options:
            raise QuestionsError(f"sub-question {q.key} has no options")
        keys = [o.key for o in q.options]
        if len(set(keys)) != len(keys):
            raise QuestionsError(f"sub-question {q.key} has a repeated option key")
        if not q.default:
            raise QuestionsError(f"sub-question {q.key} needs a --default")
        if q.default not in keys:
            raise QuestionsError(f"sub-question {q.key}'s default {q.default!r} is not one of its options")


def render_answer_lines(qs: list[Question], answers: dict[str, str], answer_notes: dict[str, str] | None = None) -> str:
    answer_notes = answer_notes or {}
    lines = []
    for q in qs:
        v = answers.get(q.key)
        other = v == OTHER_KEY
        o = next((x for x in q.options if x.key == v), None)
        label = OPTION_LABEL["other"] if other else (o.label if o else "")
        note = answer_notes.get(q.key)
        mark = DEFAULT_MARK if (not other and v == q.default) else ""
        note_part = f"：{note}" if note else ""
        lines.append(f"- {q.key} = {v} —— {label}{mark}{note_part}")
    return "\n".join(lines)


_ANSWER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<key>[A-Z]\d{1,3})[ \t]*[=＝][ \t]*"
    r"(?:\*[:：](?P<other>[^\n]*?)(?=[ \t]+[A-Z]\d{1,3}[ \t]*[=＝]|\n|$)"
    r"|(?P<opt>[A-Za-z0-9]{1,4})(?![A-Za-z0-9])(?:[:：](?P<note>[^\n，,；;]*))?)"
)


def parse_answer_tokens(text: str, qs: list[Question]) -> tuple[dict[str, str], dict[str, str]]:
    answers: dict[str, str] = {}
    notes: dict[str, str] = {}
    by_key = {q.key: q for q in qs}
    for m in _ANSWER_TOKEN_RE.finditer(text):
        key = m.group("key")
        q = by_key.get(key)
        if not q:
            continue
        other = m.group("other")
        if other is not None:
            typed = other.strip()
            if not typed:
                continue
            answers[key] = OTHER_KEY
            notes[key] = typed
            continue
        opt = m.group("opt")
        if not opt or not any(o.key == opt for o in q.options):
            continue
        answers[key] = opt
        note = m.group("note")
        if note and note.strip():
            notes[key] = note.strip()
    return answers, notes
