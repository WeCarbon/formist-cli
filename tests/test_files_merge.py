"""Tests for ``fm files merge``'s --admin gate.

Hard-deleting the dropped row is irreversible, which is why `merge` is the
one subject of `fm files` that needs --admin (`reevaluate`/`download` do
not; see ``test_files.py`` and ``test_files_download.py``). This pins that
refusing happens before any request, regardless of what ``--keep``/
``--drop``/``--apply`` were given.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import files as files_mod, privileges
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
KEEP = "33333333-3333-3333-3333-333333333333"
DROP = "44444444-4444-4444-4444-444444444444"


def _args(**overrides) -> argparse.Namespace:
    base = {
        "env": "local", "space": SPACE, "subject": "merge",
        "keep": KEEP, "drop": DROP, "reason": None, "apply": None,
        "json": False, "admin": False,
    }
    return argparse.Namespace(**(base | overrides))


def test_merge_without_admin_refuses_and_never_resolves_a_target(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must refuse before resolving a target/token")

    from formist_cli import cli

    monkeypatch.setattr(cli, "_effective", boom)
    monkeypatch.setattr(files_mod.api, "get", boom)

    with pytest.raises(privileges.PrivilegeError, match="--admin"):
        files_mod.cmd_files(_args())


def test_merge_with_admin_reaches_the_dry_run(monkeypatch, capsys):
    calls: list[str] = []

    def fake_get(target, path, *, token=None, **kw):
        calls.append(path)
        return {"report_token": "tok-1", "refusals": [], "idempotent": False}

    from formist_cli import cli

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
    monkeypatch.setattr(files_mod.api, "get", fake_get)

    rc = files_mod.cmd_files(_args(admin=True))
    assert rc == 0
    assert calls and calls[0].startswith(f"/projects/{SPACE}/admin/files/merge")


class _Eff:
    target = LOCAL
    space = SPACE
    token = "t"
