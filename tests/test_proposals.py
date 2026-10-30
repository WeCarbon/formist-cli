"""Tests for ``fm proposals``: `list` works for any member of the space by
default; `unstick` is a platform-admin repair tool that always needs
--admin.

Both halves of the MIXED contract are pinned here: which endpoint `list`
reaches with and without ``--admin``, and that ``unstick`` refuses before
any request when ``--admin`` is missing, regardless of what subject was
asked for.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import cli, privileges


class _Eff:
    target = "global"
    space = "452093ea-7b91-491e-88b6-3285edd48a31"
    token = "t"


def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": "list", "admin": False, "status": None, "target_kind": None,
        "limit": "50", "proposal_ids": None, "apply": False,
    }
    return argparse.Namespace(**(base | overrides))


def test_default_list_reads_the_member_endpoint_not_admin(monkeypatch):
    seen: dict = {}

    def fake_get(target, path, *, token, params=None, **kwargs):
        seen["path"] = path
        return {"items": []}

    def boom_admin_get(*a, **k):
        raise AssertionError("must not call the admin endpoint without --admin")

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli.api, "get", fake_get)
    monkeypatch.setattr(cli.api, "admin_get", boom_admin_get)

    rc = cli.cmd_proposals(_args())
    assert rc == 0
    assert seen["path"] == f"/projects/{_Eff.space}/proposals"


def test_admin_list_reads_the_admin_endpoint_not_member(monkeypatch):
    seen: dict = {}

    def fake_admin_get(target, path, *, token, params=None, **kwargs):
        seen["path"] = path
        return {"items": []}

    def boom_get(*a, **k):
        raise AssertionError("must not call the member endpoint with --admin")

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli.api, "admin_get", fake_admin_get)
    monkeypatch.setattr(cli.api, "get", boom_get)

    rc = cli.cmd_proposals(_args(admin=True))
    assert rc == 0
    assert seen["path"] == f"/admin/spaces/{_Eff.space}/proposals"


def test_unstick_refuses_without_admin_and_never_resolves_a_target(monkeypatch):
    """`unstick` has no member-level equivalent and always needs --admin,
    independent of whatever `list` does."""

    def boom(*a, **k):
        raise AssertionError("must refuse before resolving a target/token")

    monkeypatch.setattr(cli, "_effective", boom)
    monkeypatch.setattr(cli.api, "admin_post", boom)

    with pytest.raises(privileges.PrivilegeError, match="--admin"):
        cli.cmd_proposals(_args(subject="unstick", admin=False))


def test_unstick_with_admin_reaches_the_unstick_endpoint(monkeypatch, capsys):
    calls: list[tuple] = []

    def fake_admin_post(target, path, body, *, token):
        calls.append((path, body))
        return {"dry_run": True, "total": 0, "half_applied": 0, "proposals": []}

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli.api, "admin_post", fake_admin_post)

    rc = cli.cmd_proposals(_args(subject="unstick", admin=True))
    assert rc == 0
    assert calls[0][0] == f"/admin/spaces/{_Eff.space}/proposals/unstick"
