"""``fm space-plan`` — the client half of the plan surface.

WHAT THESE TESTS GUARD
----------------------
Not the HTTP transport (``formist_cli/api.py`` is tested elsewhere) and not the routing
(``tests/test_admin_space_plan.py`` owns that, against two real databases). What
this file pins is what the COMMAND is allowed to send and to say:

* it never sends ``apply: true`` unless ``--apply`` was given — the dry-run default
  again, one layer up, where an operator's muscle memory reaches it;
* it re-runs the dry run before an apply, and refuses when the revision has moved,
  so a stale revision cannot commit a plan nobody read;
* no request it can build names a database. The region is the SERVER's answer, and
  the command has no flag that could contradict it.

That last one is a PROPERTY over every flag combination, not a case. It is asserted
by driving the command with a recording stand-in for ``formist_cli.api`` and inspecting
everything that was sent.

Run: uv run pytest tests/test_formist_cli_space_plan.py -q
"""

from __future__ import annotations

import argparse
import re

import pytest

from formist_cli import space_plan
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"

_TARGET = {
    "space_id": SPACE,
    "space_name": "GFSS 2026",
    "region": "ae",
    "api_base": "https://ae.api.evane.ai",
    "database": {
        "region": "ae",
        "host": "evane-pg-ae.postgres.database.azure.com",
        "port": 5432,
        "database": "evane",
        "shared_with_global": False,
    },
    "project_id": SPACE,
    "project_name": "GFSS 2026",
    "project_region": "ae",
}

_EFFECTS = {
    "was_gated": False,
    "will_be_gated": True,
    "billing_gate": "FAIL-OPEN -> FAIL-CLOSED. This space has no plan row today…",
    "usage_gate_enabled_on_this_deployment": True,
    "catch_all_multiplier": 1.0,
    "catch_all_note": "every model resolves to a multiplier here",
    "effective_limits": {
        "name": "Evane Max",
        "rolling_window_hours": 5,
        "rolling_limit_standard_tokens": 20000000,
        "weekly_limit_standard_tokens": 120000000,
    },
    "effective_limits_note": "a NULL column falls back to config",
}


class RecordingApi:
    """A stand-in for ``formist_cli.api`` that records every request and answers scripted.

    ``revisions`` is consumed one per PUT, so a test can make the row move between
    the dry run and the apply — the race the revision exists to refuse.
    """

    def __init__(self, *, plan=None, revisions=("rev-aaaa",), outcome="would-create"):
        self.calls: list[tuple[str, str, object]] = []
        self._plan = plan
        self._revisions = list(revisions)
        self._outcome = outcome

    def _next_revision(self) -> str:
        return self._revisions.pop(0) if len(self._revisions) > 1 else self._revisions[0]

    def admin_get(self, target, path, *, token, **kwargs):
        self.calls.append(("GET", path, None))
        return {
            "target": _TARGET,
            "plan": self._plan,
            "revision": self._revisions[0],
            "effects": _EFFECTS,
        }

    def admin_put(self, target, path, body, *, token):
        self.calls.append(("PUT", path, body))
        applied = bool(isinstance(body, dict) and body.get("apply"))
        return {
            "applied": applied,
            "outcome": "created" if applied else self._outcome,
            "target": _TARGET,
            "before": self._plan,
            "after": {"name": "Evane Max"},
            "changes": {"name": [None, "Evane Max"]},
            "revision": self._next_revision(),
            "effects": _EFFECTS,
        }

    @property
    def bodies(self) -> list[dict]:
        return [body for _m, _p, body in self.calls if isinstance(body, dict)]


def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": "set",
        "host": "evane",
        "space": SPACE,
        "name": None,
        "rolling_window_hours": None,
        "rolling_limit_standard_tokens": None,
        "weekly_limit_standard_tokens": None,
        "additional_usage": None,
        "apply": None,
        "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    """Run the command with a recording api and a resolved context, no network."""

    def _drive(api_stub, **overrides):
        monkeypatch.setattr(space_plan, "api", api_stub)
        monkeypatch.setattr(
            space_plan.context,
            "resolve",
            lambda args, **kw: Context(
                target=GLOBAL, space=SPACE, token="t", space_source="flag"
            ),
        )
        return space_plan.run(_args(**overrides))

    return _drive


# ---------------------------------------------------------------------------
# the dry-run default
# ---------------------------------------------------------------------------
def test_set_without_apply_sends_no_apply_flag_at_all(driven, capsys):
    stub = RecordingApi()
    assert driven(stub, name="Evane Max", rolling_window_hours=5) == 0
    assert [method for method, _p, _b in stub.calls] == ["PUT"]
    body = stub.bodies[0]
    assert "apply" not in body, (
        "a dry run must not carry `apply` at all — the server's default is what "
        "makes a forgotten flag safe, and sending apply:false relies on the client "
        "remembering instead"
    )
    out = capsys.readouterr().out
    assert "NOTHING WAS WRITTEN" in out
    assert "--apply rev-aaaa" in out


def test_the_printed_next_command_is_the_one_that_applies(driven, capsys):
    stub = RecordingApi()
    driven(stub, name="Evane Max", rolling_limit_standard_tokens=20_000_000)
    out = capsys.readouterr().out
    suggested = out.rsplit("\n  ", 1)[-1].strip()
    assert suggested.startswith("fm space-plan set")
    assert "--host evane" in suggested
    assert f"--space {SPACE}" in suggested
    assert "--rolling-limit 20000000" in suggested
    assert suggested.endswith("--apply rev-aaaa")


def test_set_with_nothing_to_set_is_refused_before_any_request(driven):
    stub = RecordingApi()
    with pytest.raises(space_plan.SpacePlanError, match="nothing to set"):
        driven(stub)
    assert stub.calls == []


# ---------------------------------------------------------------------------
# the apply path
# ---------------------------------------------------------------------------
def test_apply_re_runs_the_dry_run_before_committing(driven):
    stub = RecordingApi()
    assert driven(stub, name="Evane Max", apply="rev-aaaa") == 0
    sent = stub.bodies
    assert len(sent) == 2, "the apply must re-run the dry run, not trust a stored one"
    assert "apply" not in sent[0]
    assert sent[1]["apply"] is True
    assert sent[1]["expect_revision"] == "rev-aaaa", (
        "the server must re-check the revision; a client-only check protects "
        "nobody who calls the API directly"
    )


def test_apply_with_a_stale_revision_sends_no_write(driven):
    """The row moved between the operator's dry run and their apply."""
    stub = RecordingApi(revisions=["rev-bbbb", "rev-bbbb"])
    with pytest.raises(space_plan.SpacePlanError, match="REFUSING TO APPLY"):
        driven(stub, name="Evane Max", apply="rev-aaaa")
    assert all("apply" not in body for body in stub.bodies), (
        "a refused apply must not have sent a write first"
    )


def test_off_switches_additional_usage_off_rather_than_omitting_it(driven):
    """``--additional-usage off`` is a VALUE, not an absence.

    ``False`` is falsy and an omitted flag is ``None``; collapsing the two would
    make "turn overage off" silently mean "leave it as it is" — on a column whose
    whole job is to decide whether money keeps being spent.
    """
    stub = RecordingApi()
    driven(stub, name="Evane Max", additional_usage="off")
    assert stub.bodies[0]["additional_usage_enabled"] is False
    stub_on = RecordingApi()
    driven(stub_on, name="Evane Max", additional_usage="on")
    assert stub_on.bodies[0]["additional_usage_enabled"] is True


def test_an_unset_flag_is_omitted_rather_than_sent_as_null(driven):
    """Omitting a flag must leave that column alone, not NULL it.

    The server applies only keys PRESENT in the body, so sending ``None`` for every
    unset flag would turn "set the name" into "set the name and wipe both limits"."""
    stub = RecordingApi()
    driven(stub, name="Evane Max")
    assert stub.bodies[0] == {"name": "Evane Max"}


# ---------------------------------------------------------------------------
# THE PROPERTY: nothing this command can send names a database
# ---------------------------------------------------------------------------
_DATABASE_NAMING = re.compile(
    r"region|database|\bdb\b|host|dsn|api_base|connection|dsn|url", re.IGNORECASE
)


def test_no_flag_combination_can_send_a_database_to_the_server(driven):
    """A property over the flags, not a case.

    The command has no ``--region`` today; this fails if one is ever added, whatever
    it is called, because the target must come from the server's registry lookup.
    An operator-supplied region is a flag that can be got wrong, and getting it
    wrong writes another database.
    """
    stub = RecordingApi()
    driven(
        stub,
        name="Evane Max",
        rolling_window_hours=5,
        rolling_limit_standard_tokens=20_000_000,
        weekly_limit_standard_tokens=120_000_000,
        additional_usage="off",
        apply="rev-aaaa",
    )
    offending = [
        key
        for body in stub.bodies
        for key in body
        if _DATABASE_NAMING.search(key)
    ]
    assert not offending, offending
    for _method, path, _body in stub.calls:
        assert path == f"/admin/spaces/{SPACE}/plan", path


def test_the_parser_offers_no_way_to_name_a_database():
    """Same property, one layer earlier: the flags themselves.

    ``--env`` is exempt and only ``--env``: it names which deployment's ADMIN API to
    ask, and /admin is Global-only, so it cannot select a database. Every other
    database-ish option name is the defect.
    """
    parser = space_plan.build_standalone_parser()
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option not in {"--host", "-h", "--help"}
    }
    offending = [option for option in options if _DATABASE_NAMING.search(option)]
    assert not offending, (
        f"{offending} would let an operator name the database. The target is "
        "derived from the Global space registry by routes/admin.py::_plan_target."
    )


def test_the_flag_scan_would_catch_a_region_flag():
    """Ablation for the two scans above — written against the real bad shape."""
    assert _DATABASE_NAMING.search("--region")
    assert _DATABASE_NAMING.search("region")
    assert not _DATABASE_NAMING.search("--rolling-limit")
    assert not _DATABASE_NAMING.search("name")


# ---------------------------------------------------------------------------
# what the operator is told
# ---------------------------------------------------------------------------
def test_show_prints_the_resolved_database(driven, capsys):
    stub = RecordingApi()
    assert driven(stub, subject="show") == 0
    out = capsys.readouterr().out
    assert "evane-pg-ae.postgres.database.azure.com" in out
    assert "UNGATED" in out


def test_a_dry_run_states_that_the_gate_flips_to_fail_closed(driven, capsys):
    stub = RecordingApi()
    driven(stub, name="Evane Max")
    out = capsys.readouterr().out
    assert "FAIL-OPEN -> FAIL-CLOSED" in out


def test_a_deployment_with_the_gate_switched_off_says_so(driven, capsys):
    """The row changes nothing today if the kill switch is off — and a quiet graph
    afterwards would otherwise read as "the plan is working"."""
    stub = RecordingApi()
    original = _EFFECTS["usage_gate_enabled_on_this_deployment"]
    try:
        _EFFECTS["usage_gate_enabled_on_this_deployment"] = False
        driven(stub, name="Evane Max")
    finally:
        _EFFECTS["usage_gate_enabled_on_this_deployment"] = original
    assert "EVANE_USAGE_GATE_ENABLED is OFF" in capsys.readouterr().out


def test_a_collapsed_single_database_deployment_is_flagged(driven, capsys):
    """When one database serves both regions, "region ae" is a LABEL, not evidence
    that a row landed anywhere in particular. Say so rather than let the operator
    read the region line as proof."""
    stub = RecordingApi()
    original = _TARGET["database"]["shared_with_global"]
    try:
        _TARGET["database"]["shared_with_global"] = True
        driven(stub, subject="show")
    finally:
        _TARGET["database"]["shared_with_global"] = original
    assert "shares ONE database" in capsys.readouterr().out


def test_the_help_says_the_write_is_a_dry_run_and_env_is_not_the_database():
    text = space_plan.build_standalone_parser().format_help()
    assert "DRY RUN" in text
    assert "--env DOES NOT CHOOSE THE DATABASE" in text
    assert "UNGATED" in text


# ---------------------------------------------------------------------------
# plan tiers (services/plan_tiers.py)
# ---------------------------------------------------------------------------
def test_tier_and_use_tier_limits_send_the_tier_and_null_the_overrides(driven, capsys):
    """Moving an admin-granted row onto a tier: the row's own name and limits are
    cleared to NULL so they follow the tier's config, and the printed next command
    carries the flag so the apply sends the same body."""
    stub = RecordingApi()
    driven(stub, tier="max", extra_seats=0, use_tier_limits=True)
    assert stub.bodies[0] == {
        "tier": "max",
        "extra_seats": 0,
        "name": None,
        "rolling_window_hours": None,
        "rolling_limit_standard_tokens": None,
        "weekly_limit_standard_tokens": None,
    }
    assert "--tier 'max' --extra-seats 0 --use-tier-limits --apply" in capsys.readouterr().out


def test_use_tier_limits_refuses_a_contradictory_limit(driven):
    stub = RecordingApi()
    with pytest.raises(space_plan.SpacePlanError, match="use-tier-limits"):
        driven(stub, tier="max", use_tier_limits=True, weekly_limit_standard_tokens=1)
    assert stub.calls == []
