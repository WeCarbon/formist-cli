"""The local-mode engine: every ``fm task <verb>`` operation against a
``decisions/`` directory, byte-compatible with `nod` (contract §6.3).

A direct port of noddit's ``core/store.ts`` (``Store``), scoped to ONE
project directory (``fm`` in local mode never searches several projects
at once the way noddit's multi-project bench does) and to the verbs
C1052 prioritizes. Every method below names which ``Store`` method and
``nod`` CLI verb it mirrors; see
``~/Code/decisions-md`` and the C1052 research spec
(``noddit-store-spec.md``) for exact semantics where a comment here is
terse.

Deliberately NOT ported (documented gaps, tracked as child nodes under
C1052): ``run``/``run-spec`` (contract says not ported), ``tell`` (writes
to the separate main-session store, not a per-project one), the
check-in nag bookkeeping (``checkins.json``), the three "clock" sweeps
(due reminders/waits/deferrals firing on their own), ``moveAttachment``,
the full view-filter DSL (only the 6 built-in predicates), and
``daydream``/``compact``/``restore``/``tidy``/``bench``/``migrate``.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime

from formist_cli import task_parse, task_serialize
from formist_cli.task_assignee import is_human_task
from formist_cli.task_format import NOTE_KIND, OPTION_LABEL
from formist_cli.task_identity import current_author
from formist_cli.task_intake import brief_state, with_description
from formist_cli.task_lock import with_file_lock, write_file_atomic
from formist_cli.task_model import (
    Answer,
    AttachmentApproval,
    Node,
    Option,
    PartialAnswer,
    Step,
)
from formist_cli.task_paths import (
    EntryFile,
    Layout,
    child_dir_of,
    entry_file_name,
    list_entry_files,
    release_id,
    reserve_id,
    to_posix_path,
)
from formist_cli.task_questions import (
    OTHER_KEY,
    check_questions,
    parse_answer_tokens,
    question_options,
    render_answer_lines,
    render_questions_block,
)
from formist_cli.task_queue import dequeue, enqueue, read_queue
from formist_cli.task_ref import RefError, format_ref, parse_ref
from formist_cli.task_status import awaits_decision, is_startable, is_terminal, with_open_question
from formist_cli.task_title import WAIT_ON_MAX_COLS, assert_title_fits
from formist_cli.task_width import string_width


class StoreError(ValueError):
    """A refusal the CLI layer should print as an error, not a traceback."""


class NotFoundError(StoreError):
    pass


class ClaimedError(StoreError):
    def __init__(self, message: str, node: Node):
        super().__init__(message)
        self.node = node


def now_stamp(ms: bool = False) -> str:
    now = datetime.now()
    offset_seconds = -time.timezone if time.localtime().tm_isdst == 0 else -time.altzone
    sign = "+" if offset_seconds >= 0 else "-"
    off = abs(offset_seconds)
    oh, om = divmod(off // 60, 60)
    base = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] if ms else now.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}{sign}{oh:02d}:{om:02d}"


def role_of(by: str) -> str:
    return "human" if by == "owner" else "agent"


def _default_author() -> str:
    return current_author() or "fm"


class LocalStore:
    def __init__(self, layout: Layout):
        self.layout = layout

    # -- reading ----------------------------------------------------------

    def _project_config(self) -> dict:
        from formist_cli import task_project

        return task_project.read_project_config(self.layout)

    def letter(self) -> str | None:
        return self._project_config().get("letter")

    def project_key(self) -> str:
        return os.path.basename(self.layout.project_root)

    def _read_file(self, entry: EntryFile, queued_ids: set[int]) -> Node:
        with open(entry.path, encoding="utf-8") as fh:
            text = fh.read()
        node = task_parse.parse_decision(
            text, entry.path, entry.archived, project=self.project_key(), queued=entry.id in queued_ids,
        )
        node.letter = self.letter()
        return node

    def all_nodes(self) -> list[Node]:
        entries = list_entry_files(self.layout)
        queued_ids = set(read_queue(self.layout))
        nodes = [self._read_file(e, queued_ids) for e in entries]
        parent_by_id = {e.id: e.parent for e in entries}
        for n in nodes:
            n.parent = parent_by_id.get(n.id)
        children: dict[int, list[int]] = {}
        for n in nodes:
            if n.parent is not None:
                children.setdefault(n.parent, []).append(n.id)
        for n in nodes:
            n.children = children.get(n.id, [])
        return nodes

    def resolve(self, ref: str) -> Node:
        try:
            r = parse_ref(ref)
        except RefError as e:
            raise StoreError(str(e)) from e
        nodes = self.all_nodes()
        matches = [n for n in nodes if n.id == r.id]
        if not matches:
            raise NotFoundError(f"{format_ref(r.id, self.letter())} does not exist")
        return matches[0]

    def get(self, ref_or_id) -> Node:
        if isinstance(ref_or_id, Node):
            return ref_or_id
        return self.resolve(str(ref_or_id))

    # -- the one write choke point -----------------------------------------

    def _entry_for(self, id_: int) -> EntryFile | None:
        for e in list_entry_files(self.layout):
            if e.id == id_:
                return e
        return None

    def _lock_path(self, id_: int) -> str:
        return os.path.join(self.layout.root, ".locks", f"{id_}.lock")

    def write(self, node: Node, move: str | None = None) -> Node:
        node.state = with_open_question(node.state, node.options, node.answer, node.round_withdrawn)
        node.updated_at = now_stamp(ms=True)
        write_file_atomic(node.file, task_serialize.serialize_node(node))

        if move in ("archive", "unarchive") and node.parent is None:
            # Only a root node's own file/folder physically relocates — a
            # nested node's folder already lives wherever its ancestor's
            # does (C1052 research spec §5, point 5).
            new_dir = self.layout.archived_dir if move == "archive" else self.layout.active_dir
            new_file = os.path.join(new_dir, os.path.basename(node.file))
            if os.path.abspath(new_file) != os.path.abspath(node.file):
                os.makedirs(new_dir, exist_ok=True)
                os.rename(node.file, new_file)
                old_child_dir = child_dir_of(node.file)
                if os.path.isdir(old_child_dir):
                    os.rename(old_child_dir, child_dir_of(new_file))
                node.file = new_file
                node.archived = move == "archive"

        with open(node.file, encoding="utf-8") as fh:
            reread_text = fh.read()
        reread = task_parse.parse_decision(
            reread_text, node.file, node.archived, project=node.project, queued=node.queued,
        )
        reread.letter = node.letter
        reread.parent = node.parent
        reread.children = node.children
        return reread

    def mutate(self, id_: int, change) -> Node:
        """``change(fresh_node) -> (next_node, move)``; ``move`` is one of
        ``None``/``"archive"``/``"unarchive"``.

        ``change`` returning ``(None, ...)`` means "no-op, nothing to
        write" — ``mutate`` then returns the fresh-read node untouched,
        matching every ``Store`` method that early-returns the unchanged
        node rather than re-stamping ``updated_at`` for nothing.
        """
        lock = self._lock_path(id_)

        def run():
            entry = self._entry_for(id_)
            if not entry:
                raise NotFoundError(f"node {id_} does not exist")
            fresh = self._read_file(entry, set(read_queue(self.layout)))
            parent_by_id = {e.id: e.parent for e in list_entry_files(self.layout) if e.id == id_}
            fresh.parent = parent_by_id.get(id_)
            next_node, move = change(fresh)
            if next_node is None:
                return fresh
            return self.write(next_node, move)

        return with_file_lock(lock, run)

    # -- the append-a-section primitive -------------------------------------

    def _append(self, node: Node, heading: str, text: str, by: str, role: str | None = None,
                summary: str | None = None, relayed: bool = False, sent=None) -> tuple[Node, bool, bool]:
        """Returns ``(node_with_body_updated, human, was_archived)``; caller
        decides the resulting ``state``/``move`` and whether to enqueue."""
        resolved_role = role or role_of(by)
        human = resolved_role == "human"
        was_archived = node.archived
        node.body = task_serialize.append_section(node.body, heading, text, summary, relayed, sent)
        if human and was_archived:
            node.reopened_at = now_stamp(ms=True)
        return node, human, was_archived

    def note(self, id_: int, text: str, author: str, role: str | None = None, kind: str | None = None,
              summary: str | None = None, relayed: bool = False) -> Node:
        """``nod note`` — ``Store.note``/``Store.append``."""
        deduped = False

        def change(fresh: Node):
            nonlocal deduped
            resolved_role = role or role_of(author)
            human = resolved_role == "human"
            # Dedup guard: an identical human note within 10 minutes is a no-op.
            if human:
                now = time.time()
                for n in reversed(fresh.notes):
                    if n.role != "human":
                        continue
                    if n.text.strip() != text.strip():
                        continue
                    at = _parse_epoch(n.at)
                    if at is not None and now - at < 600:
                        deduped = True
                        return None, None
                    break
            k = kind or ("回复" if human else "补充")
            heading = task_serialize.note_heading(k, resolved_role, author, now_stamp(ms=True))
            fresh2, human2, was_archived = self._append(fresh, heading, text, author, resolved_role, summary, relayed)
            fresh2.state = _owners_turn_after(fresh2, human2, relayed)
            move = "unarchive" if (human2 and was_archived) else None
            return fresh2, move

        written = self.mutate(id_, change)
        if deduped:
            return written
        if role_of(author) == "human" or role == "human":
            enqueue(self.layout, id_)
            written.queued = True
        return written

    # -- create / add -------------------------------------------------------

    def create(
        self, title: str, author: str, *, body: str = "", options: list[Option] | None = None,
        default: str | None = None, task: bool = False, fyi: bool = False, tags: list[str] | None = None,
        urgency: str = "normal", due: str | None = None, parent: str | None = None, after: list[str] | None = None,
        est: str | None = None, assignee: str | None = None, milestone: bool = False, intake: bool = False,
        remind_at: str | None = None, if_no_answer: str | None = None, reversible: bool | None = None,
    ) -> Node:
        """``nod add`` — ``Store.create``. A simplified but faithful port:
        body-parsed options and the full round-trip self-check are not
        implemented (gap, tracked under C1052); explicit ``-o``/``--task``/
        ``--fyi`` flags are the supported path, which covers the
        overwhelming majority of real usage."""
        author = author.strip()
        if not author:
            raise StoreError("who is writing this? pass an author.")
        title = title.strip()
        if not title:
            raise StoreError("a title is required")

        bare = task or fyi
        given = list(options or [])
        if bare and given:
            raise StoreError("a plain task/fyi can't also carry options")
        if not given and not bare:
            given = [Option(key="1", label=OPTION_LABEL["accept"])]
        if given:
            keys = [o.key for o in given]
            if any(not k.isdigit() or not (1 <= int(k) <= 9) for k in keys):
                raise StoreError(f"option keys must be single digits 1-9: {keys}")
            if len(set(keys)) != len(keys):
                raise StoreError("duplicate option key")
            if default and default not in keys:
                raise StoreError(f"default {default!r} is not one of the option keys")

        created = now_stamp(ms=True)
        state = "completed" if fyi else ("not_started" if task else "needs_decision")

        parent_node: Node | None = None
        if parent:
            parent_node = self.resolve(parent)
            if is_terminal(parent_node.state) and parent_node.archived:
                raise StoreError(f"{parent} is archived; file this under an active node instead")
            if fyi:
                raise StoreError("an fyi node is always a root")

        body_text = (body or "").strip()
        if intake:
            if not bare:
                raise StoreError("--brief is task-only")
            heading = task_serialize.note_heading(NOTE_KIND["original"], role_of(author), author, created)
            body_text = f"## {heading}\n\n{body_text or title}"

        options_block = task_serialize.render_options_block(given if not bare else [])
        full_body = "\n\n".join(x for x in [body_text, options_block.strip()] if x) + "\n"

        id_ = reserve_id(self.layout)
        try:
            slug = task_serialize.slugify(title)
            target_dir = (
                child_dir_of(parent_node.file) if parent_node is not None
                else (self.layout.archived_dir if is_terminal(state) else self.layout.active_dir)
            )
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, entry_file_name(id_, slug))

            node = Node(
                id=id_, title=title, author=author, state=state,
                created=created, updated_at=created,
                default=default, tags=tags or [], urgency=urgency, due=due,
                after=after or [], est=est, assignee=assignee, milestone=milestone,
                intake=intake, remind_at=remind_at, if_no_answer=if_no_answer, reversible=reversible,
                body=full_body, options=given if not bare else [],
                archived_at=created if is_terminal(state) else None,
                file=file_path, slug=slug, archived=is_terminal(state),
                project=self.project_key(), letter=self.letter(),
                parent=parent_node.id if parent_node else None,
                owner_agent=author,
            )
            from formist_cli.task_lock import create_file_atomic

            create_file_atomic(file_path, task_serialize.serialize_node(node))
        except Exception:
            release_id(self.layout, id_)
            raise
        with open(file_path, encoding="utf-8") as fh:
            text = fh.read()
        reread = task_parse.parse_decision(text, file_path, node.archived, project=node.project)
        reread.letter = node.letter
        reread.parent = node.parent
        return reread

    # -- lifecycle ------------------------------------------------------

    def start(self, id_: int, force: bool = False) -> Node:
        def change(fresh: Node):
            if awaits_decision(fresh.state, fresh.options, fresh.answer, fresh.round_withdrawn):
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} still has an unanswered question")
            if fresh.state == "needs_decision" and not force:
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} needs a decision first (--force to override)")
            if not force and fresh.after:
                items = self.all_nodes()
                if not is_startable(fresh, items):
                    raise StoreError(f"{format_ref(fresh.id, fresh.letter)} is still after unfinished nodes")
            fresh.state = "in_progress"
            fresh.waiting_on = None
            move = "unarchive" if fresh.archived else None
            return fresh, move

        return self.mutate(id_, change)

    def complete(self, id_: int, note_text: str | None = None, author: str | None = None) -> Node:
        """``nod done`` — ``Store.complete``. The dependents-cascade
        (unblocking a node whose only remaining ``after`` was this one) is
        not ported in this pass (gap)."""
        dequeue(self.layout, id_)

        def change(fresh: Node):
            if note_text and note_text.strip():
                heading = task_serialize.note_heading(NOTE_KIND["completed"], "agent", author or fresh.author, now_stamp(ms=True))
                fresh.body = task_serialize.append_section(fresh.body, heading, note_text)
            fresh.state = "completed"
            fresh.archived_at = now_stamp(ms=True)
            move = None if fresh.archived else "archive"
            return fresh, move

        result = self.mutate(id_, change)
        result.queued = False
        return result

    def archive(self, id_: int) -> Node:
        node = self.get(id_)
        if node.archived:
            raise StoreError(f"{format_ref(node.id, node.letter)} is already archived")
        return self.complete(id_)

    def unarchive(self, id_: int) -> Node:
        def change(fresh: Node):
            if not fresh.archived:
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} is not archived")
            fresh.state = "in_progress" if (fresh.answer or not fresh.options) else "needs_decision"
            fresh.close_reason = None
            return fresh, "unarchive"

        return self.mutate(id_, change)

    def close(self, id_: int, reason: str) -> Node:
        reason = (reason or "").strip()
        if not reason:
            raise StoreError("a close reason is required")
        dequeue(self.layout, id_)

        def change(fresh: Node):
            fresh.state = "closed"
            fresh.close_reason = reason
            fresh.archived_at = now_stamp(ms=True)
            move = None if fresh.archived else "archive"
            return fresh, move

        return self.mutate(id_, change)

    def dismiss(self, id_: int, by: str) -> Node:
        """``nod archive --seen`` — ``Store.dismiss``."""
        dequeue(self.layout, id_)

        def change(fresh: Node):
            at = now_stamp(ms=True)
            fresh.state = "completed"
            fresh.answer = Answer(note="看过了，不用处理", by=by, at=at)
            if not fresh.archived:
                fresh.archived_at = at
            heading = task_serialize.note_heading(NOTE_KIND["seen"], role_of(by), by, at)
            fresh.body = task_serialize.append_section(fresh.body, heading, "看过了，不用处理。")
            move = None if fresh.archived else "archive"
            return fresh, move

        return self.mutate(id_, change)

    def retitle(self, id_: int, title: str, by: str, role: str | None = None) -> Node:
        title = title.strip()
        if not title:
            raise StoreError("a title is required")
        assert_title_fits(title)

        def change(fresh: Node):
            if fresh.title == title:
                return None, None
            old = fresh.title
            fresh.title = title
            heading = task_serialize.note_heading(NOTE_KIND["addendum"], role or role_of(by), by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, f"改了标题，原来是「{old}」。")
            return fresh, None

        return self.mutate(id_, change)

    def move(self, id_: int, *, parent: str | None = None, root: bool = False) -> Node:
        """``nod move`` — reparent a node with its whole subtree. The id
        never changes, so every ref keeps resolving; the file and its
        child-node folder (if any) move together, and relative attachment
        links in the body (and approval records' targets) are rewritten to
        match the new directory depth."""
        if bool(parent) == bool(root):
            raise StoreError("move needs exactly one of --parent <ref> or --root")

        def change(fresh: Node):
            old_file = fresh.file
            old_dir = os.path.dirname(old_file)

            new_parent_id: int | None
            if root:
                new_parent_id = None
                target_dir = self.layout.archived_dir if fresh.archived else self.layout.active_dir
            else:
                parent_node = self.resolve(parent)
                if parent_node.id == fresh.id:
                    raise StoreError("a node cannot be its own parent")
                if self._is_descendant(parent_node.id, fresh.id):
                    raise StoreError(
                        f"{format_ref(parent_node.id, parent_node.letter)} is already under "
                        f"{format_ref(fresh.id, fresh.letter)} — that move would create a cycle"
                    )
                new_parent_id = parent_node.id
                target_dir = child_dir_of(parent_node.file)

            if os.path.abspath(target_dir) == os.path.abspath(old_dir) and new_parent_id == fresh.parent:
                return None, None  # already there

            os.makedirs(target_dir, exist_ok=True)
            new_file = os.path.join(target_dir, os.path.basename(old_file))
            old_child_dir = child_dir_of(old_file)
            if os.path.isdir(old_child_dir):
                os.rename(old_child_dir, child_dir_of(new_file))
            if os.path.isfile(old_file):
                os.remove(old_file)  # write() below creates new_file fresh

            fresh.body = task_serialize.relink_body(fresh.body, old_dir, target_dir)
            for ap in fresh.approvals:
                ap.target = task_serialize.relink_target(ap.target, old_dir, target_dir)
            fresh.file = new_file
            fresh.parent = new_parent_id
            # Re-derive, don't carry over: a reparent can cross from an
            # active-rooted subtree into an archived one (or the reverse),
            # and nested nodes don't rename on their own — see `write`'s
            # "only a root node's file/folder actually moves" comment.
            fresh.archived = self._dir_is_archived(target_dir)
            return fresh, None

        return self.mutate(id_, change)

    def _dir_is_archived(self, path: str) -> bool:
        archived = os.path.realpath(self.layout.archived_dir)
        real = os.path.realpath(path)
        return real == archived or real.startswith(archived + os.sep)

    def _is_descendant(self, candidate_id: int, ancestor_id: int) -> bool:
        """Whether ``candidate_id`` sits anywhere under ``ancestor_id`` in
        the tree — the cycle check `move` needs before reparenting."""
        nodes = {n.id: n for n in self.all_nodes()}
        seen: set[int] = set()
        cur = nodes.get(candidate_id)
        while cur is not None and cur.parent is not None:
            if cur.parent in seen:
                break  # a pre-existing cycle elsewhere; don't loop forever
            seen.add(cur.parent)
            if cur.parent == ancestor_id:
                return True
            cur = nodes.get(cur.parent)
        return False

    def supersede(self, old_id: int, new_id: int, author: str) -> tuple[Node, Node]:
        if old_id == new_id:
            raise StoreError("a node cannot supersede itself")
        new_node = self.mutate(new_id, lambda fresh: (
            replace(fresh, supersedes=sorted(set(fresh.supersedes) | {old_id})), None
        ))
        dequeue(self.layout, old_id)

        def change(fresh: Node):
            fresh.state = "closed"
            fresh.close_reason = "superseded"
            fresh.superseded_by = new_id
            fresh.archived_at = now_stamp(ms=True)
            heading = task_serialize.note_heading(NOTE_KIND["superseded"], role_of(author), author, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(
                fresh.body, heading, f"被 {format_ref(new_id, new_node.letter)} 取代：{new_node.title}",
            )
            move = None if fresh.archived else "archive"
            return fresh, move

        old_node = self.mutate(old_id, change)
        return old_node, new_node

    def assign(self, id_: int, assignee: str | None) -> Node:
        def change(fresh: Node):
            if fresh.assignee == assignee and not fresh.milestone:
                return None, None
            fresh.assignee = assignee
            fresh.milestone = False
            return fresh, None

        return self.mutate(id_, change)

    def set_milestone(self, id_: int, on: bool) -> Node:
        def change(fresh: Node):
            if fresh.milestone == on:
                return None, None
            if on:
                fresh.assignee = None
            fresh.milestone = on
            return fresh, None

        return self.mutate(id_, change)

    def flag(self, id_: int, important: bool | None = None, milestone: bool | None = None,
              due: str | None = None) -> Node:
        def change(fresh: Node):
            if milestone and not (due or fresh.due):
                raise StoreError("a milestone needs a due date")
            if important is True:
                fresh.urgency = "high"
            elif important is False:
                fresh.urgency = "normal"
            if milestone is True:
                fresh.milestone = True
                fresh.assignee = None
            elif milestone is False:
                fresh.milestone = False
            if due is not None:
                fresh.due = due
            return fresh, None

        return self.mutate(id_, change)

    def set_after(self, id_: int, after: list[str]) -> Node:
        def change(fresh: Node):
            fresh.after = list(after) if after else []
            return fresh, None

        return self.mutate(id_, change)

    def set_fields(self, id_: int, every=..., due=..., est=..., remind_at=...) -> Node:
        def change(fresh: Node):
            if every is not ...:
                fresh.every = every
            if due is not ...:
                fresh.due = due
                if due is None:
                    fresh.milestone = False
            if est is not ...:
                fresh.est = est
            if remind_at is not ...:
                fresh.remind_at = remind_at
            return fresh, None

        return self.mutate(id_, change)

    def set_tags(self, id_: int, add: list[str] | None = None, remove: list[str] | None = None) -> Node:
        add = [t.strip() for t in (add or []) if t.strip()]
        remove = {t.strip() for t in (remove or []) if t.strip()}

        def change(fresh: Node):
            kept = [t for t in fresh.tags if t not in remove]
            new_tags = kept + [t for t in dict.fromkeys(add) if t not in kept and t not in remove]
            if new_tags == fresh.tags:
                return None, None
            fresh.tags = new_tags
            return fresh, None

        return self.mutate(id_, change)

    def arrange(self, ids: list[int]) -> list[Node]:
        """``nod order`` — ``Store.arrange``: put these nodes in this order
        by hand, the first highest. ``order = i - len(ids)`` so the first
        gets the most negative number and the last gets ``-1``, always
        sorting above anything with no manual order (default 0). Only
        nodes whose order actually changes get written."""
        n = len(ids)
        results = []
        for i, id_ in enumerate(ids):
            target = i - n

            def change(fresh: Node, target=target):
                if fresh.order == target:
                    return None, None
                fresh.order = target
                return fresh, None

            results.append(self.mutate(id_, change))
        return results

    def unarrange(self, id_: int) -> Node:
        """``nod order --clear`` — ``Store.unarrange``."""
        def change(fresh: Node):
            if fresh.order is None:
                return None, None
            fresh.order = None
            return fresh, None

        return self.mutate(id_, change)

    # -- defer / wait -----------------------------------------------------

    def defer(self, id_: int, by: str, role: str | None = None, until: str | None = None,
               owner_said: str | None = None) -> Node:
        """``nod defer`` (passive delay). An agent may only put work off on
        the owner's own word — pass ``--owner-said`` with what they said,
        or run it as ``--human`` (the owner typing it themselves)."""
        resolved_role = role or role_of(by)
        if resolved_role != "human" and not (owner_said and owner_said.strip()):
            raise StoreError(
                "an agent may only defer on the owner's word — pass --owner-said "
                '"<what they said>", or --human if the owner is doing this themselves'
            )

        def change(fresh: Node):
            if is_terminal(fresh.state):
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} is already finished")
            if fresh.deferred_at and fresh.deferred_until == until and fresh.state != "waiting":
                return None, None
            move = None
            if fresh.state == "waiting":
                fresh.state = "not_started"
                fresh.waiting_on = None
                fresh.waiting_until = None
            fresh.deferred_at = now_stamp(ms=True)
            fresh.deferred_by = by
            fresh.deferred_until = until
            day_line = f"后面再做，{until} 再拿出来。到那天之前先别动它。" if until else "后面再做。解除延迟之前先别动它。"
            said_line = f"负责人原话：「{owner_said.strip()}」" if owner_said and owner_said.strip() else ""
            text = "\n\n".join(x for x in [day_line, said_line] if x)
            heading = task_serialize.note_heading(NOTE_KIND["deferred"], role or role_of(by), by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, text)
            return fresh, move

        return self.mutate(id_, change)

    def undefer(self, id_: int, by: str, role: str | None = None) -> Node:
        def change(fresh: Node):
            if not fresh.deferred_at:
                return None, None
            fresh.deferred_at = None
            fresh.deferred_by = None
            fresh.deferred_until = None
            heading = task_serialize.note_heading(NOTE_KIND["undeferred"], role or role_of(by), by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, "可以接着做了。")
            return fresh, None

        return self.mutate(id_, change)

    # -- the three "clock" sweeps (C1138 research spec §5.33 / §5) ---------
    #
    # Each one fires a passive event with no human action behind it: a
    # deferral's day came, a reminder's time came, a wait's check-back day
    # came. All three are signed author=DEFERRAL_CLOCK — literally the
    # string "nod", not this process's own identity — because the
    # "already fired" guard each one reads back is `author === 'nod'`
    # (store.ts's own constant). Signing as "fm" instead would mean a real
    # `nod monitor` on the same files never recognizes fm's firing as done
    # and fires it again (and the reverse); byte-compatibility with `nod`
    # requires sharing its clock-author constant, not inventing a second one.

    DEFERRAL_CLOCK_AUTHOR = "nod"

    def return_due_deferrals(self) -> list[Node]:
        """``returnDueDeferrals``: a passive ``--until`` deferral whose day
        has come lifts on its own, on any non-terminal node."""
        from formist_cli.task_status import deferral_over

        fired: list[Node] = []
        now = time.time()
        for n in self.all_nodes():
            if is_terminal(n.state) or not deferral_over(n.deferred_at, n.deferred_until, now):
                continue

            def change(fresh: Node):
                if not deferral_over(fresh.deferred_at, fresh.deferred_until, time.time()):
                    return None, None
                day = fresh.deferred_until
                fresh.deferred_at = None
                fresh.deferred_by = None
                fresh.deferred_until = None
                heading = task_serialize.note_heading(
                    NOTE_KIND["undeferred"], "agent", self.DEFERRAL_CLOCK_AUTHOR, now_stamp(ms=True),
                )
                fresh.body = task_serialize.append_section(fresh.body, heading, f"延迟到 {day}，到期了，可以接着做了。")
                return fresh, None

            fired.append(self.mutate(n.id, change))
        return fired

    def fire_due_reminders(self) -> list[Node]:
        """``fireDueReminders``: a ``remind_at`` that has passed. Guard:
        the last note signed by the clock of kind 已提醒 must be OLDER than
        the current ``remind_at`` — moving the reminder later re-fires it;
        moving it earlier or leaving it put keeps it fired."""
        fired: list[Node] = []
        now = time.time()
        for n in self.all_nodes():
            if is_terminal(n.state) or not n.remind_at:
                continue
            remind_t = _parse_epoch(n.remind_at)
            if remind_t is None or remind_t > now:
                continue
            last_reminded = next(
                (note for note in reversed(n.notes)
                 if note.author == self.DEFERRAL_CLOCK_AUTHOR and note.kind == NOTE_KIND["reminded"]),
                None,
            )
            if last_reminded and (_parse_epoch(last_reminded.at) or 0) >= remind_t:
                continue

            def change(fresh: Node, remind_t=remind_t):
                last = next(
                    (note for note in reversed(fresh.notes)
                     if note.author == self.DEFERRAL_CLOCK_AUTHOR and note.kind == NOTE_KIND["reminded"]),
                    None,
                )
                if last and (_parse_epoch(last.at) or 0) >= remind_t:
                    return None, None
                heading = task_serialize.note_heading(
                    NOTE_KIND["reminded"], "agent", self.DEFERRAL_CLOCK_AUTHOR, now_stamp(ms=True),
                )
                fresh.body = task_serialize.append_section(fresh.body, heading, f"到点了，告诉负责人：{fresh.title}")
                return fresh, None

            fired.append(self.mutate(n.id, change))
        return fired

    def remind_due_waits(self) -> list[Node]:
        """``remindDueWaits``: an active wait (``nod hold``) whose check-back
        day has come. Guard: the last clock-signed 补充 note must come
        BEFORE the most recent 等待 note — a fresh ``hold()`` call resets
        this by writing a new 等待 note."""
        from formist_cli.task_status import is_wait_due

        fired: list[Node] = []
        for n in self.all_nodes():
            if n.state != "waiting" or not is_wait_due(n):
                continue
            last_wait = max((i for i, note in enumerate(n.notes) if note.kind == NOTE_KIND["waiting"]), default=-1)
            last_clock = max(
                (i for i, note in enumerate(n.notes)
                 if note.author == self.DEFERRAL_CLOCK_AUTHOR and note.kind == NOTE_KIND["addendum"]),
                default=-1,
            )
            if last_clock > last_wait:
                continue

            def change(fresh: Node):
                if fresh.state != "waiting" or not is_wait_due(fresh):
                    return None, None
                lw = max((i for i, note in enumerate(fresh.notes) if note.kind == NOTE_KIND["waiting"]), default=-1)
                lc = max(
                    (i for i, note in enumerate(fresh.notes)
                     if note.author == self.DEFERRAL_CLOCK_AUTHOR and note.kind == NOTE_KIND["addendum"]),
                    default=-1,
                )
                if lc > lw:
                    return None, None
                heading = task_serialize.note_heading(
                    NOTE_KIND["addendum"], "agent", self.DEFERRAL_CLOCK_AUTHOR, now_stamp(ms=True),
                )
                fresh.body = task_serialize.append_section(
                    fresh.body, heading, f"到了回来看的日子：问问 {fresh.waiting_on}。",
                )
                return fresh, None

            fired.append(self.mutate(n.id, change))
        return fired

    def hold(self, id_: int, on: str | None, author: str, role: str | None = None,
              until: str | None = None) -> Node:
        def change(fresh: Node):
            who = (on or "").strip() or (fresh.waiting_on if fresh.state == "waiting" else None)
            if not who:
                raise StoreError("needs who/what it's waiting on (--on)")
            if string_width(who) > WAIT_ON_MAX_COLS:
                raise StoreError(f"--on is too long ({string_width(who)} columns, max {WAIT_ON_MAX_COLS})")
            if fresh.state == "needs_decision":
                raise StoreError("it's already the owner's own open question — use `ask`, not `defer --on`")
            same_day_only = fresh.state == "waiting" and who == fresh.waiting_on and not on and until
            fresh.state = "waiting"
            fresh.waiting_on = who
            if until is not None:
                fresh.waiting_until = until
            elif fresh.state != "waiting":
                fresh.waiting_until = None
            fresh.deferred_at = None
            fresh.deferred_by = None
            fresh.deferred_until = None
            if same_day_only:
                text = f"改到 {until} 回来看"
            elif until:
                text = f"等 {who}，{until} 回来看"
            else:
                text = f"等 {who}"
            heading = task_serialize.note_heading(NOTE_KIND["waiting"], role or role_of(author), author, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, text)
            return fresh, None

        return self.mutate(id_, change)

    def unhold(self, id_: int, author: str, role: str | None = None) -> Node:
        def change(fresh: Node):
            if fresh.state != "waiting":
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} is not waiting")
            fresh.state = "not_started"
            fresh.waiting_on = None
            fresh.waiting_until = None
            heading = task_serialize.note_heading(NOTE_KIND["addendum"], role or role_of(author), author, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, "不再等待")
            return fresh, None

        return self.mutate(id_, change)

    # -- claim / expand / briefs -------------------------------------------

    def claim(self, id_: int, by: str, force: bool = False) -> tuple[Node, bool]:
        """Returns ``(node, already)``: ``already`` is true for an
        idempotent self-claim (no write happened)."""
        already = False

        def change(fresh: Node):
            nonlocal already
            if is_terminal(fresh.state):
                raise StoreError(f"{format_ref(fresh.id, fresh.letter)} is already finished")
            if is_human_task(fresh.assignee) and not force:
                raise StoreError("that's the owner's own to-do; pass --force to claim it anyway")
            if fresh.claimed_by:
                mine = by.split(" · ")[0].strip().lower() == fresh.claimed_by.split(" · ")[0].strip().lower()
                if mine:
                    already = True
                    return None, None
                if not force:
                    raise ClaimedError(f"already claimed by {fresh.claimed_by}", fresh)
            fresh.claimed_by = by
            fresh.claimed_at = now_stamp(ms=True)
            fresh.owner_agent = by
            if fresh.state == "not_started":
                fresh.state = "in_progress"
            heading = task_serialize.note_heading(NOTE_KIND["claimed"], "agent", by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(
                fresh.body, heading, "接了。展开、拆成子节点后请负责人确认，确认前不开工。",
            )
            return fresh, None

        node = self.mutate(id_, change)
        return node, already

    def expand(self, id_: int, description: str, title: str | None = None) -> Node:
        def change(fresh: Node):
            original_heading = task_serialize.note_heading(NOTE_KIND["original"], role_of(fresh.author), fresh.author, fresh.created)
            fresh.body = with_description(fresh.body, description, original_heading)
            if title and title.strip():
                assert_title_fits(title.strip())
                fresh.title = title.strip()
            return fresh, None

        return self.mutate(id_, change)

    def briefs(self, include_claimed: bool = False) -> list[Node]:
        nodes = self.all_nodes()
        out = []
        for n in nodes:
            state = brief_state(n)
            if state == "unclaimed" or (include_claimed and state == "claimed"):
                out.append(n)
        return out

    # -- answering ----------------------------------------------------------

    def answer(self, id_: int, choice: str | None = None, note_text: str | None = None, by: str = "owner",
                role: str | None = None, answers: dict[str, str] | None = None, relayed: bool = False,
                partial: bool = False) -> Node:
        """``nod push``/``choose``/``answer`` — ``Store.answer``."""
        if not choice and not (note_text and note_text.strip()) and not answers:
            raise StoreError("needs a choice, a note, or sub-question answers")

        def change(fresh: Node):
            resolved_role = role or role_of(by)
            human = resolved_role == "human"
            at = now_stamp(ms=True)

            if fresh.questions:
                given = dict(answers or {})
                if not given and note_text:
                    given, extra_notes = parse_answer_tokens(note_text, fresh.questions)
                else:
                    extra_notes = {}
                if choice == "1":
                    merged = dict(given)
                else:
                    merged = dict(fresh.partial_answer.answers) if fresh.partial_answer else {}
                    merged.update(given)
                for k, v in merged.items():
                    q = next((x for x in fresh.questions if x.key == k), None)
                    if not q:
                        raise StoreError(f"no such sub-question: {k}")
                    if v != OTHER_KEY and not any(o.key == v for o in q.options):
                        raise StoreError(f"{k} has no option {v!r}")
                if partial and choice != "1" and any(q.key not in merged for q in fresh.questions):
                    answer_notes = {k: v for k, v in extra_notes.items() if k in merged}
                    text = "\n\n".join(x for x in [
                        render_answer_lines([q for q in fresh.questions if q.key in merged], merged, answer_notes),
                        (note_text or "").strip(),
                    ] if x)
                    fresh.partial_answer = PartialAnswer(
                        round=max(1, task_parse.options_round_of(fresh.body)), answers=merged,
                        answer_notes=answer_notes or None, by=by, at=at,
                    )
                    heading = task_serialize.note_heading(NOTE_KIND["partly_answered"], resolved_role, by, at)
                    fresh.body = task_serialize.append_section(fresh.body, heading, text)
                    return fresh, None
                for q in fresh.questions:
                    merged.setdefault(q.key, q.default or "")
                answer_notes = {k: v for k, v in extra_notes.items() if k in merged} or None
                final_choice = "1" if (all(merged.get(q.key) == q.default for q in fresh.questions) and not answer_notes) else "2"
                final_label = next((o.label for o in fresh.options if o.key == final_choice), "")
                headline = f"{final_choice} —— {final_label}" if final_label else final_choice
                text = "\n\n".join(x for x in [
                    headline, render_answer_lines(fresh.questions, merged, answer_notes), (note_text or "").strip(),
                ] if x)
                fresh.answer = Answer(choice=final_choice, answers=merged, answer_notes=answer_notes,
                                        note=(note_text or "").strip() or None, by=by, at=at)
                fresh.partial_answer = None
            else:
                if choice and fresh.options and not any(o.key == choice for o in fresh.options):
                    raise StoreError(f"no such option: {choice}")
                label = next((o.label for o in fresh.options if o.key == choice), "")
                headline = f"{choice} —— {label}" if choice else ""
                text = "\n\n".join(x for x in [headline, (note_text or "").strip()] if x)
                fresh.answer = Answer(choice=choice, note=(note_text or "").strip() or None, by=by, at=at)

            if human and fresh.approvals:
                answered_tokens = (
                    {f"{k}:{v}" for k, v in fresh.answer.answers.items()} | {fresh.answer.choice}
                    if fresh.answer.answers else {fresh.answer.choice}
                )
                for ap in fresh.approvals:
                    if ap.approve_on and (answered_tokens & set(ap.approve_on)):
                        ap.approved_hash = ap.hash
                        ap.approved_at = at
                        ap.approved_by = by
                        ap.declined = None
                    elif fresh.answer.choice is not None:
                        ap.declined = fresh.answer.choice

            was_archived = fresh.archived
            fresh.state = "decided"
            heading = task_serialize.note_heading(NOTE_KIND["answered"], resolved_role, by, at)
            fresh.body = task_serialize.append_section(fresh.body, heading, text, relayed=relayed)
            if human and was_archived:
                fresh.reopened_at = at
            move = "unarchive" if (human and was_archived) else None
            return fresh, move

        result = self.mutate(id_, change)
        if result.partial_answer is None or result.state != "needs_decision":
            enqueue(self.layout, id_)
            result.queued = True
        return result

    def deliver(self, id_: int) -> Node:
        """``nod read``/``pop`` — ``Store.deliver``: an agent has now seen
        this answer."""
        dequeue(self.layout, id_)

        def change(fresh: Node):
            was_queued = fresh.queued
            if fresh.state != "decided" and not (was_queued and fresh.state == "needs_decision"):
                return fresh, None
            if awaits_decision(fresh.state, fresh.options, fresh.answer, fresh.round_withdrawn):
                fresh.state = "needs_decision"
            else:
                items = self.all_nodes()
                fresh.state = "in_progress" if is_startable(fresh, items) else "not_started"
            return fresh, None

        result = self.mutate(id_, change)
        result.queued = False
        return result

    # -- ask ------------------------------------------------------------

    def ask(self, id_: int, options: list[Option] | None = None, text: str | None = None,
             default: str | None = None, questions: list | None = None, by: str = "owner") -> Node:
        """``nod ask`` — ``Store.ask``. Simplified: does not merge an
        in-text ``## 选项`` block with explicit ``-o`` flags (gap, tracked
        under C1052); supports the common cases of plain re-ask, ``-o``
        flags, and ``--question``."""
        questions = questions or []
        if questions:
            check_questions(questions)
        if questions and options:
            raise StoreError("sub-questions and options don't mix")

        def change(fresh: Node):
            round_ = task_parse.options_round_of(fresh.body) + 1
            if questions:
                given = question_options(questions)
                resolved_default = "1"
            elif options:
                given = options
                resolved_default = default
            else:
                given = fresh.options
                resolved_default = default if default is not None else fresh.default
            if not given:
                raise StoreError("no options to ask with")

            text_note = (text or "").strip()
            if text_note:
                heading = task_serialize.note_heading(NOTE_KIND["new_question"], "agent", by, now_stamp(ms=True))
                fresh.body = task_serialize.append_section(fresh.body, heading, text_note)
            fresh.body = fresh.body.rstrip("\n") + "\n\n" + task_serialize.render_options_block(given, round_)
            if questions:
                fresh.body += "\n" + render_questions_block(questions, round_)

            was_archived = fresh.archived
            fresh.state = "needs_decision"
            fresh.default = resolved_default
            fresh.answer = None
            fresh.questions = questions
            if was_archived:
                fresh.reopened_at = now_stamp(ms=True)
            move = "unarchive" if was_archived else None
            return fresh, move

        dequeue(self.layout, id_)
        result = self.mutate(id_, change)
        result.queued = False
        return result

    # -- notes: edit --------------------------------------------------------

    def edit_body(self, id_: int, text: str, by: str, role: str | None = None, expect: str | None = None) -> Node:
        def change(fresh: Node):
            if expect is not None and fresh.updated_at != expect:
                raise StoreError("the node changed since you last read it")
            # The description is everything before the first `## ` heading.
            lines = fresh.body.split("\n")
            first_h2 = next((i for i, l in enumerate(lines) if task_parse._H2_RE.match(l)), -1)
            tail = "" if first_h2 == -1 else "\n".join(lines[first_h2:])
            rebuilt = (text.strip() + ("\n\n" + tail if tail.strip() else "\n")) if text.strip() else (tail or "\n")
            if rebuilt == fresh.body:
                return None, None
            fresh.body = rebuilt
            heading = task_serialize.note_heading(NOTE_KIND["addendum"], role or role_of(by), by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, "改了描述。")
            return fresh, None

        return self.mutate(id_, change)

    def edit_note(self, id_: int, note_index: int, text: str | None = None, summary: str | None = None,
                   by: str = "owner", role: str | None = None, force: bool = False) -> Node:
        def change(fresh: Node):
            if note_index < 1 or note_index > len(fresh.notes):
                raise StoreError(f"no such note: {note_index}")
            target = fresh.notes[note_index - 1]
            resolved_role = role or role_of(by)
            if target.role != resolved_role and not force:
                raise StoreError("that's not your note; pass --force to edit it anyway")
            if target.kind == NOTE_KIND["answered"] and not force:
                raise StoreError("an answer is a historical record; pass --force to edit it anyway")
            if text is None and summary is None:
                return None, None
            new_text = text if text is not None else target.text
            block = new_text.strip()
            if summary is not None and summary:
                block += f"\n{task_serialize.summary_marker(summary)}"
            # The heading text itself (kind · role · author · second-precision
            # time) is specific enough to be a unique anchor in practice;
            # the reference implementation's `nth`-duplicate disambiguation
            # (two notes written in the same second by the same author) is
            # not ported (gap, tracked under C1052).
            fresh.body = task_serialize.upsert_section(fresh.body, target.heading, block)
            plain = __import__("re").sub(r"<!--[\s\S]*?-->", "", new_text).strip()
            snippet = plain[:20] + ("…" if len(plain) > 20 else "")
            when = target.at[5:16].replace("T", " ")
            audit = f"改了 {target.author} {when} 的那条{target.kind}：「{snippet}」。"
            heading = task_serialize.note_heading(NOTE_KIND["addendum"], role_of(by), by, now_stamp(ms=True))
            fresh.body = task_serialize.append_section(fresh.body, heading, audit)
            return fresh, None

        return self.mutate(id_, change)

    # -- progress / steps ---------------------------------------------------

    def progress(self, id_: int, pct: int, msg: str | None = None) -> Node:
        def change(fresh: Node):
            fresh.progress = max(0, min(100, pct))
            if msg is not None:
                fresh.progress_msg = msg
            if fresh.state == "not_started":
                fresh.state = "in_progress"
            return fresh, None

        return self.mutate(id_, change)

    def set_steps(self, id_: int, labels: list[str]) -> Node:
        def change(fresh: Node):
            fresh.steps = [Step(label=l.strip(), done=False) for l in labels]
            return fresh, None

        return self.mutate(id_, change)

    def mark_step(self, id_: int, n: int, note_text: str | None = None, author: str | None = None) -> Node:
        def change(fresh: Node):
            if not fresh.steps:
                raise StoreError("no steps defined yet")
            if n < 1 or n > len(fresh.steps):
                raise StoreError(f"there are only {len(fresh.steps)} steps")
            fresh.steps = [Step(label=s.label, done=i < n) for i, s in enumerate(fresh.steps)]
            fresh.progress = round(n / len(fresh.steps) * 100)
            fresh.progress_msg = fresh.steps[n - 1].label
            if note_text:
                heading = task_serialize.note_heading(NOTE_KIND["addendum"], "agent", author or fresh.author, now_stamp(ms=True))
                text = f"step {n}/{len(fresh.steps)} · {fresh.steps[n - 1].label}" + (f" — {note_text}" if note_text else "")
                fresh.body = task_serialize.append_section(fresh.body, heading, text)
            if fresh.state == "not_started":
                fresh.state = "in_progress"
            return fresh, None

        return self.mutate(id_, change)

    def beat(self, id_: int, note_text: str | None = None, author: str | None = None) -> Node:
        def change(fresh: Node):
            if note_text:
                heading = task_serialize.note_heading(NOTE_KIND["heartbeat"], "agent", author or fresh.author, now_stamp(ms=True))
                fresh.body = task_serialize.append_section(fresh.body, heading, note_text)
            return fresh, None

        return self.mutate(id_, change)

    # -- attach / approval ---------------------------------------------------

    def attach(self, id_: int, sources: list[str], by: str, *, link: bool = False, name: str | None = None,
                approval_content: bool = False, approve_on: list[str] | None = None,
                caption: str | None = None, group: str | None = None) -> Node:
        def change(fresh: Node):
            from formist_cli import task_attach

            fresh.body, new_attachments = task_attach.attach_sources(
                self.layout, fresh, sources, link=link, name=name, caption=caption, group=group,
            )
            if approval_content:
                keys = approve_on or task_attach.default_approve_on(fresh)
                task_attach.check_approve_on(fresh, keys)
                for a in new_attachments:
                    file_abs = os.path.normpath(os.path.join(os.path.dirname(fresh.file), a.target))
                    digest = task_attach.sha256_file(file_abs) if os.path.isfile(file_abs) else None
                    if digest:
                        fresh.approvals = [ap for ap in fresh.approvals if ap.target != a.target]
                        fresh.approvals.append(AttachmentApproval(target=a.target, hash=digest, approve_on=list(keys)))
            return fresh, None

        return self.mutate(id_, change)

    def approved_check(self, id_: int, path: str) -> tuple[int, str]:
        """Returns ``(exit_code, reason)``: 0 matches, 1 changed since
        approval, 2 never approved / no matching record."""
        from formist_cli import task_attach

        node = self.get(id_)
        rel = os.path.relpath(os.path.abspath(path), os.path.dirname(node.file))
        rel = to_posix_path(rel)
        ap = next((a for a in node.approvals if a.target == rel or os.path.abspath(
            os.path.join(os.path.dirname(node.file), a.target)) == os.path.abspath(path)), None)
        if not ap or not ap.approved_hash:
            return 2, "never approved"
        current = task_attach.sha256_file(path) if os.path.isfile(path) else None
        if current is None:
            return 2, "file not found"
        if ap.approved_hash != current:
            return 1, "content changed since it was approved"
        return 0, "matches"

    # -- search / tree / show -----------------------------------------------

    def search(self, words: list[str], include_archived: bool = False) -> list[Node]:
        from formist_cli import task_search

        nodes = self.all_nodes()
        if not include_archived:
            nodes = [n for n in nodes if not n.archived]
        return task_search.search(nodes, words)

    def tree(self, root_ref: str | None = None, include_archived: bool = False) -> list[Node]:
        nodes = self.all_nodes()
        if not include_archived:
            nodes = [n for n in nodes if not n.archived]
        if root_ref is None:
            return [n for n in nodes if n.parent is None]
        root = self.resolve(root_ref)
        return [n for n in nodes if n.id == root.id]

    def show(self, view: str = "available") -> list[Node]:
        from formist_cli import task_views

        return task_views.apply_view(self.all_nodes(), view)

    def changes(self, since: str) -> list[dict]:
        """``nod changes`` — simplified: derived from every node's own
        notes and a few front-matter timestamps, no event log, matching
        the reference implementation's own "there's no daemon" design.
        Source-item events (``nod source``) are not ported (gap)."""
        since_t = _parse_epoch(since) or 0.0
        events: list[dict] = []
        for n in self.all_nodes():
            created_t = _parse_epoch(n.created)
            if created_t is not None and created_t >= since_t:
                events.append({"ref": format_ref(n.id, n.letter), "kind": "created", "at": n.created, "title": n.title})
            for note in n.notes:
                at_t = _parse_epoch(note.at)
                if at_t is not None and at_t >= since_t:
                    events.append({
                        "ref": format_ref(n.id, n.letter), "kind": note.kind, "role": note.role,
                        "author": note.author, "at": note.at,
                    })
        events.sort(key=lambda e: e["at"])
        return events


def _owners_turn_after(node: Node, human: bool, relayed: bool) -> str:
    if not human:
        return node.state
    if relayed and awaits_decision(node.state, node.options, node.answer, node.round_withdrawn):
        return node.state
    return "decided"


def _parse_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
