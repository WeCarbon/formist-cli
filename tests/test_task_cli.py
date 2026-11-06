"""``fm task <verb>`` through its own standalone entry point
(``tasks.main``), covering argument parsing, mode resolution, and dispatch
— the layer ``test_task_store.py`` doesn't exercise.

Goes through ``tasks.main`` rather than ``cli.main`` for most tests here:
it's the exact same dispatch (``cli.main`` calls ``tasks.run`` directly
and shares ``tasks._task_user_errors()`` in its own ``USER_ERRORS``,
wired in once `fm task` was rebased onto ``feat/fm-slim``), just without
building the whole-of-`fm` parser for every test. See
``test_task_through_the_real_cli_main`` below for the one test that
drives the real ``cli.main(["task", ...])`` path end to end, proving the
wiring itself (not just the dispatch it reuses) actually works.
"""

from __future__ import annotations

import json

import pytest

from formist_cli import task_paths, tasks


def _run(capsys, *argv):
    code = tasks.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_task_through_the_real_cli_main(tmp_path, monkeypatch, capsys):
    """`fm task` as `fm` itself invokes it: ``cli.main(["task", ...])``,
    not the standalone ``tasks.main`` every other test in this file uses.
    Proves the actual wiring (the ``tasks_mod.add_parser(sub)``/
    ``"task": tasks_mod.run``/``privileges.TABLE["task"]`` entries added
    to ``cli.py``/``privileges.py``), not just the dispatch it shares."""
    from formist_cli import cli

    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    code = cli.main(["task", "init", str(tmp_path)])
    capsys.readouterr()  # drain "created <path>", not part of what the next call prints
    assert code == 0
    assert (tmp_path / "decisions" / "active").is_dir()

    code = cli.main(["task", "add", "A plain task", "--task", "--dir", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    created = json.loads(out)
    assert created["title"] == "A plain task"

    code = cli.main(["task", "start", created["ref"], "--dir", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["status"] == "in_progress"

    # A bad command still reaches cli.main's own USER_ERRORS loop (the
    # thing *_task_user_errors()* is folded into), not an unhandled
    # traceback: `fm task` is a real citizen of the shared error handling,
    # not a parallel path that merely looks the same.
    code = cli.main(["task", "get", "X999", "--dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "does not exist" in err


def test_init_creates_decisions_dir(tmp_path, capsys):
    code, out, _ = _run(capsys, "init", str(tmp_path))
    assert code == 0
    assert (tmp_path / "decisions" / "active").is_dir()


def test_add_get_lifecycle_through_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    code, _, _ = _run(capsys, "init", str(tmp_path))
    assert code == 0

    code, out, _ = _run(capsys, "add", "A plain task", "--task", "--dir", str(tmp_path), "--json")
    assert code == 0
    created = json.loads(out)
    assert created["title"] == "A plain task"
    assert created["status"] == "not_started"

    code, out, _ = _run(capsys, "start", "X1", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["status"] == "in_progress"

    code, out, _ = _run(capsys, "done", "X1", "--note", "shipped", "--dir", str(tmp_path), "--json")
    assert code == 0
    done = json.loads(out)
    assert done["status"] == "completed"
    assert done["archived"] is True

    code, out, _ = _run(capsys, "get", "X1", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["status"] == "completed"


def test_push_human_defaults_by_to_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "Decide", "--option", "1=A", "--option", "2=B", "--default", "1",
         "--dir", str(tmp_path), "--json")
    code, out, _ = _run(capsys, "push", "X1", "1", "--human", "--note", "going with A",
                         "--dir", str(tmp_path), "--json")
    assert code == 0
    answer = json.loads(out)["answer"]
    assert answer["by"] == "owner"
    assert answer["choice"] == "1"


def test_show_active_scope_lists_the_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "Task one", "--task", "--dir", str(tmp_path), "--json")
    code, out, _ = _run(capsys, "show", "active", "--dir", str(tmp_path))
    assert code == 0
    assert "Task one" in out


def test_views_lists_the_six_built_ins(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    _run(capsys, "init", str(tmp_path))
    code, out, _ = _run(capsys, "views", "--dir", str(tmp_path), "--json")
    assert code == 0
    ids = {v["id"] for v in json.loads(out)["views"]}
    assert ids == {"decide", "available", "active", "waiting", "live", "all", "external"}


def test_order_through_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "A", "--task", "--dir", str(tmp_path), "--json")
    _run(capsys, "add", "B", "--task", "--dir", str(tmp_path), "--json")
    code, out, _ = _run(capsys, "order", "X2", "X1", "--dir", str(tmp_path), "--json")
    assert code == 0
    ordered = json.loads(out)
    assert [n["order"] for n in ordered] == [-2, -1]

    code, out, _ = _run(capsys, "order", "X1", "--clear", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)[0]["order"] is None


def test_add_resolves_a_fuzzy_due_date(tmp_path, monkeypatch, capsys):
    # Not a hardcoded date: "next week" is resolved relative to whenever
    # the gate actually runs, so this stays correct on any date.
    from formist_cli import task_fuzzy_date

    expected = task_fuzzy_date.resolve_due("next week")
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    code, out, _ = _run(capsys, "add", "Something", "--task", "--due", "next week", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["due"] == expected


def test_add_refuses_an_unparseable_due_date(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    code, _, err = _run(capsys, "add", "Something", "--task", "--due", "not a date", "--dir", str(tmp_path))
    assert code != 0
    assert "--due" in err


def test_defer_needs_owner_said_or_human_through_cli(tmp_path, monkeypatch, capsys):
    from formist_cli import task_fuzzy_date

    expected = task_fuzzy_date.resolve_until("mon")
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "Something", "--task", "--dir", str(tmp_path))

    code, _, err = _run(capsys, "defer", "X1", "--until", "mon", "--agent", "--dir", str(tmp_path))
    assert code != 0
    assert "owner" in err

    code, out, _ = _run(capsys, "defer", "X1", "--until", "mon", "--owner-said", "push to monday",
                         "--agent", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["deferred_until"] == expected


def test_no_decisions_dir_and_no_host_refuses_with_a_hint(tmp_path, capsys):
    code, _, err = _run(capsys, "show", "--dir", str(tmp_path))
    assert code != 0
    assert "fm task init" in err


def test_local_and_host_together_is_refused(tmp_path, capsys):
    code, _, err = _run(capsys, "show", "--local", "--host", "formist", "--dir", str(tmp_path))
    assert code != 0
    assert "don't mix" in err


def test_check_through_cli_json_and_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "A plain task", "--task", "--body", "Some body text.", "--dir", str(tmp_path))

    code, out, _ = _run(capsys, "check", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out) == []

    code, out, _ = _run(capsys, "check", "--dir", str(tmp_path))
    assert code == 0
    assert "1 entries, no problems" in out


def test_check_groups_repeated_messages_through_cli(tmp_path, monkeypatch, capsys):
    """6 broken entries sharing the same message collapse to 5 lines
    plus a "…1 more entry" summary line, matching ``check.ts``'s own
    dedup-by-message grouping."""
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    layout = task_paths.find_layout(str(tmp_path))
    import os

    for i in range(1, 7):
        with open(os.path.join(layout.active_dir, f"{i}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nid: {i}\ntitle: No author {i}\nstatus: not_started\n"
                      "created: 2026-11-01T10:00:00+08:00\n---\nBody.\n")
        os.makedirs(layout.ids_dir, exist_ok=True)
        open(os.path.join(layout.ids_dir, str(i)), "w").close()

    code, out, _ = _run(capsys, "check", "--dir", str(tmp_path))
    assert code == 0
    lines = out.splitlines()
    author_lines = [l for l in lines if l.startswith("!") and "no author" in l]
    assert len(author_lines) == 5
    assert any("…1 more entry is also" in l for l in lines)
    assert "6 entries, 0 errors, 6 warnings" in out


def test_check_exit_code_1_on_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    layout = task_paths.find_layout(str(tmp_path))
    import os

    with open(os.path.join(layout.active_dir, "1.md"), "w", encoding="utf-8") as fh:
        fh.write('---\nid: 1\ntitle: Cycle\nauthor: agent · cli test\nstatus: not_started\n'
                  'created: 2026-11-01T10:00:00+08:00\nafter: ["X1"]\n---\nBody.\n')
    os.makedirs(layout.ids_dir, exist_ok=True)
    open(os.path.join(layout.ids_dir, "1"), "w").close()

    code, out, _ = _run(capsys, "check", "--dir", str(tmp_path))
    assert code == 1
    assert "dependency cycle" in out


def test_resolve_mode_ignores_a_stray_token_without_fm_use(tmp_path, monkeypatch):
    """A token file left over from unrelated `fm` testing must not flip
    `fm task` into cloud mode on its own — only an actual `fm use` default
    (space + credential) does (contract §6.3)."""
    import argparse

    from formist_cli import prefs

    monkeypatch.setattr(prefs, "load", lambda: prefs.Prefs(space=None, env=None, space_region=None, set_at=None))
    task_paths.init_layout(str(tmp_path))
    args = argparse.Namespace(local=False, host=None, dir=str(tmp_path))
    mode, target = tasks.resolve_mode(args)
    assert mode == "local"


# ===========================================================================
# cloud mode through the CLI (C1141): `resolve_mode` really does hand the
# `_cmd_*` handlers a `CloudStore`, and the handlers really do drive it —
# the end-to-end half of what test_task_cloud.py checks at the unit level.
# ===========================================================================
SPACE = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def cloud(monkeypatch):
    """Every `fm task <verb> --host test --space <uuid>` call in a test
    resolves to a fixed cloud context and a recording fake `api`, the
    same convention ``test_task_cloud.py``'s own ``store`` fixture uses —
    bypassing real prefs/token files entirely, the way ``test_spaces.py``'s
    ``driven`` fixture does for the same reason (hermetic, no real-disk
    read)."""
    import contextlib

    from formist_cli import context, task_cloud
    from formist_cli.targets import resolve as resolve_target

    calls: list[tuple] = []
    responses: list = []
    stream_lines: list = []

    def _next():
        return responses.pop(0) if responses else _node_json()

    class _Api:
        def get(self, target, path, *, token, params=None, **_kw):
            calls.append(("GET", path, params))
            return _next()

        def post(self, target, path, body, *, token, **_kw):
            calls.append(("POST", path, body))
            return _next()

        def patch(self, target, path, body, *, token, **_kw):
            calls.append(("PATCH", path, body))
            return _next()

        def put(self, target, path, body, *, token, **_kw):
            calls.append(("PUT", path, body))
            return _next()

        @contextlib.contextmanager
        def stream(self, target, path, *, token, params=None, read_timeout):
            calls.append(("STREAM", path, params))
            lines = [line for frame in stream_lines for line in frame.split("\n")]
            yield iter(lines)

    monkeypatch.setattr(task_cloud, "api", _Api())
    monkeypatch.setattr(
        context, "resolve",
        lambda args, **kw: context.Context(target=resolve_target("formist"), space=SPACE, token="t", space_source="test"),
    )
    return calls, responses, stream_lines


def _node_json(**over):
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


def test_add_through_cli_reaches_cloud_mode(cloud, capsys):
    calls, _responses, _stream_lines = cloud
    code, out, _err = _run(
        capsys, "add", "Ship it", "--task", "--author", "agent · x",
        "--host", "formist", "--space", SPACE, "--json",
    )
    assert code == 0
    assert json.loads(out)["ref"] == "X5"
    assert calls[0][0] == "POST" and calls[0][1].endswith(f"/projects/{SPACE}/nodes")
    assert calls[0][2]["kind"] == "task"


def test_start_through_cli_resolves_then_posts_the_verb(cloud, capsys):
    calls, responses, _stream_lines = cloud
    responses.extend([_node_json(id=5, ref="X5"), _node_json(id=5, ref="X5", status="in_progress")])
    code, out, _err = _run(
        capsys, "start", "X5", "--host", "formist", "--space", SPACE, "--json",
    )
    assert code == 0
    assert json.loads(out)["status"] == "in_progress"
    assert calls[0] == ("GET", f"/projects/{SPACE}/nodes/X5", None)
    assert calls[1][1].endswith("/5/start")  # the resolved numeric id, not the ref string


def test_defer_through_cli_without_owner_said_refuses_before_the_write(cloud, capsys):
    """The agent-consent rule (``task_cloud.CloudStore.defer``) must stop a
    cloud-mode defer the same way it stops a local one: the ref still
    resolves (a GET), but the write (POST .../defer) never goes out."""
    calls, responses, _stream_lines = cloud
    responses.append(_node_json(id=5, ref="X5"))
    code, _out, err = _run(
        capsys, "defer", "X5", "--until", "2026-12-01", "--author", "agent · x",
        "--host", "formist", "--space", SPACE,
    )
    assert code == 2
    assert "owner's word" in err
    assert not any(c[0] == "POST" for c in calls)


def test_show_through_cloud_mode_refuses_with_the_vocabulary_reason(cloud, capsys):
    code, _out, err = _run(capsys, "show", "--host", "formist", "--space", SPACE)
    assert code == 2
    assert "vocabulary" in err


def test_monitor_through_cloud_mode_streams_sse_lines_and_registers_a_session(cloud, capsys):
    """``fm task monitor`` (cloud): registers a live session first (POST
    /agent-sessions), then prints the server's own monitor lines verbatim
    off the SSE stream, stopping on DECS-DONE — the two pieces C1141's
    brief named explicitly."""
    calls, _responses, stream_lines = cloud
    stream_lines.extend([
        "event: line\ndata: DECS-UPDATED X5: something changed\n\n",
        "event: line\ndata: DECS-DONE nothing of yours is open\n\n",
    ])
    code, out, err = _run(capsys, "monitor", "--mine", "--author", "agent · x",
                           "--host", "formist", "--space", SPACE)
    assert code == 0
    assert "DECS-UPDATED X5: something changed" in out
    assert "DECS-DONE nothing of yours is open" in out
    assert err == ""  # the session registration did not fail
    assert any(c[0] == "POST" and c[1].endswith("/agent-sessions") for c in calls)
    assert any(c[0] == "STREAM" and c[1].endswith("/nodes/events") for c in calls)


# ===========================================================================
# `fm task monitor --here` (C1053/C1139): what the Claude Code plugin's
# monitors.json auto-starts — porting `nod monitor --here`'s own contract:
# local project here wins, else a remembered cloud default, else quit
# quietly (never an error for "this just isn't a project").
# ===========================================================================
def test_monitor_here_exits_quietly_with_no_project_and_no_remembered_cloud(tmp_path, monkeypatch, capsys):
    from formist_cli import prefs

    monkeypatch.setattr(prefs, "load", lambda: prefs.Prefs(space=None, env=None, space_region=None, set_at=None))
    code, out, err = _run(capsys, "monitor", "--here", "--dir", str(tmp_path))
    assert code == 0
    assert out == "" and err == ""


def test_monitor_here_picks_the_local_project_and_reports_a_note_added_after(tmp_path, monkeypatch, capsys):
    """``--here`` must resolve to THIS local project (not quietly skip it,
    not reach for a cloud fallback): a note added after the monitor's own
    fresh cursor is caught on the next pass, same as a plain `fm task
    monitor` would — proving it is really watching, not just exiting 0
    for an unrelated reason."""
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    created = _run(capsys, "add", "A plain task", "--task", "--dir", str(tmp_path), "--json")
    ref = json.loads(created[1])["ref"]
    # First pass establishes this author's cursor at "now" (nothing to
    # report about work added before the monitor ever ran — expected, see
    # task_monitor's own "since" semantics, not a bug).
    code, _out, _err = _run(capsys, "monitor", "--here", "--once", "--dir", str(tmp_path))
    assert code == 0

    _run(capsys, "note", ref, "a human update", "--human", "--dir", str(tmp_path))
    code, out, _err = _run(capsys, "monitor", "--here", "--once", "--dir", str(tmp_path))
    assert code == 0
    assert ref in out


def test_monitor_here_falls_back_to_a_remembered_cloud_space(tmp_path, cloud, monkeypatch, capsys):
    """No local decisions/ here, but `fm use` remembers a cloud space with
    a credential — contract §6.3's own fallback order, reused for
    ``--here``."""
    from formist_cli import prefs, tokens

    # `_use_remembered_host_with_credential` (the --here detection, same
    # function `resolve_mode` uses) reads prefs/tokens directly, a layer
    # below where the `cloud` fixture patches context.resolve — both need
    # a remembered space + credential here, or this falls through to
    # "nothing here", the thing being tested against.
    monkeypatch.setattr(
        prefs, "load", lambda: prefs.Prefs(space=SPACE, env="formist", space_region=None, set_at=None),
    )
    monkeypatch.setattr(tokens, "load_for", lambda env: ("t", None))

    calls, _responses, stream_lines = cloud
    stream_lines.append("event: line\ndata: DECS-DONE nothing of yours is open\n\n")
    code, out, _err = _run(capsys, "monitor", "--here", "--dir", str(tmp_path))
    assert code == 0
    assert "DECS-DONE nothing of yours is open" in out
    assert any(c[0] == "STREAM" for c in calls)


# ===========================================================================
# `fm task tell` (C1053/C1139): cloud-only, and not landed server-side yet
# either way — both refusals must name why, not just say no.
# ===========================================================================
def test_tell_in_local_mode_refuses_with_the_no_registry_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    code, _out, err = _run(capsys, "tell", "please check the budget", "--dir", str(tmp_path))
    assert code == 2
    assert "no multi-project main-session registry" in err


def test_tell_through_cli_reaches_cloud_mode(cloud, capsys):
    """`fm task tell` in cloud mode now reaches POST /tell for real
    (C1130's routes landed) — the client-shape work from before is now
    live, not a refusal."""
    calls, responses, _stream_lines = cloud
    responses.append({
        "id": "t-1", "text": "please check the budget", "under": None, "author": "owner",
        "important": False, "created_at": "2026-01-01T00:00:00+00:00", "routed": "cloud",
        "done": {"at": "2026-01-01T00:00:01+00:00", "refs": ["X9"]},
        "node": _node_json(id=9, ref="X9"), "chat_id": "chat-1",
    })
    code, out, _err = _run(capsys, "tell", "please check the budget",
                            "--author", "owner", "--host", "formist", "--space", SPACE)
    assert code == 0
    assert "routed: cloud" in out
    assert "filed as: X9" in out
    assert calls[0][0] == "POST" and calls[0][1].endswith("/tell")
    assert calls[0][2] == {"text": "please check the budget", "author": "owner", "important": False}


def test_tell_needs_text_or_a_subcommand_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    code, _out, err = _run(capsys, "tell", "--dir", str(tmp_path))
    assert code == 2
    assert "needs the owner's words" in err


# ===========================================================================
# plugin-session-start / plugin-prompt-refs (C1053/C1139): what
# hooks/hooks.json runs directly, end to end through tasks.main().
# ===========================================================================
def _run_with_stdin(capsys, monkeypatch, stdin_text, *argv):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return _run(capsys, *argv)


def test_plugin_session_start_includes_rules_and_open_work(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "init", str(tmp_path))
    _run(capsys, "add", "A plain task", "--task", "--dir", str(tmp_path))

    code, out, _err = _run_with_stdin(
        capsys, monkeypatch, json.dumps({"cwd": str(tmp_path)}), "plugin-session-start",
    )
    assert code == 0
    payload = json.loads(out)
    text = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "MUST FOLLOW" in text
    assert "A plain task" in text


def test_plugin_session_start_with_no_project_is_rules_only(tmp_path, monkeypatch, capsys):
    from formist_cli import prefs

    monkeypatch.setattr(prefs, "load", lambda: prefs.Prefs(space=None, env=None, space_region=None, set_at=None))
    code, out, _err = _run_with_stdin(
        capsys, monkeypatch, json.dumps({"cwd": str(tmp_path)}), "plugin-session-start",
    )
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "MUST FOLLOW" in text
    assert "Open work" not in text


def test_plugin_prompt_refs_reminds_when_a_ref_is_named(tmp_path, monkeypatch, capsys):
    code, out, _err = _run_with_stdin(
        capsys, monkeypatch, json.dumps({"prompt": "please check C1052 today", "cwd": str(tmp_path)}),
        "plugin-prompt-refs",
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "C1052" in payload["hookSpecificOutput"]["additionalContext"]


def test_plugin_prompt_refs_prints_nothing_with_no_ref_named(tmp_path, monkeypatch, capsys):
    code, out, _err = _run_with_stdin(
        capsys, monkeypatch, json.dumps({"prompt": "hello there", "cwd": str(tmp_path)}), "plugin-prompt-refs",
    )
    assert code == 0
    assert out == ""


# ===========================================================================
# session convenience (C1139): omitting a ref on beat/step/note falls back
# to this session's current node; omitting --author falls back to what
# this session last signed as — both through the real CLI, local mode only.
# ===========================================================================
def test_start_with_an_author_remembers_both_the_node_and_the_author(tmp_path, monkeypatch):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setenv("NODDIT_WORKTREE", "/w")
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    tasks.main(["init", str(project)])
    tasks.main(["add", "A task", "--task", "--author", "agent · y", "--dir", str(project)])
    tasks.main(["start", "X1", "--author", "agent · y", "--dir", str(project)])

    from formist_cli import task_sessions

    assert task_sessions.current_node()["ref"] == "X1"
    assert task_sessions.remembered_author() == "agent · y"


def test_beat_with_no_ref_falls_back_to_the_session_current_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setenv("NODDIT_WORKTREE", "/w")
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    _run(capsys, "init", str(project))
    _run(capsys, "add", "A task", "--task", "--author", "agent · y", "--dir", str(project))
    _run(capsys, "start", "X1", "--author", "agent · y", "--dir", str(project))

    code, out, _err = _run(capsys, "beat", "--dir", str(project), "--json")
    assert code == 0
    assert json.loads(out)["ref"] == "X1"


def test_note_with_no_ref_and_no_author_uses_both_remembered_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setenv("NODDIT_WORKTREE", "/w")
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    _run(capsys, "init", str(project))
    _run(capsys, "add", "A task", "--task", "--author", "agent · y", "--dir", str(project))
    _run(capsys, "start", "X1", "--author", "agent · y", "--dir", str(project))

    code, out, _err = _run(capsys, "note", "relying on remembered state", "--agent", "--dir", str(project), "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["ref"] == "X1"
    assert payload["notes"][-1]["author"] == "agent · y"


def test_step_with_no_ref_falls_back_to_the_session_current_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setenv("NODDIT_WORKTREE", "/w")
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    _run(capsys, "init", str(project))
    _run(capsys, "add", "A task", "--task", "--author", "agent · y", "--dir", str(project))
    _run(capsys, "start", "X1", "--author", "agent · y", "--dir", str(project))
    _run(capsys, "steps", "X1", "a", "b", "--dir", str(project))

    code, out, _err = _run(capsys, "step", "1", "--dir", str(project), "--json")
    assert code == 0
    assert json.loads(out)["ref"] == "X1"


def test_beat_with_no_ref_and_no_session_refuses_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    _run(capsys, "init", str(project))

    code, _out, err = _run(capsys, "beat", "--dir", str(project))
    assert code == 2
    assert "current node" in err


def test_a_human_running_start_records_no_session_state(tmp_path, monkeypatch):
    """Matches noddit's own `if (author && rememberStarted(...))`: a bare
    `fm task start` with no author resolved at all records nothing — the
    session convenience is for repeated agent CLI calls, not a human's
    one-off terminal use."""
    monkeypatch.setenv("NODDIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setenv("NODDIT_WORKTREE", "/w")
    monkeypatch.delenv("NODDIT_AUTHOR", raising=False)
    project = tmp_path / "proj"
    tasks.main(["init", str(project)])
    tasks.main(["add", "A task", "--task", "--author", "agent · y", "--dir", str(project)])
    tasks.main(["start", "X1", "--dir", str(project)])  # no --author this time

    from formist_cli import task_sessions

    assert task_sessions.current_node() is None


# ===========================================================================
# search / changes through cloud mode (C1130)
# ===========================================================================
def test_search_through_cli_reaches_cloud_mode(cloud, capsys):
    calls, responses, _stream_lines = cloud
    responses.append({"nodes": [_node_json(id=3, ref="X3", title="Ship it")]})
    code, out, _err = _run(capsys, "search", "ship", "it", "--host", "formist", "--space", SPACE, "--json")
    assert code == 0
    assert json.loads(out)[0]["ref"] == "X3"
    assert calls[0][0] == "GET" and calls[0][1].endswith("/nodes/search")
    assert calls[0][2] == {"q": "ship it"}


def test_changes_through_cli_reaches_cloud_mode(cloud, capsys):
    calls, responses, _stream_lines = cloud
    responses.append({"events": [{"ref": "X1", "at": "2026-01-01T00:00:00+00:00", "kind": "created"}]})
    code, out, _err = _run(
        capsys, "changes", "--since", "2026-01-01T00:00:00+00:00", "--host", "formist", "--space", SPACE,
    )
    assert code == 0
    assert "X1" in out and "created" in out
    assert calls[0][1].endswith("/nodes/changes")


def test_changes_through_cli_prefers_note_kind_for_a_note_row(cloud, capsys):
    calls, responses, _stream_lines = cloud
    responses.append({"events": [
        {"ref": "X1", "at": "2026-01-01T00:00:00+00:00", "kind": "note", "note_kind": "ask"},
    ]})
    code, out, _err = _run(
        capsys, "changes", "--since", "2026-01-01T00:00:00+00:00", "--host", "formist", "--space", SPACE,
    )
    assert code == 0
    assert "ask" in out
    assert calls[0][1].endswith("/nodes/changes")
