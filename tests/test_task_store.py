"""``LocalStore`` lifecycle tests — every verb exercised directly against a
throwaway ``decisions/`` directory, no `nod` binary required.

Byte-compatibility with `nod`'s own output is covered separately
(``test_task_parse_serialize.py``'s fixtures, ``test_task_nod_interop.py``'s
cross-tool subprocess checks); these tests check the ENGINE's own
behavior: state transitions, queueing, archiving, and the refusals each
verb is supposed to raise.
"""

from __future__ import annotations

import pytest

from formist_cli import task_paths, task_store
from formist_cli.task_model import Option


@pytest.fixture
def store(tmp_path):
    layout = task_paths.init_layout(str(tmp_path))
    return task_store.LocalStore(layout)


def test_add_task_then_start_then_progress_then_done(store):
    node = store.create("A plain task", "agent · doing it", task=True)
    assert node.state == "not_started"
    assert node.kind == "task"

    node = store.start(node.id)
    assert node.state == "in_progress"

    node = store.progress(node.id, 50, "halfway")
    assert node.progress == 50 and node.progress_msg == "halfway"

    node = store.set_steps(node.id, ["a", "b", "c"])
    node = store.mark_step(node.id, 2, note_text="did a and b", author="agent")
    assert [s.done for s in node.steps] == [True, True, False]
    assert node.progress == 67

    node = store.complete(node.id, note_text="shipped")
    assert node.state == "completed"
    assert node.archived is True
    assert "archived" in node.file


def test_add_without_options_defaults_to_acceptance(store):
    node = store.create("Needs a look", "agent · author")
    assert node.kind == "acceptance"
    assert [o.key for o in node.options] == ["1"]


def test_add_fyi_writes_the_kind_marker(store):
    """Regression: `kind: fyi` was never written to the file (only the
    in-memory pre-reread Node defaulted to kind="task"), so a freshly
    created fyi node read back as kind="task" instead of "fyi" until
    this was fixed (found while testing `show fyi`/C1137)."""
    node = store.create("Just letting you know", "agent · author", fyi=True)
    assert node.kind == "fyi"
    with open(node.file, encoding="utf-8") as fh:
        assert "kind: fyi" in fh.read()


def test_decision_answer_queues_and_deliver_dequeues(store):
    node = store.create(
        "Pick one", "agent · author", options=[Option(key="1", label="A"), Option(key="2", label="B")], default="1",
    )
    assert node.state == "needs_decision"

    answered = store.answer(node.id, choice="1", note_text="go with A", by="owner", role="human")
    assert answered.state == "decided"
    assert answered.queued is True
    assert answered.answer.choice == "1"
    assert answered.answer.by == "owner"

    from formist_cli.task_queue import read_queue

    assert node.id in read_queue(store.layout)

    delivered = store.deliver(node.id)
    assert delivered.state == "in_progress"
    assert delivered.queued is False
    assert node.id not in read_queue(store.layout)


def test_start_refuses_unanswered_decision(store):
    node = store.create("Pick one", "agent · author", options=[Option(key="1", label="A")])
    with pytest.raises(task_store.StoreError):
        store.start(node.id)


def test_close_requires_a_reason(store):
    node = store.create("Something", "agent · author", task=True)
    with pytest.raises(task_store.StoreError):
        store.close(node.id, "")
    result = store.close(node.id, "obsolete")
    assert result.state == "closed"
    assert result.close_reason == "obsolete"
    assert result.archived is True


def test_defer_and_undefer(store):
    node = store.create("Something", "agent · author", task=True)
    deferred = store.defer(node.id, "owner", until="2026-12-25T23:59:59+08:00")
    assert deferred.deferred_at is not None
    assert deferred.deferred_until == "2026-12-25T23:59:59+08:00"

    undeferred = store.undefer(node.id, "owner")
    assert undeferred.deferred_at is None


def test_defer_needs_owner_consent_unless_human_or_owner_said(store):
    """An agent may only put work off on the owner's own word (verified
    against real nod: a plain agent defer with neither is refused)."""
    node = store.create("Something", "agent · author", task=True)
    with pytest.raises(task_store.StoreError):
        store.defer(node.id, "agent · author", role="agent", until="2026-12-25T23:59:59+08:00")

    said = store.defer(
        node.id, "agent · author", role="agent", until="2026-12-25T23:59:59+08:00",
        owner_said="push it to the 25th",
    )
    assert said.deferred_until == "2026-12-25T23:59:59+08:00"

    node2 = store.create("Something else", "agent · author", task=True)
    human = store.defer(node2.id, "owner", role="human", until="2026-12-25T23:59:59+08:00")
    assert human.deferred_until == "2026-12-25T23:59:59+08:00"


def test_hold_and_unhold_active_wait(store):
    node = store.create("Something", "agent · author", task=True)
    held = store.hold(node.id, "Carol to confirm", "agent · author", until="2026-12-01T00:00:00+08:00")
    assert held.state == "waiting"
    assert held.waiting_on == "Carol to confirm"

    released = store.unhold(node.id, "agent · author")
    assert released.state == "not_started"
    assert released.waiting_on is None


def test_assign_and_milestone(store):
    node = store.create("Something", "agent · author", task=True)
    assigned = store.assign(node.id, "human")
    assert assigned.assignee == "human"

    back_to_ai = store.assign(node.id, None)
    assert back_to_ai.assignee is None

    milestone = store.set_milestone(node.id, True)
    assert milestone.milestone is True
    assert milestone.assignee is None


def test_flag_milestone_needs_due(store):
    node = store.create("Something", "agent · author", task=True)
    with pytest.raises(task_store.StoreError):
        store.flag(node.id, milestone=True)
    flagged = store.flag(node.id, milestone=True, due="2026-12-25T23:59:59+08:00")
    assert flagged.milestone is True


def test_order_arrange_and_unarrange(store):
    a = store.create("A", "agent · author", task=True)
    b = store.create("B", "agent · author", task=True)
    c = store.create("C", "agent · author", task=True)

    arranged = store.arrange([b.id, a.id, c.id])
    by_id = {n.id: n for n in arranged}
    assert by_id[b.id].order == -3
    assert by_id[a.id].order == -2
    assert by_id[c.id].order == -1

    # Re-arranging to the identical order is a no-op (no spurious write).
    before = store.get(a.id).updated_at
    store.arrange([b.id, a.id, c.id])
    assert store.get(a.id).updated_at == before

    cleared = store.unarrange(a.id)
    assert cleared.order is None
    # Clearing twice is a no-op, not an error.
    store.unarrange(a.id)


def test_after_and_is_startable(store):
    blocker = store.create("Blocker", "agent · author", task=True)
    dependent = store.create("Dependent", "agent · author", task=True, after=[str(blocker.id)])
    with pytest.raises(task_store.StoreError):
        store.start(dependent.id)

    store.complete(blocker.id)
    started = store.start(dependent.id)
    assert started.state == "in_progress"


def test_claim_is_idempotent_for_the_same_agent(store):
    node = store.create("A brief", "owner", intake=True, task=True)
    claimed, already = store.claim(node.id, "lane-a · doing it")
    assert already is False
    assert claimed.claimed_by == "lane-a · doing it"

    claimed_again, already2 = store.claim(node.id, "lane-a · doing it")
    assert already2 is True

    with pytest.raises(task_store.ClaimedError):
        store.claim(node.id, "lane-b · someone else")


def test_expand_moves_description_under_original(store):
    node = store.create("A brief", "owner", intake=True, task=True, body="fix the thing")
    expanded = store.expand(node.id, "Full plan: do X then Y.")
    assert "Full plan: do X then Y." in expanded.body
    assert "原话" in expanded.body
    assert "fix the thing" in expanded.body


def test_note_dedup_within_ten_minutes(store):
    node = store.create("Something", "agent · author", task=True)
    once = store.note(node.id, "same text", "owner", role="human")
    twice = store.note(node.id, "same text", "owner", role="human")
    assert len(twice.notes) == len(once.notes)


def test_note_unarchives_a_human_reply(store):
    node = store.create("Something", "agent · author")
    answered = store.answer(node.id, choice="1", by="owner", role="human")
    done = store.complete(answered.id)
    assert done.archived is True

    replied = store.note(done.id, "actually, wait", "owner", role="human")
    assert replied.archived is False
    assert replied.state == "decided"


def test_retitle_checks_width_and_records_old_title(store):
    node = store.create("Short title", "agent · author", task=True)
    too_long = "x" * 40
    with pytest.raises(Exception):
        store.retitle(node.id, too_long, "owner")
    retitled = store.retitle(node.id, "New title", "owner")
    assert retitled.title == "New title"
    assert "Short title" in retitled.body


def test_move_reparents_with_subtree_and_relinks_attachments(store, tmp_path):
    root1 = store.create("Root one", "agent · a", task=True)
    root2 = store.create("Root two", "agent · a", task=True)
    child = store.create("Child", "agent · a", task=True, parent=str(root1.id))
    grandchild = store.create("Grandchild", "agent · a", task=True, parent=str(child.id))

    sample = tmp_path / "sample.txt"
    sample.write_text("hi")
    attached = store.attach(child.id, [str(sample)], "agent · a")
    assert attached.attachments[0].target == "../../attachments/3/sample.txt"

    moved = store.move(child.id, parent=str(root2.id))
    assert moved.parent == root2.id
    assert "2-root-two" in moved.file
    # Same depth (both roots are direct children of active/), so the
    # relative attachment path is unchanged.
    assert moved.attachments[0].target == "../../attachments/3/sample.txt"

    all_nodes = {n.id: n for n in store.all_nodes()}
    assert all_nodes[grandchild.id].parent == child.id
    assert "2-root-two" in all_nodes[grandchild.id].file

    rooted = store.move(child.id, root=True)
    assert rooted.parent is None
    # Shallower now: one less directory level, so the link gains a `../`.
    assert rooted.attachments[0].target == "../attachments/3/sample.txt"


def test_move_refuses_a_cycle(store):
    root = store.create("Root", "agent · a", task=True)
    child = store.create("Child", "agent · a", task=True, parent=str(root.id))
    grandchild = store.create("Grandchild", "agent · a", task=True, parent=str(child.id))
    with pytest.raises(task_store.StoreError):
        store.move(root.id, parent=str(grandchild.id))
    with pytest.raises(task_store.StoreError):
        store.move(root.id, parent=str(root.id))


def test_move_needs_exactly_one_target(store):
    root = store.create("Root", "agent · a", task=True)
    child = store.create("Child", "agent · a", task=True)
    with pytest.raises(task_store.StoreError):
        store.move(child.id, parent=str(root.id), root=True)
    with pytest.raises(task_store.StoreError):
        store.move(child.id)


def test_supersede_links_both_sides(store):
    old = store.create("Old approach", "agent · author", task=True)
    new = store.create("New approach", "agent · author", task=True)
    old_result, new_result = store.supersede(old.id, new.id, "agent · author")
    assert old_result.state == "closed"
    assert old_result.close_reason == "superseded"
    assert old_result.superseded_by == new.id
    assert new_result.supersedes == [old.id]


def test_tree_and_children(store):
    parent = store.create("Parent", "agent · author", task=True)
    child = store.create("Child", "agent · author", task=True, parent=str(parent.id))
    nodes = store.all_nodes()
    by_id = {n.id: n for n in nodes}
    assert by_id[parent.id].children == [child.id]
    assert by_id[child.id].parent == parent.id


def test_search_matches_title_and_body(store):
    store.create("Findable title", "agent · author", task=True, body="unique body text xyz")
    store.create("Unrelated", "agent · author", task=True)
    hits = store.search(["unique", "xyz"])
    assert len(hits) == 1
    assert hits[0].title == "Findable title"


def test_show_open_scope_is_needs_decision(store):
    task = store.create("A task", "agent · author", task=True)
    decision = store.create("Pick one", "agent · author", options=[Option(key="1", label="A")])
    shown = {n.id for n in store.show("open")}
    assert decision.id in shown
    assert task.id not in shown


def test_show_active_scope_excludes_archived(store):
    task = store.create("A task", "agent · author", task=True)
    done = store.create("Done already", "agent · author", task=True)
    store.complete(done.id)
    shown = {n.id for n in store.show("active")}
    assert task.id in shown
    assert done.id not in shown


def test_available_view_excludes_milestones(store):
    """`available` is a BENCH VIEW (the filter DSL), a different
    vocabulary from `show`'s scopes — see task_views.apply_show_scope
    vs. apply_view."""
    from formist_cli import task_views

    task = store.create("A task", "agent · author", task=True)
    milestone = store.create("A milestone", "agent · author", task=True, milestone=True, due="2026-12-25T23:59:59+08:00")
    nodes = store.all_nodes()
    shown = {n.id for n in task_views.apply_view(nodes, "available")}
    assert task.id in shown
    assert milestone.id not in shown


def test_attach_and_approve(store, tmp_path):
    node = store.create("Needs acceptance", "agent · author")
    sample = tmp_path / "sample.txt"
    sample.write_text("hello")
    attached = store.attach(node.id, [str(sample)], "agent · author", approval_content=True)
    assert len(attached.approvals) == 1

    approved = store.answer(attached.id, choice="1", by="owner", role="human")
    assert approved.approvals[0].approved_hash == approved.approvals[0].hash

    attachment_path = tmp_path / "decisions" / "attachments" / str(node.id) / "sample.txt"
    assert attachment_path.is_file()
    code, _ = store.approved_check(approved.id, str(attachment_path))
    assert code == 0

    attachment_path.write_text("changed")
    code, _ = store.approved_check(approved.id, str(attachment_path))
    assert code == 1


def test_letter_defaults_to_x_and_cross_refs_resolve(store):
    """Regression (found while testing `check`/C1140): ``letter()`` used
    to return ``None`` when no ``decs.json`` letter was configured — the
    common case. ``format_ref`` masks this with its own ``letter or "X"``
    fallback at render time, but ``find_ref``'s raw ``i.letter ==
    r.letter`` comparison does not, so every ``after``/``supersedes``
    cross-reference silently failed to resolve (``dependencies_of``
    fell back to the unresolved ref string) whenever a project had no
    explicit letter — which is every project that never ran `fm task
    config letter`. Real nod's `letterOf` never returns null."""
    from formist_cli import task_status

    assert store.letter() == "X"

    a = store.create("A", "agent · author", task=True)
    b = store.create("B", "agent · author", task=True, after=["X1"])
    nodes = store.all_nodes()
    deps = task_status.dependencies_of(next(n for n in nodes if n.id == b.id), nodes)
    assert deps == [next(n for n in nodes if n.id == a.id)]


def test_source_qualified_ref_is_limited_to_this_local_project_and_hints_stale_letter(store):
    """Finding (C1125): LocalStore previously ignored a source qualifier,
    so `another-project:C1` could mutate local node 1.  A source now gates
    lookup, while its letter remains display-only and the returned node's
    canonical letter is the correction for a stale spelling."""
    node = store.create("A", "agent · author", task=True)
    assert store.resolve(f"{store.project_key()}:Z{node.id}").id == node.id
    assert store.resolve(f"{store.project_key()}:Z{node.id}").letter == store.letter()
    with pytest.raises(task_store.NotFoundError, match="different source"):
        store.resolve(f"another-project:C{node.id}")
