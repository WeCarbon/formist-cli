"""C1120: one golden workflow corpus against LocalStore and installed Noddit."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from formist_cli import task_monitor, task_paths, task_store, workflow_corpus
from formist_cli.task_model import Option


def _case(corpus, id_):
    return next(case for case in corpus["cases"] if case["id"] == id_)


def _line(lines, prefix):
    return next(line for line in lines if line.startswith(prefix))


def test_local_store_runs_the_shared_workflow_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("NODDIT_LANG", "zh-Hans")
    corpus = workflow_corpus.load()
    store = task_store.LocalStore(task_paths.init_layout(str(tmp_path)))
    since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

    decision = _case(corpus, "ask_answer_read")
    node = store.create(decision["title"], corpus["agent"], task=True)
    states = [node.state]
    node = store.start(node.id)
    states.append(node.state)
    node = store.ask(
        node.id,
        options=[Option(key=decision["option"]["key"], label=decision["option"]["label"])],
        by=corpus["agent"],
    )
    states.append(node.state)
    node = store.answer(
        node.id,
        choice=decision["option"]["key"],
        note_text=decision["answer_note"],
        by="owner",
        role="human",
    )
    states.append(node.state)
    assert node.queued is True
    lines, _ = task_monitor.run_pass(store, corpus["agent"], since)
    expected = workflow_corpus.render(decision["monitor"], ref="X1", cli="fm task")
    assert workflow_corpus.without_cursor(_line(lines, "X1 负责人答复")) == expected
    node = store.deliver(node.id)
    states.append(node.state)
    assert node.queued is False
    assert states == decision["states"]

    brief = _case(corpus, "brief_claim_expand_ask")
    node = store.create(
        brief["title"], "owner", body=brief["original"], intake=True, task=True
    )
    states = [node.state]
    lines, _ = task_monitor.run_pass(store, None, since)
    expected = workflow_corpus.render(brief["monitor"], ref="X2", cli="fm task")
    assert workflow_corpus.without_cursor(_line(lines, "DECS-BRIEF X2")) == expected
    node, already = store.claim(node.id, corpus["agent"])
    assert already is False
    states.append(node.state)
    assert node.owner_agent == corpus["agent"]
    node = store.expand(node.id, brief["expanded"])
    states.append(node.state)
    assert brief["expanded"] in node.body and brief["original"] in node.body
    node = store.ask(node.id, options=[Option(key="1", label="Start")], by=corpus["agent"])
    states.append(node.state)
    assert states == brief["states"]

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    reminder = _case(corpus, "reminder_due")
    waiting = _case(corpus, "wait_check_back")
    deferred = _case(corpus, "defer_due")
    reminder_node = store.create(
        reminder["title"], corpus["agent"], task=True, remind_at=past
    )
    waiting_node = store.create(waiting["title"], corpus["agent"], task=True)
    store.hold(waiting_node.id, waiting["waiting_on"], corpus["agent"], until=past)
    deferred_node = store.create(deferred["title"], corpus["agent"], task=True)
    store.defer(deferred_node.id, "owner", role="human", until=past)
    lines, _ = task_monitor.run_pass(store, None, since)
    for ref, case in (("X3", reminder), ("X4", waiting), ("X5", deferred)):
        expected = workflow_corpus.render(case["monitor"], ref=ref, cli="fm task")
        assert _line(lines, expected.split(" ", 1)[0] + " " + ref) == expected
    assert store.get(reminder_node.id).state == "not_started"
    assert store.get(waiting_node.id).state == waiting["state"]
    returned = store.get(deferred_node.id)
    assert returned.state == deferred["state"] and returned.deferred_at is None


NOD = shutil.which("nod")


@pytest.mark.skipif(NOD is None, reason="installed Noddit CLI is not on PATH")
def test_installed_nod_public_output_matches_the_shared_monitor_goldens(tmp_path):
    """Noddit stays a black box: only its installed CLI and public output are used."""
    corpus = workflow_corpus.load()
    assert NOD is not None
    nod_bin = NOD
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {
        **os.environ,
        "NODDIT_HOME": str(home),
        "NODDIT_AUTHOR": corpus["agent"],
        "NODDIT_WORKTREE": str(project),
    }

    def nod(*args: str):
        result = subprocess.run(
            [nod_bin, *args, "--dir", str(project)],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    nod("init")
    nod("config", "letter", "X")
    nod("config", "lang", "zh-Hans")
    decision = _case(corpus, "ask_answer_read")
    nod("add", decision["title"], "--task", "--author", corpus["agent"])
    nod(
        "ask",
        "X1",
        "-o",
        f'{decision["option"]["key"]}={decision["option"]["label"]}',
        "--author",
        corpus["agent"],
    )

    proc = subprocess.Popen(
        [
            nod_bin,
            "monitor",
            "--all",
            "--dir",
            str(project),
            "--interval",
            "50",
            "--idle-exit",
            "30s",
            "--no-heartbeat",
            "--no-self-update",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        first = proc.stdout.readline()
        assert "monitor" in first
        nod("push", "X1", decision["option"]["key"], "--human", "--note", decision["answer_note"])
        brief = _case(corpus, "brief_claim_expand_ask")
        nod("add", brief["title"], "--brief", "--author", "owner")
        reminder = _case(corpus, "reminder_due")
        nod("add", reminder["title"], "--remind", "2026-01-01T00:00:00+08:00", "--author", corpus["agent"])

        found: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(found) < 3:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline().strip()
            if line.startswith(("X1 负责人答复", "DECS-BRIEF X2", "NODDIT-REMIND X3")):
                found.append(workflow_corpus.without_cursor(line))
        assert workflow_corpus.render(decision["monitor"], ref="X1", cli="nod") in found
        assert workflow_corpus.render(brief["monitor"], ref="X2", cli="nod") in found
        assert workflow_corpus.render(reminder["monitor"], ref="X3", cli="nod") in found
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # The remaining two cases have day-granularity clocks. Public JSON proves
    # Noddit stores their state; LocalStore and the server drive the due edge.
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    waiting = _case(corpus, "wait_check_back")
    nod("add", waiting["title"], "--task", "--author", corpus["agent"])
    nod("defer", "X4", "--on", waiting["waiting_on"], "--until", tomorrow, "--owner-said", "wait")
    waiting_json = json.loads(nod("get", "X4", "--json"))
    assert waiting_json["status"] == waiting["state"] and waiting_json["waiting_on"] == waiting["waiting_on"]
    deferred = _case(corpus, "defer_due")
    nod("add", deferred["title"], "--task", "--author", corpus["agent"])
    nod("defer", "X5", "--until", tomorrow, "--owner-said", "later")
    deferred_json = json.loads(nod("get", "X5", "--json"))
    assert deferred_json["status"] == deferred["state"] and deferred_json["deferred_until"] is not None
