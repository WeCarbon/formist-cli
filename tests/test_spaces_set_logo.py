"""Tests for ``fm spaces set-logo``: ``fm project`` folded into ``fm
spaces`` as one more subject, plus ``fm project`` kept as a deprecated
alias.

``spaces._run_set_logo`` adapts the two generic positional slots
(``space_id``/``child_id``) into the names `formist_cli.projects.run`
(the ORIGINAL, unchanged implementation) reads: ``path`` first, then an
alternative to ``--space`` second -- the same order `fm project` itself
always used.
"""

from __future__ import annotations

import argparse

from formist_cli import cli
from formist_cli import projects as projects_mod
from formist_cli import spaces as spaces_mod


def _args(**over):
    base = {
        "subject": "set-logo", "space_id": None, "child_id": None, "space": None,
        "admin": False, "apply": False, "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_set_logo_reuses_space_id_slot_as_path_and_child_id_slot_as_space_id(monkeypatch):
    """`fm spaces set-logo <path> <space_id>` -- the generic positional
    slots carry `project`'s own order (path first, then an alternative to
    --space second), documented on the subject's own help text.
    """
    seen = {}
    monkeypatch.setattr(
        projects_mod, "run", lambda a, **kw: seen.update(args=(a, kw)) or 0
    )
    rc = spaces_mod.run(_args(space_id="/tmp/logo.png", child_id="alt-space-id"))
    assert rc == 0
    adapted, kwargs = seen["args"]
    assert adapted.path == "/tmp/logo.png"
    assert adapted.space_id == "alt-space-id"
    assert adapted.subject == "set-logo"
    assert kwargs == {"label": "fm spaces set-logo"}


def test_set_logo_does_not_require_admin(monkeypatch):
    """Every OTHER non-list subject on `fm spaces` needs --admin; set-logo
    must not, since it folded in from a plain member-level command.
    """
    monkeypatch.setattr(projects_mod, "run", lambda a, **kw: 0)
    rc = spaces_mod.run(_args(admin=False))
    assert rc == 0


def test_set_logo_is_not_routed_through_the_mutating_subject_dispatch(monkeypatch):
    """Guards the ordering in `spaces.run`: `set-logo` must be intercepted
    BEFORE `_MUTATING_SUBJECTS`/`_run_write`, which is spaces.py's own CRUD
    surface and does not know this subject.
    """
    def _boom(*a, **kw):
        raise AssertionError("_run_write must not be reached for set-logo")

    monkeypatch.setattr(spaces_mod, "_run_write", _boom)
    monkeypatch.setattr(projects_mod, "run", lambda a, **kw: 0)
    assert spaces_mod.run(_args()) == 0


def _alias_handler(fn):
    """See the identical helper in ``test_skill_group.py``: ``_COMMANDS`` is
    a dict literal, so the real handler is read out of the wrapper's
    closure, not re-looked-up off the module.
    """
    i = fn.__code__.co_freevars.index("handler")
    return fn.__closure__[i].cell_contents


def test_project_alias_is_wired_to_the_real_projects_run():
    assert _alias_handler(cli._COMMANDS["project"]) is projects_mod.run


def test_project_alias_warns_then_runs_the_handler(capsys):
    calls = []
    wrapped = cli._deprecated_alias("project", "spaces set-logo", lambda a: calls.append(a) or 0)
    rc = wrapped(argparse.Namespace())
    assert rc == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "`fm project`" in err
    assert "spaces set-logo" in err


def test_set_logo_is_a_valid_spaces_subject():
    assert "set-logo" in spaces_mod.SUBJECTS
    assert spaces_mod.canonical_subject("set-logo") == "set-logo"
