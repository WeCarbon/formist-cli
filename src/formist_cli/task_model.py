"""The node data model: a Python port of noddit's ``Decision`` (``core/types.ts``).

``fm task`` in local mode reads and writes the exact same ``decisions/*.md``
files `nod` does (C1058, the noddit-tasks contract §6.3), so this module's
shape mirrors ``core/types.ts`` field for field rather than inventing a
Formist-shaped equivalent. Where a comment here is terse, the fuller
rationale lives in the TS source it mirrors (``~/Code/decisions-md/src/core/types.ts``).

Kept dependency-free (dataclasses only): this tool must stay stdlib-only
(``formist_cli.cli``'s module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Option:
    """One selectable option inside a decision: ``### <key> · <label>``."""

    key: str
    label: str
    body: str = ""


@dataclass
class Question:
    """A sub-question (``## 子问题``): one node, several small questions."""

    key: str
    label: str
    body: str = ""
    options: list[Option] = field(default_factory=list)
    default: str | None = None


@dataclass
class Step:
    """One entry in ``nod steps``' step list."""

    label: str
    done: bool = False


@dataclass
class Attachment:
    """A markdown link under ``## 附件``."""

    label: str
    target: str
    image: bool = False
    group: str | None = None
    caption: str | None = None


@dataclass
class NoteSent:
    """``## 已发送``'s typed data (an outbound send tool's archive note)."""

    channel: str = ""
    to_name: str | None = None
    to_id: str | None = None
    at: str | None = None
    message_id: str | None = None
    legacy: bool = False


@dataclass
class Note:
    """One appended ``## <kind>（role · author · time）`` section."""

    kind: str
    role: str  # "human" | "agent"
    author: str
    at: str
    text: str
    heading: str
    writer: str | None = None
    summary: str | None = None
    revises: bool = False
    relayed: bool = False
    sent: NoteSent | None = None
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Answer:
    choice: str | None = None
    answers: dict[str, str] | None = None
    answer_notes: dict[str, str] | None = None
    note: str | None = None
    by: str = ""
    at: str = ""


@dataclass
class PartialAnswer:
    round: int
    answers: dict[str, str]
    answer_notes: dict[str, str] | None = None
    by: str = ""
    at: str = ""


@dataclass
class PreviousRound:
    round: int
    options: list[Option] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    answer: str | None = None
    sub_answers: dict[str, str] | None = None
    by: str | None = None
    at: str | None = None


@dataclass
class AttachmentApproval:
    target: str
    hash: str
    approved_hash: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    approve_on: list[str] | None = None
    declined: str | None = None


@dataclass
class Checkin:
    asked: int
    last_asked_at: str
    unanswered: bool = False


# ``state``: the real status; see ``task_status.ITEM_STATUSES``.
ITEM_STATUSES = (
    "not_started", "in_progress", "waiting", "needs_decision", "decided", "completed", "closed",
)


@dataclass
class Node:
    """A noddit node: one piece of work, task or decision, one id.

    Mirrors ``core/types.ts``'s ``Decision``. Fields are ``None``/empty
    rather than omitted so every attribute always exists; ``task_serialize``
    decides what is worth writing back to front matter, the same
    "only what differs from the default" rule ``serialize.ts`` uses.
    """

    id: int
    title: str = ""
    author: str = ""
    state: str = "not_started"
    kind: str = "task"  # decision | acceptance | fyi | task (derived on parse)

    close_reason: str | None = None
    waiting_on: str | None = None
    waiting_until: str | None = None
    deferred_at: str | None = None
    deferred_by: str | None = None
    deferred_until: str | None = None
    assignee: str | None = None  # "human" or None (AI)

    created: str = ""
    updated_at: str | None = None
    active_at: str | None = None  # derived, never stored

    default: str | None = None
    order: int | None = None
    urgency: str = "normal"  # high | normal | low
    due: str | None = None
    milestone: bool = False
    remind_at: str | None = None
    if_no_answer: str | None = None
    reversible: bool | None = None
    session: str | None = None

    progress: int | None = None
    progress_msg: str | None = None
    after: list[str] = field(default_factory=list)
    est: str | None = None
    every: str | None = None
    when: str | None = None
    stale_after: str | None = None
    steps: list[Step] | None = None
    owner_agent: str | None = None

    intake: bool = False
    claimed_by: str | None = None
    claimed_at: str | None = None

    parent: int | None = None
    children: list[int] = field(default_factory=list)

    checkin: Checkin | None = None
    extra: dict = field(default_factory=dict)

    superseded_by: int | None = None
    supersedes: list[int] = field(default_factory=list)

    answer: Answer | None = None
    partial_answer: PartialAnswer | None = None

    archived_at: str | None = None
    reopened_at: str | None = None

    tags: list[str] = field(default_factory=list)

    body: str = ""
    options: list[Option] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    previous_rounds: list[PreviousRound] = field(default_factory=list)
    round_withdrawn: bool = False

    attachments: list[Attachment] = field(default_factory=list)
    body_attachments: list[Attachment] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    approvals: list[AttachmentApproval] = field(default_factory=list)

    run: dict | None = None

    file: str = ""
    slug: str = ""
    archived: bool = False
    project: str = ""
    project_name: str | None = None
    letter: str | None = None

    queued: bool = False
