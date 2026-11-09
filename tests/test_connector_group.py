"""Tests for ``fm connector``: the noun group ``fm connectors`` (list,
grants) folded into, alongside its own original ``reauth``/``sync``.

``cmd_connector`` routes ``list``/``grants`` back into ``cmd_connectors``
(the exact function `fm connectors` always ran) and anything else into
``connectors_mod.run_connector`` (the exact function `fm connector` always
ran) -- neither is reimplemented. ``grants`` has no member-level
equivalent and must still refuse locally without ``--admin``, reached
through either name.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import cli, privileges
from formist_cli import connectors as connectors_mod


def _args(**over):
    base = {"subject": None, "arg": None, "space_id": None, "space": None, "admin": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_list_routes_to_cmd_connectors_with_arg_as_space_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_connectors", lambda a, **kw: seen.update(args=a) or 0)
    rc = cli.cmd_connector(_args(subject="list", arg="452093ea-7b91-491e-88b6-3285edd48a31"))
    assert rc == 0
    assert seen["args"].space_id == "452093ea-7b91-491e-88b6-3285edd48a31"


def test_grants_without_admin_is_refused_before_any_request():
    with pytest.raises(privileges.PrivilegeError, match="fm connector grants"):
        cli.cmd_connector(_args(subject="grants", admin=False))


def test_reauth_routes_to_run_connector_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        connectors_mod, "run_connector", lambda a: seen.update(args=a) or 0
    )
    rc = cli.cmd_connector(_args(subject="reauth", arg="conn-1"))
    assert rc == 0
    assert seen["args"].subject == "reauth"
    assert seen["args"].arg == "conn-1"


def test_sync_routes_to_run_connector_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        connectors_mod, "run_connector", lambda a: seen.update(args=a) or 0
    )
    rc = cli.cmd_connector(_args(subject="sync", arg="conn-2"))
    assert rc == 0
    assert seen["args"].subject == "sync"


def test_unknown_subject_names_all_four():
    with pytest.raises(connectors_mod.ConnectorsError, match="list, grants.*reauth.*sync"):
        cli.cmd_connector(_args(subject="bogus"))


def _alias_handler(fn):
    """See the identical helper in ``test_skill_group.py``: ``_COMMANDS`` is
    a dict literal, so the real handler is read out of the wrapper's
    closure, not re-looked-up off the module.
    """
    i = fn.__code__.co_freevars.index("handler")
    return fn.__closure__[i].cell_contents


def test_connectors_alias_is_wired_to_the_real_cmd_connectors():
    assert _alias_handler(cli._COMMANDS["connectors"]) is cli.cmd_connectors


def test_connectors_alias_warns_then_runs_the_handler(capsys):
    calls = []
    wrapped = cli._deprecated_alias(
        "connectors", "connector list|grants", lambda a: calls.append(a) or 0
    )
    rc = wrapped(argparse.Namespace())
    assert rc == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "`fm connectors`" in err
    assert "connector list|grants" in err


def test_connector_privilege_level_is_mixed_now_grants_folded_in():
    assert privileges.level_for("connector") == privileges.LEVEL_MIXED
