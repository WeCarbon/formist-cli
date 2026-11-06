"""``fm task <verb>`` through its own standalone entry point
(``tasks.main``), covering argument parsing, mode resolution, and dispatch
— the layer ``test_task_store.py`` doesn't exercise.

Deliberately NOT going through ``cli.main``: `fm task` isn't wired into
the shared ``cli.py``/``privileges.py`` yet — lane fm-review owns those
files while restructuring them on ``feat/fm-slim`` (coordination note,
2026-11-06). ``tasks.main`` is the same dispatch `cli.py` will call once
it's wired in; these tests exercise exactly that shape without touching
the files fm-review is mid-restructure on.
"""

from __future__ import annotations

import json

import pytest

from formist_cli import task_paths, tasks


def _run(capsys, *argv):
    code = tasks.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


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
