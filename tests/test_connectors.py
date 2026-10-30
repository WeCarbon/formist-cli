"""``fm connectors`` — the operator's only window onto what the sync tick did.

WHY THIS FILE EXISTS. On 2026-09-06 a production lane found that connectors in the
GFSS space were re-syncing every 3-4 minutes against a 30-minute setting. It could
report the symptom (two `next_sync_at` reads five minutes apart) but not the cause,
and said so: to confirm
it you would have to read the run history, and no admin route or `ev` subcommand
could. `fm logs --host evane-ae` is refused (Global region only) and the Global region has
no SharePoint connector of its own to look at.

Every fact needed was already recorded. `SyncRun` has `started_at`, `finished_at`,
`trigger` and the item counts; `routes/connectors.py::_sync_run_dict` serialises them.
Two links were missing, and both are now covered — `routes/admin.py` passing the run
into `_connection_dict` (tests/test_connector_run_visibility.py) and this command
printing it.

WHAT THE OUTPUT HAS TO SEPARATE, and why each is asserted below rather than eyeballed:

  * `trigger` — `scheduler` (the 30-minute poll) vs `push` (a provider change
    notification). The owner's rule is made of exactly these two clocks, and until
    2026-09-06 nothing in the tree ever wrote `"push"`, so they were one reading.
  * `dur` — how long a run held the tick. The tick is strictly serial, so this is the
    number that names which connector is starving the others.
  * a run in flight vs a finished one — a live run's duration is elapsed-so-far and
    must not be read as final.
  * never-run vs failed — for a linked connector, "never ran at all" is itself the
    finding, and printing nothing would hide it.

Run: uv run pytest tests/test_formist_cli_connectors.py -q
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
    faking it keeps this a test of the RENDERING — which is the half that was
    missing — rather than of the route, which has its own tests.
    """

    def _run(items: list[dict]) -> str:
        monkeypatch.setattr(cli, "_effective", lambda *a, **k: _Eff())
        monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
        monkeypatch.setattr(api, "admin_get", lambda *a, **k: {"items": items})
        monkeypatch.setattr(cli.api, "admin_get", lambda *a, **k: {"items": items})
        rc = cli.cmd_connectors(argparse.Namespace())
        assert rc == 0
        return capsys.readouterr().out

    return _run


def _connector(**over) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        # `type`, matching the real payload. This fixture said `kind` while the
        # server has always said `type` — a hand-written fixture agreeing with
        # itself rather than with the thing it stands in for.
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
        "the poll and a push notification must be distinguishable in the output — "
        "they are the two clocks the owner's cadence rule is made of"
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
    """For a linked connector, "never ran at all" is a finding, not an empty cell.
    The pre-2026-09-06 endpoint reported `sync_run: null` for EVERY connector, so a
    renderer that skipped nulls silently would reproduce the original blind spot."""
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
    """The command read `item["kind"]`; the payload has never had that key.

    `routes/connectors.py::_connection_dict` publishes `type` (and `label`), never
    `kind`, so `item.get("kind", "?")` fell through to the default on every row of
    every space this command has ever printed. Measured against production
    2026-09-06 (GFSS space 296346bf-…): three connectors — SHAREPOINT, OUTLOOK and
    AIRTABLE — printed as three identical `?`.

    That is not cosmetic. The command exists so an operator can tell one connector
    from another, and this is the column that does it: the whole D193 investigation
    turns on which of the three held the tick. A default of "?" is exactly the shape
    this codebase keeps being burned by — a lookup that cannot fail, and so reports
    nothing rather than reporting that it found nothing.
    """
    out = run_connectors(
        [
            _connector(id="aaaa", type="SHAREPOINT", sync_run=None),
            _connector(id="bbbb", type="AIRTABLE", sync_run=None),
        ]
    )
    assert "sharepoint" in out and "airtable" in out
    assert "?" not in out, (
        f"a connector printed as '?' — the type column is what tells an operator "
        f"which connector they are looking at. Output:\n{out}"
    )
