"""Cloud mode for ``fm task``: the client side of contract §4's API surface
(``routes/task_nodes.py``, mounted at ``/projects/{space}/nodes`` — landed
C1092/bead5c101, events+sessions C1093/fb43e4761).

:class:`CloudStore` is deliberately duck-type compatible with
:class:`formist_cli.task_store.LocalStore`: the same method names, same
positional/keyword shapes, so every ``_cmd_*`` handler in ``tasks.py``
written against a local ``Node`` works unchanged against a cloud result —
see :class:`CloudNode`, which wraps one server ``NodeOut`` JSON dict with
the attributes a local ``Node`` has (``.id``/``.letter``/``.state``/
``.title``/``.body``), so ``format_ref(node.id, node.letter)`` and
``node.state == "waiting"`` work on either.

Every call passes ``for_write=False`` to ``context.resolve`` (see
:func:`cloud_store`), regardless of the verb: elsewhere in `fm`,
``for_write=True`` makes a write refuse to inherit ``fm use``'s
remembered environment, because a write landing on a destination nobody
consciously picked FOR THAT COMMAND is the failure ``context.py`` exists
to prevent. `fm task`'s own mode resolution (``resolve_mode``, contract
§6.3) already is that conscious choice, one level up: it explicitly
names "`fm use` with a stored credential" as a deliberate way to select
remote mode, on purpose so a configured user does not have to spell out
``--host`` on every single call. Re-asking the question inside
``cloud_store`` after ``resolve_mode`` already answered it would not add
safety, it would just contradict §6.3 and force ``--host`` on every
cloud-mode write, which the contract does not ask for.

**What is NOT wired here** (a real server route exists for some of
these; a documented gap, not an oversight, same discipline as every
other `fm task` batch this session):

- ``show``/``views``/``view``: the server's only list filter
  (``GET /nodes?view=``) is the 7-id BENCH vocabulary (available/
  active/decide/waiting/live/all/external/mine) — a different,
  unrelated set of words from ``show``'s own scope vocabulary (open/
  answered/archived/fyi/deferred/flagged/due/remind/<status>; see
  ``task_views.py``'s own split of the two). There is no server route
  for ``show``'s scope language, and no ``/node-views`` route for
  custom views yet, so claiming either here would be exactly the kind
  of same-word-different-meaning bug this project has hit before
  (C1137's `nod show` vs `nod bench` conflation). Both raise.
- ``search``, ``changes``, ``tell``, ``sources``: no server route.
- ``attach``/``approved --check``: no server route (multipart upload).
- ``flag``/``order`` (``arrange``/``unarrange``): no server route for
  toggling ``important`` or a manual sort position on an existing
  node (milestone-at-creation and milestone-via-``assign`` both work,
  through :meth:`CloudStore.create` and :meth:`CloudStore.set_milestone`).
- ``wait``/``watch``: superseded by :meth:`CloudStore.watch_events`
  (an SSE push beats a poll loop), not reimplemented as a second path.
- ``check``/``config``: no server equivalent — these are about a local
  ``decisions/`` tree and ``decs.json``, which cloud mode has neither of.
- Note-editing (``edit --note N``): contract §4.1 lists
  ``PATCH /nodes/{ref}/notes/{n}``, but it is not in the landed route
  table (only whole-node ``PATCH /nodes/{ref}`` exists) — not ported
  pending that route.
- ``supersede``'s second return value (the new node) is not re-fetched:
  the only caller, ``_cmd_supersede``, never reads it.
- ``claim``'s ``force`` has no server bypass; a 409 is still reported
  as a refusal.
"""

from __future__ import annotations

import re
from typing import Any

from formist_cli import api
from formist_cli.api import ApiError
from formist_cli.logs import parse_sse
from formist_cli.task_assignee import HUMAN
from formist_cli.task_ref import format_ref
from formist_cli.task_store import StoreError, role_of

#: Module-level, not a lazy per-call import, so a test can
#: ``monkeypatch.setattr(task_cloud, "api", stub)`` the way every other
#: `fm` command module exposing ``api`` at module scope already does
#: (``spaces.py``, ``chat.py``, ...) — see ``tests/test_task_cloud.py``.
__all__ = ["CloudError", "CloudNode", "CloudNotAvailable", "CloudStore", "cloud_store"]

_REF_LETTER_RE = re.compile(r"^([A-Za-z])\d+$")


class CloudError(Exception):
    pass


class CloudNotAvailable(CloudError):
    def __init__(self, verb: str, *, reason: str | None = None):
        detail = reason or (
            "the server's /projects/{id}/nodes router has this verb, but a client "
            "for it has not been wired up here yet"
        )
        super().__init__(f"`fm task {verb}` (cloud mode): {detail}. Use --local for a decisions/ directory.")
        self.verb = verb


class CloudNode:
    """One server ``NodeOut`` JSON dict, exposed with the attributes a
    local ``task_model.Node`` has, so code written against a local node
    (printing, ``format_ref``, a ``node.state == "waiting"`` check) runs
    unchanged against a cloud result — see the module docstring.

    ``to_json()``/``task_json.to_json`` read ``_raw_json`` straight
    through rather than re-deriving it: the server's shape already IS
    noddit's ``--json`` shape (routes/task_nodes.py's own docstring),
    so re-deriving it through `Node`'s fields would be redundant at
    best and a second, divergent implementation at worst.
    """

    __slots__ = ("_raw_json",)

    def __init__(self, data: dict):
        self._raw_json = data

    def __getattr__(self, name: str) -> Any:
        if name == "state":
            return self._raw_json.get("status")
        if name == "letter":
            m = _REF_LETTER_RE.match(self._raw_json.get("ref") or "")
            return m.group(1) if m else None
        try:
            return self._raw_json[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"CloudNode({self._raw_json.get('ref')!r}, {self._raw_json.get('title')!r})"


def cloud_store(args) -> CloudStore:
    """Resolve ``--host``/``--space``/the stored token and build a
    :class:`CloudStore`. The one place ``context.resolve`` is called for
    `fm task`; see the module docstring for why ``for_write`` is always
    false here."""
    from formist_cli import context

    ctx = context.resolve(args, for_write=False, need_space=True, need_token=True)
    return CloudStore(ctx)


class CloudStore:
    def __init__(self, ctx):
        self.ctx = ctx
        #: already validated as a UUID by ``targets.require_space_id``
        #: (``context.resolve`` → ``prefs.effective``).
        self.project_id = ctx.space

    # -- transport -----------------------------------------------------------
    def _base(self) -> str:
        return f"/projects/{self.project_id}/nodes"

    def _get(self, suffix: str = "", params: dict[str, str] | None = None) -> Any:
        return api.get(self.ctx.target, self._base() + suffix, token=self.ctx.token, params=params)

    def _post(self, suffix: str, body: dict | None = None) -> Any:
        return api.post(self.ctx.target, self._base() + suffix, _drop_none(body or {}), token=self.ctx.token)

    def _patch(self, suffix: str, body: dict) -> Any:
        return api.patch(self.ctx.target, self._base() + suffix, _drop_none(body), token=self.ctx.token)

    def _put(self, suffix: str, body: dict) -> Any:
        return api.put(self.ctx.target, self._base() + suffix, _drop_none(body), token=self.ctx.token)

    def _verb(self, ref: str, verb: str, **body: Any) -> CloudNode:
        """``POST /nodes/{ref}/{verb}`` — the simple-lifecycle-verb route
        (contract §4.1's ``VerbIn``: ``author``, ``note``, ``reason``,
        ``until``, ``on``, ``parent``, ``title``, ``by``, ``session_id``)."""
        return CloudNode(self._post(f"/{ref}/{verb}", body))

    # -- reads -----------------------------------------------------------------
    def resolve(self, ref: str) -> CloudNode:
        return CloudNode(self._get(f"/{ref}"))

    def get(self, ref: str) -> CloudNode:
        return self.resolve(ref)

    def tree(self, root_ref: str | None = None, include_archived: bool = False) -> list[CloudNode]:
        """``GET /nodes/tree`` returns every node, unfiltered; ``root_ref``/
        ``include_archived`` are applied client-side (the route takes
        neither param)."""
        data = self._get("/tree")
        nodes = [CloudNode(n) for n in data["nodes"]]
        if not include_archived:
            nodes = [n for n in nodes if not n.archived]
        if root_ref:
            root = self.resolve(root_ref)
            keep = {root.id}
            changed = True
            while changed:
                changed = False
                for n in nodes:
                    if n.parent in keep and n.id not in keep:
                        keep.add(n.id)
                        changed = True
            nodes = [n for n in nodes if n.id in keep]
        return nodes

    def briefs(self, include_claimed: bool = False) -> list[CloudNode]:
        """``GET /nodes/briefs`` already excludes claimed ones server-side
        and has no param to include them — ``include_claimed`` has no
        effect here (gap: see the module docstring)."""
        data = self._get("/briefs")
        return [CloudNode(n) for n in data["nodes"]]

    def queue_refs(self) -> list[CloudNode]:
        """``GET /nodes/queue`` — used by ``fm task queue``'s cloud branch
        in ``tasks.py`` (that command prints ids directly, unlike every
        other verb here, so it does not go through :class:`CloudNode`'s
        ``_cmd_*`` convention)."""
        data = self._get("/queue")
        return [CloudNode(n) for n in data["nodes"]]

    # -- create / edit -----------------------------------------------------------
    def create(
        self, title: str, author: str, *, body: str = "", options=None,
        default: str | None = None, task: bool = False, fyi: bool = False, tags=None,
        urgency: str = "normal", due: str | None = None, parent: str | None = None, after=None,
        est: str | None = None, assignee: str | None = None, milestone: bool = False, intake: bool = False,
        remind_at: str | None = None, if_no_answer: str | None = None, reversible: bool | None = None,
    ) -> CloudNode:
        """``POST /nodes`` — contract §4.1's ``AddNode``."""
        kind = "milestone" if milestone else "brief" if intake else "fyi" if fyi else "task" if task else "decision"
        payload: dict[str, Any] = {
            "title": title, "author": author, "body": body or None, "kind": kind,
            "parent": parent, "after": list(after or []), "est": est, "due": due,
            "remind_at": remind_at, "urgency": urgency, "tags": list(tags or []),
            "options": [{"key": o.key, "label": o.label, "body": o.body} for o in (options or [])],
            "default": default, "if_no_answer": if_no_answer, "reversible": reversible,
        }
        if assignee == HUMAN:
            payload["kind"] = "for_me" if kind == "task" else kind
        return CloudNode(self._post("", payload))

    def edit_body(self, id_: int, text: str, by: str, role: str | None = None, expect: str | None = None) -> CloudNode:
        """``PATCH /nodes/{ref}`` — ``expect`` (optimistic-lock) has no
        server equivalent on this route and is accepted but ignored,
        matching the local signature so callers don't need to branch."""
        return CloudNode(self._patch(f"/{id_}", {"body": text}))

    def edit_note(self, *args: Any, **kwargs: Any):
        raise CloudNotAvailable(
            "edit --note",
            reason="contract §4.1 lists PATCH /nodes/{ref}/notes/{n}, but it is not "
            "in the landed route table yet",
        )

    def note(self, id_: int, text: str, author: str, role: str | None = None, kind: str | None = None,
             summary: str | None = None, relayed: bool = False) -> CloudNode:
        """``POST /nodes/{ref}/notes`` — contract §4.1's ``NoteIn``."""
        body: dict[str, Any] = {"body": text, "author": author, "summary": summary, "relayed": relayed}
        if role:
            body["role"] = role
        if kind:
            body["kind"] = kind
        return CloudNode(self._post(f"/{id_}/notes", body))

    def progress(self, id_: int, pct: int, msg: str | None = None) -> CloudNode:
        return CloudNode(self._post(f"/{id_}/progress", {"percent": pct, "message": msg}))

    def set_steps(self, id_: int, labels: list[str]) -> CloudNode:
        return CloudNode(self._put(f"/{id_}/steps", {"steps": list(labels)}))

    def mark_step(self, id_: int, n: int, note_text: str | None = None, author: str | None = None) -> CloudNode:
        return CloudNode(self._post(f"/{id_}/steps/{n}"))

    def beat(self, id_: int, note_text: str | None = None, author: str | None = None) -> CloudNode:
        return CloudNode(self._post(f"/{id_}/beat"))

    def set_after(self, id_: int, after: list[str]) -> CloudNode:
        return CloudNode(self._put(f"/{id_}/after", {"after": list(after or [])}))

    # -- decisions -----------------------------------------------------------
    def ask(self, id_: int, options=None, text: str | None = None, default: str | None = None,
             questions=None, by: str = "owner") -> CloudNode:
        payload = {
            "author": by, "body": text,
            "options": [{"key": o.key, "label": o.label, "body": o.body} for o in (options or [])],
            "default": default,
        }
        return CloudNode(self._post(f"/{id_}/ask", payload))

    def answer(self, id_: int, choice: str | None = None, note_text: str | None = None, by: str = "owner",
               role: str | None = None, answers: dict[str, str] | None = None, relayed: bool = False,
               partial: bool = False) -> CloudNode:
        """``POST /nodes/{ref}/answer``. ``partial`` has no client-side
        meaning here — the server decides complete-vs-partial itself from
        whether every sub-question got an answer (contract §4.1)."""
        payload = {"choice": choice, "note": note_text, "author": by, "relayed": relayed, "answers": answers or {}}
        return CloudNode(self._post(f"/{id_}/answer", payload))

    def deliver(self, id_: int) -> CloudNode:
        """``nod read``/``pop`` with no ``--ack``: just clears the queue
        flag. ``tasks.py``'s ``_cmd_read`` calls :meth:`note` separately
        first when ``--ack`` is given, matching the local flow."""
        return CloudNode(self._post(f"/{id_}/read", {}))

    # -- assignment / milestone -----------------------------------------------
    def assign(self, id_: int, assignee: str | None) -> CloudNode:
        """``POST /nodes/{ref}/assign``. Noddit's own assignee model is
        binary (human owner / AI) — ``assignee`` is either
        ``task_assignee.HUMAN`` or ``None`` (AI), never a specific
        person, so this maps onto the server's ``to=me`` (the caller's
        own person row) / ``to=ai``, never ``to=people``."""
        return CloudNode(self._post(f"/{id_}/assign", {"to": "me" if assignee == HUMAN else "ai"}))

    def set_milestone(self, id_: int, on: bool) -> CloudNode:
        if not on:
            raise CloudNotAvailable("assign <ref> milestone --off", reason="un-marking a milestone has no server route yet")
        return CloudNode(self._post(f"/{id_}/assign", {"to": "milestone"}))

    def flag(self, id_: int, important: bool | None = None, milestone: bool | None = None,
              due: str | None = None) -> CloudNode:
        if milestone:
            return self.set_milestone(id_, True)
        raise CloudNotAvailable("flag", reason="toggling `important` on an existing node has no server route yet")

    # -- lifecycle (the generic verb route) -----------------------------------
    def start(self, id_: int, force: bool = False) -> CloudNode:
        return self._verb(id_, "start")

    def complete(self, id_: int, note_text: str | None = None, author: str | None = None) -> CloudNode:
        return self._verb(id_, "done", note=note_text, author=author)

    def close(self, id_: int, reason: str) -> CloudNode:
        return self._verb(id_, "close", reason=reason)

    def dismiss(self, id_: int, by: str) -> CloudNode:
        """``nod archive --seen`` — the server's plain ``archive`` verb
        (archives without completing), not :meth:`complete`."""
        return self._verb(id_, "archive", author=by)

    def unarchive(self, id_: int) -> CloudNode:
        return self._verb(id_, "unarchive")

    def retitle(self, id_: int, title: str, by: str, role: str | None = None) -> CloudNode:
        return self._verb(id_, "retitle", title=title, author=by)

    def move(self, id_: int, *, parent: str | None = None, root: bool = False) -> CloudNode:
        return self._verb(id_, "move", parent=None if root else parent)

    def supersede(self, old_id: int, new_id: int, author: str) -> tuple[CloudNode, None]:
        """Returns ``(old_node, None)``: the server route returns only the
        superseded node, and the only caller (``_cmd_supersede``) never
        reads the second element (see the module docstring)."""
        new_ref = self.resolve(str(new_id))
        by_ref = new_ref.ref if hasattr(new_ref, "ref") else format_ref(new_ref.id, new_ref.letter)
        return self._verb(old_id, "supersede", by=by_ref, author=author), None

    def claim(self, id_: int, by: str, force: bool = False) -> tuple[CloudNode, bool]:
        """Returns ``(node, already)``. ``force`` has no server bypass:
        an already-claimed 409 is reported as a refusal either way
        (gap, see the module docstring)."""
        try:
            return self._verb(id_, "claim", author=by), False
        except ApiError as exc:
            if exc.status == 409:
                return self.resolve(str(id_)), True
            raise

    def defer(self, id_: int, by: str, role: str | None = None, until: str | None = None,
              owner_said: str | None = None) -> CloudNode:
        """``POST /nodes/{ref}/defer``. The server's generic verb route
        hardcodes every write it handles as agent-authored (contract
        §4.1: ``node_verb`` always builds its actor with role ``agent``),
        so the "an agent needs the owner's word to defer" rule has no
        server-side enforcement on this route — ported here client-side,
        same as :meth:`task_store.LocalStore.defer`."""
        resolved_role = role or role_of(by)
        if resolved_role != "human" and not (owner_said and owner_said.strip()):
            raise StoreError(
                "an agent may only defer on the owner's word — pass --owner-said "
                '"<what they said>", or --human if the owner is doing this themselves'
            )
        return self._verb(id_, "defer", until=until, author=by)

    def undefer(self, id_: int, by: str, role: str | None = None) -> CloudNode:
        return self._verb(id_, "undefer", author=by)

    def hold(self, id_: int, on: str | None, author: str, role: str | None = None,
             until: str | None = None) -> CloudNode:
        return self._verb(id_, "wait", on=on, until=until, author=author)

    def unhold(self, id_: int, author: str, role: str | None = None) -> CloudNode:
        """There is no separate "stop waiting" verb server-side — the same
        ``undefer`` route clears both ``waiting`` and a passive defer."""
        return self._verb(id_, "undefer", author=author)

    # -- not (yet) available ----------------------------------------------------
    def show(self, scope: str = "open", stale: bool = False):
        raise CloudNotAvailable(
            "show",
            reason="the server's only node-list filter is bench's 7 view ids, a different "
            "vocabulary from `show`'s own scopes — see the module docstring",
        )

    def search(self, words, include_archived: bool = False):
        raise CloudNotAvailable("search")

    def arrange(self, ids):
        raise CloudNotAvailable("order")

    def unarrange(self, id_):
        raise CloudNotAvailable("order")

    def attach(self, *args, **kwargs):
        raise CloudNotAvailable("attach")

    def approved_check(self, *args, **kwargs):
        raise CloudNotAvailable("approved")

    def expand(self, *args, **kwargs):
        raise CloudNotAvailable("expand", reason="not landed")

    def check(self):
        raise CloudNotAvailable("check", reason="there is no decisions/ tree in cloud mode")

    def changes(self, since):
        raise CloudNotAvailable("changes")

    # -- live agent sessions (contract §7) ------------------------------------
    def register_session(self, client_session: str, *, client: str = "claude-code", label: str | None = None,
                          author: str | None = None, cwd_hash: str | None = None, current: str | None = None,
                          ended: bool = False) -> dict:
        """``POST /agent-sessions`` — register or heartbeat one row for
        this client session."""
        path = f"/projects/{self.project_id}/agent-sessions"
        payload = _drop_none({
            "client_session": client_session, "client": client, "label": label,
            "author": author, "cwd_hash": cwd_hash, "current": current, "ended": ended,
        })
        return api.post(self.ctx.target, path, payload, token=self.ctx.token)

    # -- the monitor stream (contract §5) -------------------------------------
    #: Generous relative to the server's 15s SSE heartbeat
    #: (services/events.py's SSE_HEARTBEAT_SECONDS) — big enough that a
    #: couple of missed pings on a slow network is not mistaken for a
    #: dead connection, small enough that a genuinely dead one is caught
    #: well inside a human's patience.
    EVENTS_READ_TIMEOUT = 60.0

    def watch_events(self, mine: str | None = None):
        """``GET /nodes/events`` (SSE): yields one noddit monitor line per
        event, exactly as the server rendered it — ``fm task monitor``
        prints these verbatim, the same contract local mode's own
        ``task_monitor.run_pass`` output follows (contract §6.2: same
        names, same shape)."""
        params = {"mine": mine} if mine else None
        with api.stream(
            self.ctx.target, self._base() + "/events", token=self.ctx.token, params=params,
            read_timeout=self.EVENTS_READ_TIMEOUT,
        ) as lines:
            for event in parse_sse(lines):
                if event["event"] != "line":
                    continue
                data = event["data"]
                if isinstance(data, str) and data:
                    yield data


def _drop_none(body: dict) -> dict:
    return {k: v for k, v in body.items() if v is not None}
