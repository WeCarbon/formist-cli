"""``fm task <verb>`` through the real CLI entry point (``cli.main``),
covering argument parsing, mode resolution, and dispatch — the layer
``test_task_store.py`` doesn't exercise."""

from __future__ import annotations

import json

import pytest

from formist_cli import cli, task_paths, tasks


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_creates_decisions_dir(tmp_path, capsys):
    code, out, _ = _run(capsys, "task", "init", str(tmp_path))
    assert code == 0
    assert (tmp_path / "decisions" / "active").is_dir()


def test_add_get_lifecycle_through_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    code, _, _ = _run(capsys, "task", "init", str(tmp_path))
    assert code == 0

    code, out, _ = _run(capsys, "task", "add", "A plain task", "--task", "--dir", str(tmp_path), "--json")
    assert code == 0
    created = json.loads(out)
    assert created["title"] == "A plain task"
    assert created["status"] == "not_started"

    code, out, _ = _run(capsys, "task", "start", "X1", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["status"] == "in_progress"

    code, out, _ = _run(capsys, "task", "done", "X1", "--note", "shipped", "--dir", str(tmp_path), "--json")
    assert code == 0
    done = json.loads(out)
    assert done["status"] == "completed"
    assert done["archived"] is True

    code, out, _ = _run(capsys, "task", "get", "X1", "--dir", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out)["status"] == "completed"


def test_push_human_defaults_by_to_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "task", "init", str(tmp_path))
    _run(capsys, "task", "add", "Decide", "--option", "1=A", "--option", "2=B", "--default", "1",
         "--dir", str(tmp_path), "--json")
    code, out, _ = _run(capsys, "task", "push", "X1", "1", "--human", "--note", "going with A",
                         "--dir", str(tmp_path), "--json")
    assert code == 0
    answer = json.loads(out)["answer"]
    assert answer["by"] == "owner"
    assert answer["choice"] == "1"


def test_show_lists_available_nodes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · cli test")
    _run(capsys, "task", "init", str(tmp_path))
    _run(capsys, "task", "add", "Task one", "--task", "--dir", str(tmp_path), "--json")
    code, out, _ = _run(capsys, "task", "show", "available", "--dir", str(tmp_path))
    assert code == 0
    assert "Task one" in out


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


def test_no_decisions_dir_and_no_host_refuses_with_a_hint(tmp_path, capsys):
    code = cli.main(["task", "show", "--dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "fm task init" in err


def test_local_and_host_together_is_refused(tmp_path, capsys):
    code = cli.main(["task", "show", "--local", "--host", "formist", "--dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != 0
    assert "don't mix" in err


def test_resolve_mode_ignores_a_stray_token_without_fm_use(tmp_path, monkeypatch):
    """A token file left over from unrelated `fm` testing must not flip
    `fm task` into cloud mode on its own — only an actual `fm use` default
    (space + credential) does (contract §6.3)."""
    import argparse

    from formist_cli import prefs

    monkeypatch.setattr(prefs, "load", lambda: prefs.Prefs(space=None, env=None, space_region=None, set_at=None))
    layout = task_paths.init_layout(str(tmp_path))
    args = argparse.Namespace(local=False, host=None, dir=str(tmp_path))
    mode, target = tasks.resolve_mode(args)
    assert mode == "local"
