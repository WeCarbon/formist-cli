"""Tests for ``fm connectors``: the operator's only window onto what the
sync tick did.

A connector re-syncing far more often than its own interval setting is a
symptom that is easy to see (two `next_sync_at` reads a few minutes apart)
but hard to diagnose without the run history: which trigger caused each
run, and how long each run held the tick. This command exists to surface
that history in the terminal.

What the output has to separate, and why each is asserted below rather
than eyeballed:

  * `trigger`: `scheduler` (a periodic poll) vs `push` (a provider change
    notification). These are two different clocks, and a renderer that
    collapses them into one reading hides exactly the distinction an
    operator needs.
  * `dur`: how long a run held the tick. The tick is strictly serial, so
    this is the number that names which connector is starving the others.
  * a run in flight vs a finished one: a live run's duration is
    elapsed-so-far and must not be read as final.
  * never-run vs failed: for a linked connector, "never ran at all" is
    itself the finding, and printing nothing would hide it.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import api, cli


class _Eff:
    """The shape `_effective` returns, reduced to what `cmd_connectors` reads."""

    target = "global"
    space = "452093ea-7b91-491e-88b6-3285edd48a31"
    token = "t"


@pytest.fixture
def run_connectors(monkeypatch, capsys):
    """Drive `cmd_connectors` over a canned admin payload and return its stdout.

    `api.admin_get` is the seam: it is the one network call the command makes, and
    faking it keeps this a test of the RENDERING: which is the half that was
    missing: rather than of the route, which has its own tests.
    """

    def _run(items: list[dict]) -> str:
        monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
        monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
        monkeypatch.setattr(api, "admin_get", lambda *a, **k: {"items": items})
        monkeypatch.setattr(cli.api, "admin_get", lambda *a, **k: {"items": items})
        # --admin: the rendering under test is identical on both branches
        # (same `_connection_dict` card shape server-side); the branch choice
        # itself has its own tests below.
        rc = cli.cmd_connectors(argparse.Namespace(admin=True))
        assert rc == 0
        return capsys.readouterr().out

    return _run


def _connector(**over) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        # `type`, matching the real payload field name.
        "type": "SHAREPOINT",
        "status": "ok",
        "last_synced_at": "2026-09-06T14:33:39.514953+00:00",
        "next_sync_at": "2026-09-06T15:03:39.514953+00:00",
        "sync_interval_seconds": 1800,
    }
    base.update(over)
    return base


def test_a_finished_run_reports_its_trigger_and_how_long_it_held_the_tick(
    run_connectors,
):
    out = run_connectors(
        [
            _connector(
                sync_run={
                    "state": "succeeded",
                    "trigger": "push",
                    "started_at": "2026-09-06T14:30:17+00:00",
                    "finished_at": "2026-09-06T14:33:39+00:00",
                    "duration_seconds": 202.0,
                    "items_seen": 12,
                    "items_processed": 11,
                    "items_failed": 1,
                    "error": None,
                }
            )
        ]
    )
    assert "trigger=push" in out, (
        "the poll and a push notification must be distinguishable in the output"
    )
    assert "dur=202.0s" in out
    assert "so far" not in out, "a finished run's duration is final, not elapsed"
    assert "items=11/12" in out
    assert "failed=1" in out
    assert "every=1800s" in out


def test_a_run_still_in_flight_says_its_duration_is_only_so_far(run_connectors):
    """The run holding the tick right now is the one an operator most needs, and its
    duration is not final. Printing `202s` for a run that has not finished invites
    exactly the wrong conclusion, so the output says which kind of number it is."""
    out = run_connectors(
        [
            _connector(
                sync_run={
                    "state": "running",
                    "trigger": "scheduler",
                    "started_at": "2026-09-06T14:30:17+00:00",
                    "finished_at": None,
                    "duration_seconds": 1500.0,
                    "items_seen": 4,
                    "items_processed": 2,
                    "items_failed": 0,
                }
            )
        ]
    )
    assert "dur=1500.0s so far" in out
    assert "trigger=scheduler" in out
    assert "running" in out


def test_a_connector_that_has_never_run_says_so_rather_than_printing_nothing(
    run_connectors,
):
    """For a linked connector, "never ran at all" is a finding, not an
    empty cell. The endpoint can report `sync_run: null` for a connector
    that has not run yet, so a renderer that skips nulls silently would
    hide that it never ran."""
    out = run_connectors([_connector(sync_run=None)])
    assert "last run: (never)" in out


def test_a_failed_run_shows_its_error(run_connectors):
    out = run_connectors(
        [
            _connector(
                status="error",
                sync_run={
                    "state": "failed",
                    "trigger": "scheduler",
                    "started_at": "2026-09-06T14:30:17+00:00",
                    "finished_at": "2026-09-06T14:30:19+00:00",
                    "duration_seconds": 2.0,
                    "items_seen": 0,
                    "items_processed": 0,
                    "items_failed": 0,
                    "error": "TimeoutError",
                },
            )
        ]
    )
    assert "failed" in out
    assert "error: TimeoutError" in out


def test_every_connector_is_still_listed_when_one_has_no_run(run_connectors):
    """The never-run branch uses `continue`; a connector after it must still print."""
    out = run_connectors(
        [
            _connector(id="aaaa", sync_run=None),
            _connector(
                id="bbbb",
                sync_run={
                    "state": "succeeded",
                    "trigger": "push",
                    "started_at": "2026-09-06T14:30:17+00:00",
                    "finished_at": "2026-09-06T14:30:19+00:00",
                    "duration_seconds": 2.0,
                    "items_seen": 1,
                    "items_processed": 1,
                    "items_failed": 0,
                },
            ),
        ]
    )
    assert "aaaa" in out and "bbbb" in out
    assert "last run: (never)" in out and "trigger=push" in out


def test_the_connector_type_is_printed_and_not_a_question_mark(run_connectors):
    """The command must read the payload's actual `type` field, never a
    `kind` key that does not exist in the response: a lookup that silently
    falls through to a "?" default would print every connector as the
    same unhelpful symbol, defeating the one column that lets an operator
    tell one connector from another.
    """
    out = run_connectors(
        [
            _connector(id="aaaa", type="SHAREPOINT", sync_run=None),
            _connector(id="bbbb", type="AIRTABLE", sync_run=None),
        ]
    )
    assert "sharepoint" in out and "airtable" in out
    assert "?" not in out, (
        f"a connector printed as '?': the type column is what tells an operator "
        f"which connector they are looking at. Output:\n{out}"
    )


# ===========================================================================
# default vs --admin: which endpoint; `grants` always needs --admin
# ===========================================================================
def test_default_list_reads_the_member_endpoint_not_admin(monkeypatch, capsys):
    """No --admin: `fm connectors` must read the caller's own space's
    connections (`GET /projects/{space}/connections`), never `/admin/...`."""
    seen: dict = {}

    def fake_get(target, path, *, token, **kwargs):
        seen["path"] = path
        return {"items": []}

    def boom_admin_get(*a, **k):
        raise AssertionError("must not call the admin endpoint without --admin")

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
    monkeypatch.setattr(cli.api, "get", fake_get)
    monkeypatch.setattr(cli.api, "admin_get", boom_admin_get)

    rc = cli.cmd_connectors(argparse.Namespace(admin=False))
    assert rc == 0
    assert seen["path"] == f"/projects/{_Eff.space}/connections"


def test_admin_list_reads_the_admin_endpoint_not_member(monkeypatch, capsys):
    seen: dict = {}

    def fake_admin_get(target, path, *, token, **kwargs):
        seen["path"] = path
        return {"items": []}

    def boom_get(*a, **k):
        raise AssertionError("must not call the member endpoint with --admin")

    monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
    monkeypatch.setattr(cli.api, "admin_get", fake_admin_get)
    monkeypatch.setattr(cli.api, "get", boom_get)

    rc = cli.cmd_connectors(argparse.Namespace(admin=True))
    assert rc == 0
    assert seen["path"] == f"/admin/spaces/{_Eff.space}/connectors"


def test_grants_refuses_without_admin_and_never_calls_the_api(monkeypatch):
    """`grants` has no member-level equivalent and always needs --admin,
    whether or not `list` was given it."""

    def boom(*a, **k):
        raise AssertionError("must refuse before any request")

    monkeypatch.setattr(cli, "_effective", boom)
    monkeypatch.setattr(cli.api, "admin_get", boom)

    from formist_cli import privileges

    with pytest.raises(privileges.PrivilegeError, match="--admin"):
        cli.cmd_connectors(argparse.Namespace(subject="grants", admin=False))
