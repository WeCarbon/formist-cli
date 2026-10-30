"""Tests for ``fm audit-log``: the newest rows, and an honest account of
how many it saw.

The server's default sort for this route is ascending by `created_at`,
and the server also caps `limit` silently. A command that sends no `sort`
and asks for more rows than the cap allows would receive the oldest page
instead of the newest, with no error of its own to flag the mismatch, so
the request must always ask for a descending sort explicitly.

This follows the same defect class ``fm users`` guards against: a report
that is true about what it fetched and false about what exists, which
never surfaces on its own because every number it prints is real. So
these tests range over the three outcomes a paginated listing can have
(truncated, complete, and `total` missing) and assert that each one is
distinguishable from the others in the output. A truncation notice that
also fires when nothing was truncated is an alarm that cries wolf, and an
operator who learns to ignore it has lost the notice for the case that
matters; that direction is tested too.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from formist_cli import cli


@dataclass
class _Eff:
    """The three attributes ``cmd_audit_log`` reads off the resolved context."""

    target: str = "https://api.evane.ai"
    space: str = "452093ea-7b91-491e-88b6-3285edd48a31"
    token: str = "token-not-used-by-the-fake"


def _rows(n: int) -> list[dict]:
    return [
        {
            "created_at": f"2026-09-0{(i % 9) + 1}T00:00:00",
            "actor_type": "HUMAN",
            "action": "CREATE",
            "target_kind": "SESSION",
        }
        for i in range(n)
    ]


@pytest.fixture
def run(monkeypatch):
    """Drive ``cmd_audit_log`` against a fake server, returning (params, stdout)."""

    def _run(capsys, payload):
        seen: dict = {}

        def fake_admin_get(target, path, *, token, params=None, **kwargs):
            seen["path"] = path
            seen["params"] = dict(params or {})
            return payload

        monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
        monkeypatch.setattr(cli.api, "admin_get", fake_admin_get)

        rc = cli.cmd_audit_log(_Args())
        assert rc == 0
        return seen, capsys.readouterr().out

    return _run


class _Args:
    env = "global"
    space = "452093ea-7b91-491e-88b6-3285edd48a31"
    limit = "50"


class TestItAsksForTheNewestRows:
    def test_the_request_names_a_descending_sort(self, run, capsys):
        """The one question this command is ever asked is "what changed recently".

        Asserted on the REQUEST, not on the order of the printed rows: the fake
        can hand back any order it likes, so checking the output would pass with
        the sort dropped. What must not regress is the ask.
        """
        seen, _ = run(capsys, {"items": _rows(3), "total": 3})
        assert seen["params"].get("sort") == "-created_at"

    def test_the_limit_is_still_sent(self, run, capsys):
        """Adding the sort must not have displaced the parameter already there."""
        seen, _ = run(capsys, {"items": _rows(3), "total": 3})
        assert seen["params"].get("limit") == "50"


class TestItNeverOverstatesWhatItSaw:
    #: Assert that the three outcomes are distinguishable, not merely that
    #: one magic substring is present: `"200" in out and "484" in out` is
    #: satisfied by a line that echoes both numbers in the wrong
    #: relationship, which would still read as claiming 200 was all there
    #: was.

    def test_a_truncated_listing_says_how_many_it_did_not_show(self, run, capsys):
        _, out = run(capsys, {"items": _rows(200), "total": 484})
        assert "200" in out and "484" in out
        # The relationship, not just the numbers: it must say more exist.
        assert "284" in out, "the count of rows NOT listed is the actionable part"
        assert "--limit" in out, "a notice the operator cannot act on is noise"

    def test_a_complete_listing_raises_no_alarm(self, run, capsys):
        """The opposite failure. An alarm that cries wolf is a lost alarm."""
        _, out = run(capsys, {"items": _rows(7), "total": 7})
        assert "not listed" not in out
        assert "7 of 7" in out

    def test_a_missing_total_is_reported_as_unknown_not_as_complete(
        self, run, capsys
    ):
        """If the route ever stops sending `total`, say so.

        Printing a bare count here would look exactly like a complete
        listing, the same shape of false confidence, one layer down.
        """
        _, out = run(capsys, {"items": _rows(5)})
        assert "UNKNOWN" in out

    def test_the_three_outcomes_do_not_render_identically(self, run, capsys):
        """The property, over the whole set it ranges across.

        Three distinct states of the world must produce three distinct summary
        lines. A rendering where two of them agree is the defect restated.
        """
        _, truncated = run(capsys, {"items": _rows(200), "total": 484})
        _, complete = run(capsys, {"items": _rows(200), "total": 200})
        _, unknown = run(capsys, {"items": _rows(200)})
        summaries = {
            truncated.rsplit("\n\n", 1)[-1].strip(),
            complete.rsplit("\n\n", 1)[-1].strip(),
            unknown.rsplit("\n\n", 1)[-1].strip(),
        }
        assert len(summaries) == 3, f"two outcomes render alike: {summaries}"
