"""``fm task monitor``/``wait``/``watch`` — the C1138 pieces: DECS-DONE,
the three clock sweeps, cursor persistence across separate invocations
(the "re-arm the exact same command" pattern this whole feature exists
for), and the node-event line format."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from formist_cli import task_monitor, task_paths, task_store, tasks
from formist_cli.task_model import Option


@pytest.fixture
def store(tmp_path):
    layout = task_paths.init_layout(str(tmp_path))
    return task_store.LocalStore(layout)


def test_same_agent_matches_name_before_dot():
    assert task_monitor.same_agent("lane · doing stuff", "lane")
    assert task_monitor.same_agent("Lane · x", "  lane  ")
    assert not task_monitor.same_agent("lane · x", "other-lane")
    assert not task_monitor.same_agent(None, "lane")


def test_has_open_work_for_true_while_a_node_is_open(store):
    store.create("Something", "lane · doing stuff", task=True)
    nodes = store.all_nodes()
    assert task_monitor.has_open_work_for(nodes, "lane") is True


def test_has_open_work_for_false_once_everything_is_closed(store):
    n = store.create("Something", "lane · doing stuff", task=True)
    store.complete(n.id)
    nodes = store.all_nodes()
    assert task_monitor.has_open_work_for(nodes, "lane") is False


def test_has_open_work_for_true_when_queued_even_if_terminal_elsewhere(store):
    n = store.create("Pick one", "lane · x", options=[Option(key="1", label="A")])
    store.answer(n.id, choice="1", by="owner", role="human")
    nodes = store.all_nodes()
    assert task_monitor.has_open_work_for(nodes, "lane") is True


def test_has_open_work_for_true_for_unclaimed_brief_under_owned_node(store):
    parent = store.create("Parent", "lane · x", task=True)
    store.create("A brief", "owner", intake=True, task=True, parent=str(parent.id))
    nodes = store.all_nodes()
    assert task_monitor.has_open_work_for(nodes, "lane") is True


def test_run_pass_reports_a_human_answer_and_not_before(store, monkeypatch):
    monkeypatch.setenv("NODDIT_LANG", "en")
    n = store.create("Pick one", "lane · x", options=[Option(key="1", label="A"), Option(key="2", label="B")])
    early_since = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    lines, _since = task_monitor.run_pass(store, "lane", early_since)
    assert lines == []

    past_since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    store.answer(n.id, choice="1", note_text="go with A", by="owner", role="human")
    lines2, _since2 = task_monitor.run_pass(store, "lane", past_since)
    assert len(lines2) == 1
    assert "owner answered 1" in lines2[0]
    assert "go with A" in lines2[0]
    assert "fm task read" in lines2[0]


def test_run_pass_fires_decs_done_once_everything_closes(store):
    n = store.create("Something", "lane · x", task=True)
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    store.complete(n.id)
    lines, _ = task_monitor.run_pass(store, "lane", since)
    assert any(l.startswith("DECS-DONE") for l in lines)


def test_run_pass_never_fires_decs_done_without_an_author(store):
    n = store.create("Something", "lane · x", task=True)
    store.complete(n.id)
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    lines, _ = task_monitor.run_pass(store, None, since)
    assert not any(l.startswith("DECS-DONE") for l in lines)


def test_run_pass_reports_undeferred_reminded_and_checkback(store):
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    deferred = store.create("Deferred", "lane · x", task=True)
    store.defer(deferred.id, "owner", until=past)

    store.create("Remind me", "lane · x", task=True, remind_at=past)

    waiting = store.create("Waiting", "lane · x", task=True)
    store.hold(waiting.id, "Carol", "lane · x", until=past)

    lines, _ = task_monitor.run_pass(store, None, since)
    joined = "\n".join(lines)
    assert "NODDIT-UNDEFERRED" in joined and "Deferred" in joined
    assert "NODDIT-REMIND" in joined and "Remind me" in joined
    assert "NODDIT-CHECK-BACK" in joined and "Carol" in joined


def test_clock_sweep_notes_are_signed_nod_not_fm(store):
    """So a real `nod monitor` on the same files never double-fires the
    same clock event `fm` already fired (byte-compat requires sharing the
    clock-author constant)."""
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    n = store.create("Remind me", "lane · x", task=True, remind_at=past)
    store.fire_due_reminders()
    reread = store.get(n.id)
    assert reread.notes[-1].author == "nod"


def test_monitor_cli_persists_cursor_across_separate_invocations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_LANG", "en")
    monkeypatch.setenv("NODDIT_AUTHOR", "lane · x")
    tasks.main(["init", str(tmp_path)])
    tasks.main(["add", "Decide", "--option", "1=A", "--dir", str(tmp_path), "--json"])
    capsys.readouterr()

    # First call establishes the cursor; nothing has happened yet.
    code = tasks.main(["monitor", "--once", "--mine", "--dir", str(tmp_path)])
    assert code == 0
    assert capsys.readouterr().out == ""

    tasks.main(["push", "X1", "1", "--human", "--note", "go with A", "--dir", str(tmp_path)])
    capsys.readouterr()

    # A SEPARATE invocation (new process in real usage) must still see the
    # answer via the persisted cursor, not just "now" at this call's start.
    code = tasks.main(["monitor", "--once", "--mine", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "owner answered" in out


def test_wait_returns_immediately_when_queue_is_non_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "lane · x")
    tasks.main(["init", str(tmp_path)])
    tasks.main(["add", "Decide", "--option", "1=A", "--dir", str(tmp_path), "--json"])
    tasks.main(["push", "X1", "1", "--human", "--dir", str(tmp_path)])
    capsys.readouterr()

    code = tasks.main(["wait", "--dir", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert '"updates": true' in out
    assert "X1" in out


def test_wait_times_out_on_an_empty_queue(tmp_path, capsys):
    tasks.main(["init", str(tmp_path)])
    capsys.readouterr()
    code = tasks.main(["wait", "--dir", str(tmp_path), "--timeout", "1", "--json"])
    out = capsys.readouterr().out
    assert code == 2
    assert '"timed_out": true' in out
