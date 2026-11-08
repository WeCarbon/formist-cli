"""Cloud mode for ``fm task``: :class:`task_cloud.CloudStore` against a fake
server built from contract §4.1's shapes (``routes/task_nodes.py``).

No live server is driven here — team-lead's brief for C1141 explicitly
calls that optional. These tests drive ``CloudStore`` exactly as
``tasks.py``'s ``_cmd_*`` handlers do, through a stub ``task_cloud.api``
that records every request and answers with a canned ``NodeOut``-shaped
dict, the same fake-API convention ``test_spaces.py``'s ``driven``
fixture uses (``monkeypatch.setattr(module, "api", stub)``) — ``api`` is
a module-level name in ``task_cloud.py`` for exactly this reason.
"""

from __future__ import annotations

import contextlib
import types

import pytest

from formist_cli import context, task_cloud
from formist_cli.targets import resolve as resolve_target
from formist_cli.task_assignee import HUMAN
from formist_cli.task_model import Option

SPACE = "11111111-1111-1111-1111-111111111111"
TARGET = resolve_target("formist")


def _node(**over) -> dict:
    base = {
        "id": 5, "task_id": "t-5", "ref": "X5", "project": "space", "project_name": "space",
        "title": "A node", "author": "agent · x", "status": "not_started", "status_key": "todo",
        "status_label": "To do", "close_reason": None, "waiting_on": None, "waiting_until": None,
        "deferred_at": None, "deferred_by": None, "deferred_until": None, "assignee": None,
        "assignees": [], "human_task": False, "created": "2026-11-06T10:00:00+08:00",
        "updated_at": "2026-11-06T10:00:00+08:00", "after": [], "progress": None,
        "progress_msg": None, "est": None, "est_minutes": None, "est_who": None, "every": None,
        "when": None, "stale_after": None, "checkin": None, "steps": None, "owner_agent": None,
        "intake": False, "brief": None, "claimed_by": None, "claimed_at": None, "original": None,
        "parent": None, "parent_ref": None, "children": [], "default": None, "order": None,
        "kind": "task", "urgency": "normal", "due": None, "remind_at": None, "milestone": False,
        "important": False, "at_risk": False, "if_no_answer": None, "reversible": None,
        "session": None, "claimed_session_id": None, "supersedes": [], "superseded_by": None,
        "options": [], "answer": None, "questions": [], "answers": None, "answer_notes": None,
        "partial_answer": None, "previous_rounds": [], "notes": [], "queued": False,
        "archived_at": None, "reopened_at": None, "reopened_after_archive": False, "tags": [],
        "attachments": [], "body_attachments": [], "approvals": [], "run": None, "body": "",
        "archived": False, "extra": {},
    }
    base.update(over)
    return base


class _Recording:
    """Every ``api`` call the real transport exposes, logged and answered
    from a queue — a test pops exactly as many responses as it expects
    calls, so an unexpected extra call fails loudly (``IndexError``)
    rather than quietly reusing the last response."""

    def __init__(self, *responses):
        self.calls: list[tuple] = []
        self._responses = list(responses)

    def _next(self):
        return self._responses.pop(0) if self._responses else _node()

    def get(self, target, path, *, token, params=None, **_kw):
        self.calls.append(("GET", path, params))
        return self._next()

    def post(self, target, path, body, *, token, **_kw):
        self.calls.append(("POST", path, body))
        return self._next()

    def patch(self, target, path, body, *, token, **_kw):
        self.calls.append(("PATCH", path, body))
        return self._next()

    def put(self, target, path, body, *, token, **_kw):
        self.calls.append(("PUT", path, body))
        return self._next()

    @contextlib.contextmanager
    def stream(self, target, path, *, token, params=None, read_timeout):
        """The real transport (``api._stream_lines``) yields one already
        rstrip'd LINE per item, never a multi-line blob — the fixture
        blocks below are written as one string per SSE frame for
        readability and split here, matching that shape exactly."""
        self.calls.append(("STREAM", path, params))
        block = self._responses.pop(0) if self._responses else []
        lines = [line for frame in block for line in frame.split("\n")]
        yield iter(lines)


@pytest.fixture
def store(monkeypatch):
    api_stub = _Recording()
    monkeypatch.setattr(task_cloud, "api", api_stub)
    ctx = context.Context(target=TARGET, space=SPACE, token="t", space_source="test")
    return task_cloud.CloudStore(ctx), api_stub


# ===========================================================================
# CloudNode: the duck-typed attribute surface _cmd_* handlers rely on
# ===========================================================================
def test_cloud_node_exposes_state_and_letter_from_status_and_ref():
    n = task_cloud.CloudNode(_node(status="in_progress", ref="W42", title="Ship it"))
    assert n.state == "in_progress"
    assert n.letter == "W"
    assert n.id == 5
    assert n.title == "Ship it"
    assert n.body == ""


def test_cloud_node_unknown_attribute_raises_attribute_error():
    n = task_cloud.CloudNode(_node())
    with pytest.raises(AttributeError):
        _ = n.nonexistent_field


def test_to_json_passes_the_raw_dict_through_unchanged():
    from formist_cli import task_json

    raw = _node(title="Round trips")
    n = task_cloud.CloudNode(raw)
    assert task_json.to_json(n) is raw  # pyright: ignore[reportArgumentType] -- intentional duck type


# ===========================================================================
# reads
# ===========================================================================
def test_resolve_gets_the_ref_path(store):
    cs, api_stub = store
    node = cs.resolve("X5")
    assert isinstance(node, task_cloud.CloudNode)
    assert api_stub.calls == [("GET", "/projects/11111111-1111-1111-1111-111111111111/nodes/X5", None)]


def test_tree_filters_archived_and_root_client_side(store):
    cs, api_stub = store
    api_stub._responses = [{
        "nodes": [
            _node(id=1, ref="X1", parent=None, archived=False),
            _node(id=2, ref="X2", parent=1, archived=False),
            _node(id=3, ref="X3", parent=None, archived=True),
        ],
    }]
    nodes = cs.tree()
    assert {n.id for n in nodes} == {1, 2}

    api_stub._responses = [
        {"nodes": [
            _node(id=1, ref="X1", parent=None, archived=False),
            _node(id=2, ref="X2", parent=1, archived=False),
            _node(id=3, ref="X3", parent=None, archived=False),
        ]},
        _node(id=1, ref="X1"),  # the resolve() call inside tree() for root_ref
    ]
    nodes = cs.tree(root_ref="X1")
    assert {n.id for n in nodes} == {1, 2}


def test_queue_refs_lists_queue_nodes(store):
    cs, api_stub = store
    api_stub._responses = [{"nodes": [_node(id=9, ref="X9")]}]
    refs = cs.queue_refs()
    assert [n.id for n in refs] == [9]
    assert api_stub.calls[0][1].endswith("/nodes/queue")


# ===========================================================================
# search (C1130)
# ===========================================================================
def test_search_joins_words_into_one_q_and_lists_nodes(store):
    cs, api_stub = store
    api_stub._responses = [{"nodes": [_node(id=3, ref="X3", title="Ship it")]}]
    hits = cs.search(["ship", "it"])
    assert [n.id for n in hits] == [3]
    method, path, params = api_stub.calls[0]
    assert method == "GET" and path.endswith("/nodes/search")
    assert params == {"q": "ship it"}


def test_search_include_archived_sends_all_true(store):
    cs, api_stub = store
    api_stub._responses = [{"nodes": []}]
    cs.search(["x"], include_archived=True)
    assert api_stub.calls[0][2] == {"q": "x", "all": "true"}


# ===========================================================================
# changes (C1130)
# ===========================================================================
def test_changes_returns_the_events_list(store):
    cs, api_stub = store
    api_stub._responses = [{
        "since": "2026-01-01T00:00:00+00:00", "until": "2026-01-02T00:00:00+00:00",
        "next_since": "2026-01-02T00:00:00+00:00",
        "events": [{"ref": "X1", "project": "p", "title": "A", "entry_author": "a", "at": "...", "kind": "created", "role": "agent", "by": "a"}],
    }]
    events = cs.changes("2026-01-01T00:00:00+00:00")
    assert len(events) == 1
    method, path, params = api_stub.calls[0]
    assert method == "GET" and path.endswith("/nodes/changes") and params == {"since": "2026-01-01T00:00:00+00:00"}


def test_changes_passes_kind_and_note_kind_through_unchanged(store):
    """``kind`` (feat/noddit-tell 70886dc53) is already the six-value
    discriminator ``LocalStore.changes``'s own events use — no
    translation needed. A ``kind="note"`` row's finer activity type
    lives in the separate ``note_kind`` field, left as-is (an earlier
    server revision used ``event`` for the discriminator instead,
    needing a backfill here — removed along with that revision)."""
    cs, api_stub = store
    api_stub._responses = [{"events": [
        {"ref": "X1", "at": "t1", "kind": "completed"},
        {"ref": "X2", "at": "t2", "kind": "note", "note_kind": "ask"},
    ]}]
    events = cs.changes("2026-01-01T00:00:00+00:00")
    assert events[0]["kind"] == "completed"
    assert events[1]["kind"] == "note"
    assert events[1]["note_kind"] == "ask"


# ===========================================================================
# tell (C1130)
# ===========================================================================
def _tell(**over):
    base = {
        "id": "t-1", "text": "please check the budget", "under": None, "author": "owner",
        "important": False, "created_at": "2026-01-01T00:00:00+00:00", "routed": "session",
        "done": None, "node": None, "chat_id": None,
    }
    base.update(over)
    return base


def test_tell_posts_text_author_under_important(store):
    cs, api_stub = store
    api_stub._responses = [_tell()]
    result = cs.tell("please check the budget", author="owner", under="X5", important=True)
    assert result["id"] == "t-1"
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/tell")
    assert body == {"text": "please check the budget", "author": "owner", "under": "X5", "important": True}


def test_tell_refuses_attach_naming_the_reason(store):
    cs, _api = store
    with pytest.raises(task_cloud.CloudNotAvailable, match="attachments"):
        cs.tell("x", attach=["/some/file"])


def test_tell_cloud_routing_carries_done_node_and_chat_id(store):
    cs, api_stub = store
    api_stub._responses = [_tell(
        routed="cloud", done={"at": "2026-01-01T00:00:01+00:00", "refs": ["X9"]},
        node=_node(id=9, ref="X9"), chat_id="chat-1",
    )]
    result = cs.tell("do a thing")
    assert result["routed"] == "cloud"
    assert result["done"]["refs"] == ["X9"]
    assert result["chat_id"] == "chat-1"


def test_tell_list_unread_only_by_default(store):
    cs, api_stub = store
    api_stub._responses = [{"items": [_tell(id="t-1")]}]
    items = cs.tell_list()
    assert [i["id"] for i in items] == ["t-1"]
    method, path, params = api_stub.calls[0]
    assert method == "GET" and path.endswith("/tell") and params == {"unread": "1"}


def test_tell_list_include_finished_sends_unread_0(store):
    cs, api_stub = store
    api_stub._responses = [{"items": []}]
    cs.tell_list(include_finished=True)
    assert api_stub.calls[0][2] == {"unread": "0"}


def test_tell_read_finds_the_matching_id_from_the_full_list(store):
    """No GET /tell/{id} route — this reads the full list and picks one,
    matching `nod tell --read`'s own "print one, change nothing"."""
    cs, api_stub = store
    api_stub._responses = [{"items": [_tell(id="t-1", text="first"), _tell(id="t-2", text="second")]}]
    result = cs.tell_read("t-2")
    assert result["text"] == "second"
    assert api_stub.calls[0][2] == {"unread": "0"}  # reads everything, not just unread


def test_tell_read_raises_not_found_for_an_unknown_id(store):
    from formist_cli.task_store import NotFoundError

    cs, api_stub = store
    api_stub._responses = [{"items": []}]
    with pytest.raises(NotFoundError):
        cs.tell_read("nope")


def test_tell_finish_posts_refs(store):
    cs, api_stub = store
    api_stub._responses = [_tell(id="t-1", done={"at": "...", "refs": ["X1", "X2"]})]
    result = cs.tell_finish("t-1", ["X1", "X2"])
    assert result["done"]["refs"] == ["X1", "X2"]
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/tell/t-1/finish") and body == {"refs": ["X1", "X2"]}


# ===========================================================================
# create / edit
# ===========================================================================
def test_create_posts_add_node_shape(store):
    cs, api_stub = store
    cs.create(
        "Ship it", "agent · x", options=[Option(key="1", label="A", body="")],
        default="1", tags=["ops"], due="2026-12-01T23:59:59+08:00", urgency="high", parent="X10",
    )
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/nodes")
    assert body["title"] == "Ship it" and body["author"] == "agent · x"
    assert body["kind"] == "decision"
    assert body["options"] == [{"key": "1", "label": "A", "body": ""}]
    assert body["default"] == "1" and body["tags"] == ["ops"] and body["parent"] == "X10"


def test_create_task_kind(store):
    cs, _api = store
    cs.create("Plain task", "agent · x", task=True)
    assert _api.calls[0][2]["kind"] == "task"


def test_create_for_me_maps_human_assignee_onto_for_me_kind(store):
    cs, api_stub = store
    cs.create("Call the vendor", "agent · x", task=True, assignee=HUMAN)
    assert api_stub.calls[0][2]["kind"] == "for_me"


def test_edit_body_patches_the_node(store):
    cs, api_stub = store
    cs.edit_body(5, "new body", "agent · x")
    method, path, body = api_stub.calls[0]
    assert method == "PATCH" and path.endswith("/nodes/5") and body == {"body": "new body"}


def test_note_posts_role_and_kind_when_given(store):
    cs, api_stub = store
    cs.note(5, "heads up", "agent · x", role="agent", kind="已收到", summary="s")
    _method, path, body = api_stub.calls[0]
    assert path.endswith("/5/notes")
    assert body["role"] == "agent" and body["kind"] == "已收到" and body["summary"] == "s"


def test_progress_and_steps(store):
    cs, api_stub = store
    cs.progress(5, 50, "halfway")
    cs.set_steps(5, ["a", "b"])
    cs.mark_step(5, 1)
    cs.beat(5)
    paths = [c[1] for c in api_stub.calls]
    assert paths == [
        "/projects/11111111-1111-1111-1111-111111111111/nodes/5/progress",
        "/projects/11111111-1111-1111-1111-111111111111/nodes/5/steps",
        "/projects/11111111-1111-1111-1111-111111111111/nodes/5/steps/1",
        "/projects/11111111-1111-1111-1111-111111111111/nodes/5/beat",
    ]


def test_set_after_puts_the_refs(store):
    cs, api_stub = store
    cs.set_after(5, ["X1", "X2"])
    method, path, body = api_stub.calls[0]
    assert method == "PUT" and path.endswith("/5/after") and body == {"after": ["X1", "X2"]}


# ===========================================================================
# decisions
# ===========================================================================
def test_ask_posts_options(store):
    cs, api_stub = store
    cs.ask(5, options=[Option(key="1", label="Yes", body="")], text="pick one", by="agent · x")
    _method, path, body = api_stub.calls[0]
    assert path.endswith("/5/ask")
    assert body["options"] == [{"key": "1", "label": "Yes", "body": ""}]
    assert body["author"] == "agent · x"


def test_answer_posts_choice_and_answers(store):
    cs, api_stub = store
    cs.answer(5, choice="1", note_text="going with A", by="owner", answers={"Q1": "a"})
    _method, path, body = api_stub.calls[0]
    assert path.endswith("/5/answer")
    assert body["choice"] == "1" and body["answers"] == {"Q1": "a"} and body["author"] == "owner"


def test_deliver_reads_the_node(store):
    cs, api_stub = store
    cs.deliver(5)
    method, path, _body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/5/read")


# ===========================================================================
# assignment / milestone / flag
# ===========================================================================
def test_assign_maps_human_to_me_and_none_to_ai(store):
    cs, api_stub = store
    cs.assign(5, HUMAN)
    cs.assign(5, None)
    assert api_stub.calls[0][2] == {"to": "me"}
    assert api_stub.calls[1][2] == {"to": "ai"}


def test_set_milestone_on_assigns_to_milestone(store):
    cs, api_stub = store
    cs.set_milestone(5, True)
    assert api_stub.calls[0][2] == {"to": "milestone"}


def test_set_milestone_off_refuses(store):
    cs, _api = store
    with pytest.raises(task_cloud.CloudNotAvailable):
        cs.set_milestone(5, False)


def test_flag_important_refuses_but_flag_milestone_works(store):
    cs, api_stub = store
    with pytest.raises(task_cloud.CloudNotAvailable):
        cs.flag(5, important=True)
    cs.flag(5, milestone=True)
    assert api_stub.calls[-1][2] == {"to": "milestone"}


# ===========================================================================
# lifecycle (the generic verb route)
# ===========================================================================
@pytest.mark.parametrize(("method_name", "verb", "call_args", "expect_body"), [
    ("start", "start", (), {}),
    ("complete", "done", ("some note", "agent · x"), {"note": "some note", "author": "agent · x"}),
    ("close", "close", ("not needed",), {"reason": "not needed"}),
    ("dismiss", "archive", ("agent · x",), {"author": "agent · x"}),
    ("unarchive", "unarchive", (), {}),
    ("retitle", "retitle", ("New title", "agent · x"), {"title": "New title", "author": "agent · x"}),
    ("undefer", "undefer", ("agent · x",), {"author": "agent · x"}),
])
def test_simple_lifecycle_verbs_hit_the_generic_verb_route(store, method_name, verb, call_args, expect_body):
    cs, api_stub = store
    getattr(cs, method_name)(5, *call_args)
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith(f"/5/{verb}")
    assert body == expect_body


def test_move_to_root_sends_a_null_parent(store):
    cs, api_stub = store
    cs.move(5, root=True)
    assert api_stub.calls[0][2] == {}  # parent=None is dropped, which the server reads as root


def test_move_to_a_parent_sends_it(store):
    cs, api_stub = store
    cs.move(5, parent="X1")
    assert api_stub.calls[0][2] == {"parent": "X1"}


def test_supersede_resolves_the_new_ref_then_posts_by(store):
    cs, api_stub = store
    api_stub._responses = [_node(id=9, ref="X9")]
    _old, new = cs.supersede(5, 9, "agent · x")
    assert new is None
    method, path, body = api_stub.calls[-1]
    assert method == "POST" and path.endswith("/5/supersede")
    assert body == {"by": "X9", "author": "agent · x"}


def test_claim_returns_already_true_on_a_409(store):
    from formist_cli.api import ApiError

    cs, api_stub = store

    def _boom_then_resolve(*a, **k):
        raise ApiError("already-claimed", status=409)

    api_stub.post = _boom_then_resolve
    api_stub._responses = [_node(id=5, claimed_by="someone")]
    node, already = cs.claim(5, "agent · x")
    assert already is True
    assert node.id == 5


def test_claim_reraises_a_non_409_error(store):
    from formist_cli.api import ApiError

    cs, api_stub = store

    def _boom(*a, **k):
        raise ApiError("server error", status=500)

    api_stub.post = _boom
    with pytest.raises(ApiError):
        cs.claim(5, "agent · x")


def test_hold_and_unhold(store):
    cs, api_stub = store
    cs.hold(5, "the vendor", "agent · x", until="2026-12-01T00:00:00+08:00")
    cs.unhold(5, "agent · x")
    assert api_stub.calls[0][1].endswith("/5/wait")
    assert api_stub.calls[0][2] == {"on": "the vendor", "until": "2026-12-01T00:00:00+08:00", "author": "agent · x"}
    assert api_stub.calls[1][1].endswith("/5/undefer")


# ===========================================================================
# defer: the owner-consent rule is enforced client-side (no server route does)
# ===========================================================================
def test_defer_needs_owner_said_or_human(store):
    cs, _api = store
    with pytest.raises(Exception, match="owner's word"):
        cs.defer(5, "agent · x", until="2026-12-01T00:00:00+08:00")


def test_defer_with_owner_said_goes_through(store):
    cs, api_stub = store
    cs.defer(5, "agent · x", until="2026-12-01T00:00:00+08:00", owner_said="next week")
    assert api_stub.calls[0][1].endswith("/5/defer")


def test_defer_as_human_needs_no_owner_said(store):
    cs, api_stub = store
    cs.defer(5, "owner", role="human", until="2026-12-01T00:00:00+08:00")
    assert api_stub.calls[0][1].endswith("/5/defer")


# ===========================================================================
# not (yet) available — each names a reason, not a bare "no"
# ===========================================================================
@pytest.mark.parametrize("call", [
    lambda cs: cs.show("open"),
    lambda cs: cs.arrange([1, 2]),
    lambda cs: cs.unarrange(1),
    lambda cs: cs.attach(),
    lambda cs: cs.approved_check(),
    lambda cs: cs.check(),
    lambda cs: cs.edit_note(5, 2, text="x"),
])
def test_not_yet_available_verbs_refuse_clearly(store, call):
    cs, _api = store
    with pytest.raises(task_cloud.CloudNotAvailable):
        call(cs)


def test_expand_posts_the_full_brief(store):
    cs, api_stub = store
    cs.expand(5, "A full plan", title="Better title")
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/5/expand")
    assert body == {"body": "A full plan", "title": "Better title"}


# ===========================================================================
# agent sessions + the monitor SSE stream
# ===========================================================================
def test_register_session_posts_to_agent_sessions(store):
    cs, api_stub = store
    cs.register_session("sess-1", author="agent · x", current="X5")
    method, path, body = api_stub.calls[0]
    assert method == "POST" and path.endswith("/agent-sessions")
    assert body["client_session"] == "sess-1" and body["author"] == "agent · x" and body["current"] == "X5"


def test_watch_events_yields_monitor_lines_from_sse(store):
    cs, api_stub = store
    api_stub._responses = [[
        'event: line\ndata: DECS-UPDATED X5: something changed\n\n',
        "event: heartbeat\ndata: \n\n",
        "event: line\ndata: DECS-DONE nothing of yours is open\n\n",
    ]]
    lines = list(cs.watch_events(mine="agent · x"))
    assert lines == [
        "DECS-UPDATED X5: something changed",
        "DECS-DONE nothing of yours is open",
    ]
    assert api_stub.calls[0][0] == "STREAM"
    assert api_stub.calls[0][2] == {"mine": "agent · x"}


def test_watch_events_reheartbeats_the_session_periodically(store, monkeypatch):
    """A long-lived monitor only registering once at start would have its
    agent_session row read as gone after SESSION_LIVE_SECONDS (180s) even
    while still actively listening (found while wiring the C1053/C1139
    plugin's auto-start monitor) — it must re-POST /agent-sessions on its
    own, not rely on the caller to notice."""
    cs, api_stub = store
    monkeypatch.setattr(task_cloud.CloudStore, "SESSION_HEARTBEAT_EVERY", 0)
    api_stub._responses = [[
        "event: line\ndata: DECS-UPDATED X5: x\n\n",
        "event: heartbeat\ndata: \n\n",
        "event: line\ndata: DECS-DONE nothing of yours is open\n\n",
    ]]
    lines = list(cs.watch_events(mine="agent · x", client_session="sess-1", author="agent · x"))
    assert lines == ["DECS-UPDATED X5: x", "DECS-DONE nothing of yours is open"]
    reheartbeats = [c for c in api_stub.calls if c[0] == "POST" and c[1].endswith("/agent-sessions")]
    # 3 SSE frames, threshold always due (0) -> one re-heartbeat check per frame.
    assert len(reheartbeats) >= 1
    assert all(c[2]["client_session"] == "sess-1" for c in reheartbeats)


def test_watch_events_without_a_client_session_never_reheartbeats(store, monkeypatch):
    """No ``client_session`` (the plain unit test above it, and anyone
    calling ``watch_events`` without opting in) means no re-registration
    at all — the original behavior, unchanged."""
    cs, api_stub = store
    monkeypatch.setattr(task_cloud.CloudStore, "SESSION_HEARTBEAT_EVERY", 0)
    api_stub._responses = [["event: line\ndata: DECS-DONE nothing of yours is open\n\n"]]
    list(cs.watch_events(mine="agent · x"))
    assert not any(c[0] == "POST" and c[1].endswith("/agent-sessions") for c in api_stub.calls)


# ===========================================================================
# cloud_store(): the context.resolve wiring, and why for_write=False
# ===========================================================================
def test_cloud_store_resolves_context_with_for_write_false(monkeypatch):
    captured = {}

    def fake_resolve(args, **kw):
        captured.update(kw)
        return context.Context(target=TARGET, space=SPACE, token="t", space_source="test")

    # `cloud_store` imports `context` lazily inside its own body (it is the
    # one place `fm task` resolves a destination, not a hot path worth
    # avoiding the import cost for) — patching the module's own `resolve`
    # attribute reaches it regardless of when that import runs.
    monkeypatch.setattr(context, "resolve", fake_resolve)
    cs = task_cloud.cloud_store(types.SimpleNamespace(host=None, space=SPACE))
    assert captured["for_write"] is False
    assert captured["need_space"] is True
    assert captured["need_token"] is True
    assert cs.project_id == SPACE
