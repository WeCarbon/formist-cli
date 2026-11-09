"""The filter DSL (``task_filter``) and views (``task_views``) — the 6
built-in bench views, `nod show`'s SEPARATE scope vocabulary, and
``$NODDIT_HOME/views.json`` storage shared with `nod`."""

from __future__ import annotations

import pytest

from formist_cli import task_filter, task_paths, task_store, task_views
from formist_cli.task_model import Option


def test_parse_filter_round_trips_through_filter_text():
    cases = [
        "state:in_progress,waiting",
        "assignee:human",
        "is:startable -is:deferred",
        'title:"release notes"',
        "state:needs_decision -is:milestone or assignee:human state:not_started is:startable",
        "tag:external",
    ]
    for text in cases:
        f = task_filter.parse_filter(text)
        assert task_filter.filter_text(f) == text


def test_parse_filter_rejects_unknown_field_and_is_value():
    with pytest.raises(task_filter.FilterError):
        task_filter.parse_filter("bogus:1")
    with pytest.raises(task_filter.FilterError):
        task_filter.parse_filter("is:not-a-real-value")


@pytest.fixture
def store(tmp_path):
    layout = task_paths.init_layout(str(tmp_path))
    return task_store.LocalStore(layout)


def test_available_view_excludes_milestones_but_not_other_open_work(store):
    task = store.create("A task", "agent · a", task=True)
    milestone = store.create("A milestone", "agent · a", task=True, milestone=True, due="2026-12-25T23:59:59+08:00")
    nodes = store.all_nodes()
    shown = {n.id for n in task_views.apply_view(nodes, "available")}
    assert task.id in shown
    assert milestone.id not in shown


def test_decide_view_matches_needs_decision_and_human_todo(store):
    decision = store.create("Pick one", "agent · a", options=[Option(key="1", label="A")])
    human_todo = store.create("Call the vendor", "owner", task=True, assignee="human")
    unrelated = store.create("Just working", "agent · a", task=True)
    nodes = store.all_nodes()
    shown = {n.id for n in task_views.apply_view(nodes, "decide")}
    assert decision.id in shown
    assert human_todo.id in shown
    assert unrelated.id not in shown


def test_external_view_matches_the_external_tag(store):
    tagged = store.create("Sent out", "agent · a", task=True)
    store.set_tags(tagged.id, add=["external"])
    untagged = store.create("Internal", "agent · a", task=True)
    nodes = store.all_nodes()
    shown = {n.id for n in task_views.apply_view(nodes, "external")}
    assert tagged.id in shown
    assert untagged.id not in shown


def test_show_scope_open_vs_active_vs_archived(store):
    decision = store.create("Pick one", "agent · a", options=[Option(key="1", label="A")])
    plain = store.create("Plain task", "agent · a", task=True)
    done = store.create("Finished", "agent · a", task=True)
    store.complete(done.id)
    fyi = store.create("Just an fyi", "agent · a", fyi=True)

    assert {n.id for n in store.show("open")} == {decision.id}
    assert {n.id for n in store.show("active")} == {decision.id, plain.id}
    # An fyi is always archived immediately but never shows under "archived"
    # (verified against real nod: "archived" means done real work).
    assert {n.id for n in store.show("archived")} == {done.id}
    assert {n.id for n in store.show("fyi")} == {fyi.id}
    assert {n.id for n in store.show("all")} == {decision.id, plain.id, done.id, fyi.id}


def test_show_scope_due_and_remind(store):
    with_due = store.create("Has a due date", "agent · a", task=True, due="2026-12-25T23:59:59+08:00")
    with_remind = store.create("Has a reminder", "agent · a", task=True, remind_at="2026-12-25T09:00:00+08:00")
    plain = store.create("Neither", "agent · a", task=True)
    assert {n.id for n in store.show("due")} == {with_due.id}
    assert {n.id for n in store.show("remind")} == {with_remind.id}
    assert plain.id not in {n.id for n in store.show("due")}


def test_show_scope_rejects_a_view_id():
    """"available" is a bench VIEW, not a `show` SCOPE — the two
    vocabularies overlap on some words ("active"/"all") but are not
    interchangeable (verified: real `nod show available` is refused)."""
    with pytest.raises(ValueError):
        task_views.apply_show_scope([], "available")


def test_views_storage_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path))
    custom = task_views.ViewDef(
        id="my-view", layout="list",
        filter=task_filter.parse_filter("state:in_progress"), sort=["bench"],
    )
    saved = [*task_views.BUILTIN_VIEWS, custom]
    task_views.save_views(saved)

    loaded = task_views.load_views()
    assert [v.id for v in loaded] == [*task_views.BUILTIN_IDS, "my-view"]
    mine = next(v for v in loaded if v.id == "my-view")
    # The filter stored as the parsed object form, not text — verified
    # against a real `nod view add`'s own views.json.
    assert mine.filter == {"field": "state", "value": ["in_progress"]}


def test_reset_views_restores_built_ins(tmp_path, monkeypatch):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path))
    task_views.save_views([*task_views.BUILTIN_VIEWS, task_views.ViewDef(id="extra", layout="list")])
    reset = task_views.reset_views()
    assert [v.id for v in reset] == task_views.BUILTIN_IDS


def test_views_json_path_defaults_under_decs_home(monkeypatch):
    monkeypatch.delenv("NODDIT_HOME", raising=False)
    monkeypatch.delenv("DECS_HOME", raising=False)
    monkeypatch.delenv("NODDIT_VIEWS", raising=False)
    monkeypatch.delenv("DECS_VIEWS", raising=False)
    assert task_views.views_path().endswith("/.decs/views.json")
