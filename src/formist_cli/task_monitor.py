"""``fm task monitor`` / ``wait`` — a port of noddit's ``cli/commands/
{monitor,wait}.ts``, scoped to the pieces documented in the C1138 research
spec as worth porting for an agent-facing CLI (see the module-level gaps
list at the bottom).

Every "what to do about it" suggested command below says ``fm task``, not
``nod`` — the agent reading this output runs `fm`, not `nod`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from formist_cli.task_format import NOTE_KIND
from formist_cli.task_identity import agent_name_of
from formist_cli.task_model import Node
from formist_cli.task_ref import format_ref
from formist_cli.task_status import is_terminal

#: English approximations of the Chinese note-kind words, for monitor's
#: own operator-facing "owner {{kind}}" line — display text, not file
#: format (the file format stays the Chinese words in task_format.py).
_KIND_EN = {
    "追问": "follow-up", "回复": "reply", "补充": "addendum", "已看过": "seen",
    "等待": "waiting", "延迟": "deferred", "解除延迟": "undeferred",
    "完成": "done", "被取代": "superseded", "新问题": "a new question",
    "原话": "the original words", "接活": "claimed", "心跳": "heartbeat",
    "已收到": "received", "已提醒": "reminded", "部分已答": "a partial answer",
    "已执行": "ran", "整理": "condensed", "已发送": "sent",
}


def same_agent(a: str | None, b: str | None) -> bool:
    """Name-before-`·` match, case/whitespace-normalized — `ownedBy`'s
    rule (C1138 research spec §2), NOT `owner_agent`."""
    na, nb = agent_name_of(a), agent_name_of(b)
    if not na or not nb:
        return False
    return na.strip().lower() == nb.strip().lower()


def _owned_by_ancestor(node: Node, by_id: dict[int, Node], author: str) -> bool:
    """Whether some ancestor of ``node`` (walking ``parent`` up to 50
    levels) is owned by ``author`` — the "filed anywhere under a node I
    own" half of :func:`has_open_work_for`."""
    depth = 0
    cur = node
    seen: set[int] = set()
    while cur is not None and cur.parent is not None and depth < 50:
        if cur.parent in seen:
            break
        seen.add(cur.parent)
        parent = by_id.get(cur.parent)
        if parent is None:
            break
        if same_agent(parent.author, author):
            return True
        cur = parent
        depth += 1
    return False


def has_open_work_for(nodes: list[Node], author: str) -> bool:
    """``hasOpenWorkFor`` (C1138 research spec §3): false only when NONE
    of the three hold — owns an open node, has something queued, or an
    unclaimed brief sits under a node it owns."""
    from formist_cli.task_intake import is_unclaimed_brief

    by_id = {n.id: n for n in nodes}
    for n in nodes:
        if same_agent(n.author, author) and (not is_terminal(n.state) or n.queued):
            return True
        if is_unclaimed_brief(n) and _owned_by_ancestor(n, by_id, author):
            return True
    return False


def _language() -> str:
    """Use the same public Noddit language setting as the installed CLI."""
    if value := os.environ.get("NODDIT_LANG"):
        return value
    home = os.environ.get("NODDIT_HOME") or os.path.join(os.path.expanduser("~"), ".decs")
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("lang") or "en")
    except (OSError, ValueError):
        return "en"


def _next_step(ref: str, kind: str, lang: str) -> str:
    if lang == "zh-Hans":
        if kind == NOTE_KIND["deferred"]:
            return f"→ 先停下 {ref}，等它回来"
        if kind == NOTE_KIND["undeferred"]:
            return f"→ 从停下的地方接着做 {ref}"
        return f'→ fm task read {ref} --ack "<你打算怎么做>"'
    if kind == NOTE_KIND["deferred"]:
        return f"→ stop work on {ref} until it comes back"
    if kind == NOTE_KIND["undeferred"]:
        return f"→ pick {ref} back up where you left off"
    return f'→ fm task read {ref} --ack "<what you\'ll do>"'


def format_note_event(node: Node, note, is_reopen: bool, *, lang: str | None = None) -> str:
    """``{{ref}} {{what}}: {{title}}{{ —— text}} {{next step}}  since={{at}}``
    (C1138 research spec §1, row 1)."""
    ref = format_ref(node.id, node.letter)
    lang = lang or _language()
    answer = node.answer
    same_moment = False
    if lang == "zh-Hans":
        if note.kind == NOTE_KIND["answered"]:
            same_moment = answer is not None and answer.at[:19] == note.at[:19]
            choice = answer.choice if answer is not None and same_moment else None
            what = f"负责人答复 {choice}" if choice else "负责人答复"
        elif is_reopen:
            what = "负责人回复，节点从归档拉回"
        else:
            what = f"负责人 {note.kind}"
        # Noddit flattens the full answer activity, including the chosen
        # option label and the owner's free-text note, onto the monitor line.
        text = " ".join(note.text.split())[:80]
        body = f"{ref} {what}：{node.title}"
        if text:
            body += f" —— {text}"
        return f"{body} {_next_step(ref, note.kind, lang)}  since={note.at}"
    if note.kind == NOTE_KIND["answered"]:
        # `note.at` only carries second precision (the heading text is
        # truncated there); `node.answer.at` keeps milliseconds. Compare
        # at second granularity, or this would almost never match.
        same_moment = answer is not None and answer.at[:19] == note.at[:19]
        choice = answer.choice if answer is not None and same_moment else None
        what = f"owner answered {choice}" if choice else "owner answered"
    elif is_reopen:
        what = "owner replied, node pulled back from archive"
    else:
        what = f"owner {_KIND_EN.get(note.kind, note.kind)}"
    if note.kind == NOTE_KIND["answered"] and same_moment and answer is not None and answer.note:
        text = answer.note
    else:
        text = note.summary or note.text.split("\n", 1)[0].strip()
    text = text[:80]
    body = f"{ref} {what}: {node.title}"
    if text:
        body += f" —— {text}"
    body += f" {_next_step(ref, note.kind, lang)}  since={note.at}"
    return body


def _parse_epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def collect_node_events(
    nodes: list[Node], since_t: float, author: str | None, *, lang: str | None = None
) -> list[tuple[float, str, str]]:
    """Every human-note / brief event at or after ``since_t`` an agent
    should act on, as ``(at_epoch, ref, line)`` tuples the caller sorts.
    Mirrors monitor.ts's "an agent's own note doesn't count" — agent-role
    notes never appear here. A brief filed since the cursor gets its own
    DECS-BRIEF line.

    A note heading only carries second precision (``noteHeading`` slices
    off the rest — see ``task_serialize``), while ``since`` is a
    millisecond/microsecond cursor saved between passes, so a strict
    ``>=`` can drop an event whose real moment was a few ms into the
    cursor's own second. `nod`'s own ``changes`` help text names this
    exact trade and picks a side: "better duplicated than missed." One
    second of tolerance does the same here.
    """
    from formist_cli.task_intake import brief_state, original_words

    since_t -= 1.0
    lang = lang or _language()
    out: list[tuple[float, str, str]] = []
    for n in nodes:
        if author and not same_agent(n.author, author):
            continue
        ref = format_ref(n.id, n.letter)
        if brief_state(n) == "unclaimed":
            at_t = _parse_epoch(n.created) or 0.0
            if at_t >= since_t:
                words = original_words(n) or n.title
                extra = f" —— {words}" if words != n.title else ""
                if lang == "zh-Hans":
                    out.append((at_t, ref, (
                        f"DECS-BRIEF {ref} 负责人新派了：{n.title}{extra} —— "
                        f"fm task claim {ref} 接走，展开、拆分后用 fm task ask 请负责人确认  "
                        f"since={n.created}"
                    )))
                else:
                    out.append((at_t, ref, (
                        f"DECS-BRIEF {ref} the owner filed a brief: {n.title}{extra} — "
                        f"fm task claim {ref}, expand and split it, then fm task ask to have the owner confirm  "
                        f"since={n.created}"
                    )))
        for note in n.notes:
            if note.role != "human":
                continue
            at_t = _parse_epoch(note.at)
            if at_t is None or at_t < since_t:
                continue
            is_reopen = (not n.archived) and n.reopened_at == note.at
            out.append((at_t, ref, format_note_event(n, note, is_reopen, lang=lang)))
    return out


def collect_clock_events(
    fired_undeferred: list[Node],
    fired_reminded: list[Node],
    fired_checkback: list[Node],
    *,
    lang: str | None = None,
) -> list[str]:
    lang = lang or _language()
    lines = []
    for n in fired_undeferred:
        ref = format_ref(n.id, n.letter)
        if lang == "zh-Hans":
            lines.append(f"NODDIT-UNDEFERRED {ref} 延迟到期，回来了：{n.title} → 从停下的地方接着做")
        else:
            lines.append(f"NODDIT-UNDEFERRED {ref} is back from deferral: {n.title} → pick it back up where you left off")
    for n in fired_reminded:
        ref = format_ref(n.id, n.letter)
        if lang == "zh-Hans":
            lines.append(f"NODDIT-REMIND {ref}：到点了，告诉负责人——{n.title} → 告诉负责人，然后 fm task done {ref}")
        else:
            lines.append(f"NODDIT-REMIND {ref}: time to tell the owner — {n.title} → tell the owner, then fm task done {ref}")
    for n in fired_checkback:
        ref = format_ref(n.id, n.letter)
        if lang == "zh-Hans":
            lines.append(
                f"NODDIT-CHECK-BACK {ref}：到日子了，去问问 {n.waiting_on}——{n.title} → "
                f"还在等：fm task hold {ref} --until <日期>；等到了：fm task start {ref}"
            )
        else:
            lines.append(
                f"NODDIT-CHECK-BACK {ref}: time to check on {n.waiting_on} — {n.title} → "
                f"still waiting: fm task defer {ref} --on \"{n.waiting_on}\" --until <date>; "
                f"cleared: fm task start {ref}"
            )
    return lines


#: How stale a saved cursor may be before a resume is treated as a fresh
#: start instead (C1138 research spec §6: nod's own 24h).
_CURSOR_MAX_AGE_SECONDS = 86_400


def _cursor_path(layout, author: str | None) -> str:
    """Keyed by (author, this project) — deliberately NOT by OS pid.

    `nod`'s own cursor file is pid-keyed because it's a long-running
    daemon; `fm task monitor` is re-invoked as a short-lived process each
    poll (the Monitor-tool "re-arm the exact same command" pattern this
    whole feature is built around), so pid-keying would mean a cursor
    that can never be resumed between calls. Keying on (author, project)
    instead is the deliberate fork the C1138 research spec flagged as a
    real design decision, not an oversight.
    """
    safe = (author or "_all_").strip().lower().replace("/", "_").replace(" ", "_")
    return os.path.join(layout.root, ".monitor", f"{safe}.json")


def load_cursor(layout, author: str | None) -> str | None:
    path = _cursor_path(layout, author)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    cursor = data.get("cursor")
    at = _parse_epoch(cursor)
    if at is None or (datetime.now(timezone.utc).timestamp() - at) > _CURSOR_MAX_AGE_SECONDS:
        return None
    return cursor


def save_cursor(layout, author: str | None, cursor: str) -> None:
    path = _cursor_path(layout, author)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"cursor": cursor}, fh)
    os.replace(tmp, path)


def run_pass(store, author: str | None, since: str) -> tuple[list[str], str]:
    """One monitor pass: run the three clock sweeps, then report every
    event since ``since``. Returns ``(lines, next_since)``. Each clock
    sweep is wrapped so one bad node never kills the whole pass
    (DECS-ERROR, C1138 research spec §1)."""
    lines: list[str] = []
    fired_undeferred: list[Node] = []
    fired_reminded: list[Node] = []
    fired_checkback: list[Node] = []
    for label, fn, sink in (
        ("returnDueDeferrals", store.return_due_deferrals, fired_undeferred),
        ("fireDueReminders", store.fire_due_reminders, fired_reminded),
        ("remindDueWaits", store.remind_due_waits, fired_checkback),
    ):
        try:
            sink.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one bad sweep must not kill the pass
            lines.append(f"DECS-ERROR {label}: {exc}")

    next_since = datetime.now(timezone.utc).isoformat()
    try:
        nodes = store.all_nodes()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"DECS-ERROR reading nodes: {exc}")
        nodes = []

    since_t = _parse_epoch(since) or 0.0
    lang = _language()
    events = collect_node_events(nodes, since_t, author, lang=lang)
    events.sort(key=lambda e: e[0])
    lines.extend(line for _, _, line in events)
    lines.extend(collect_clock_events(fired_undeferred, fired_reminded, fired_checkback, lang=lang))

    if author and not has_open_work_for(nodes, author):
        lines.append("DECS-DONE nothing of yours is open any more: stop, don't re-arm.")

    return lines, next_since


#: What this module does NOT port (C1138 research spec, flagged risks
#: §1-8), tracked as open gaps rather than guessed at:
#: - the nag-coalescing buffer and its four buffered kinds (NODDIT-TIDY,
#:   NODDIT-STALE, NODDIT-CHECKIN, DECS-UNCLAIMED) — these need
#:   checkins.json tracking this pass doesn't add either;
#: - NODDIT-TELL (separate main-session store, C1139's scope);
#: - source-item events (`nod source`, not ported);
#: - the teammate/`--agent-name` self-detection and its DECS-WARN (no
#:   Python-side "teammate" concept to detect);
#: - NODDIT-DAYDREAM (depends on `core/daydream.ts`, not ported).
