"""``fm users grants`` / ``fm connectors grants`` — the renderer must not mislead.

Run: uv run pytest tests/test_formist_cli_grants_render.py -q

There is no HTTP here and that is deliberate: the defect available in these two
commands is not the request, it is a table that is TRUE about the JSON it received
and FALSE about what the reader concludes. Three ways that happens, one test each.
"""

from __future__ import annotations

from formist_cli import connectors as connectors_mod
from formist_cli import users as users_mod
from formist_cli.cli_support import render_grant_rows


def _row(**over) -> dict:
    base = {
        "provider": "AIRTABLE",
        "identity": "Delegates",
        "account": None,
        "auth_status": "ok",
        "scopes": ["a", "b"],
        "scopes_recorded": True,
        "expires_at": None,
        "expiry_kind": "none_recorded",
    }
    base.update(over)
    return base


def test_a_provider_that_records_no_scopes_is_not_shown_as_granted_nothing():
    """⚠️ The distinction the whole panel turns on.

    An API-key connector records no scopes. Rendering that as ``0`` — the same cell
    an empty-but-recorded grant gets — would tell an operator that a Fireflies key
    may do nothing, when it may do everything that API allows.
    """
    none_recorded = render_grant_rows([_row(scopes=[], scopes_recorded=False)])
    empty_but_recorded = render_grant_rows([_row(scopes=[], scopes_recorded=True)])
    assert none_recorded != empty_but_recorded
    assert " - " in none_recorded
    assert " 0 " in empty_but_recorded


def test_an_access_token_expiry_is_labelled_not_printed_bare():
    """A silently-refreshed ~1h token must never read as a deadline.

    Printed bare it would show every healthy Google/Airtable connector as expiring
    within the hour, permanently — an alarm that is always on, which is an alarm
    nobody reads.
    """
    out = render_grant_rows(
        [_row(expires_at="2026-09-06T10:00:00+00:00", expiry_kind="access_token")]
    )
    assert "auto-renewed" in out


def test_an_unrecorded_expiry_does_not_claim_the_grant_never_expires():
    out = render_grant_rows([_row(expiry_kind="none_recorded")])
    assert "(not recorded)" in out
    assert "never" not in out.lower()


def test_a_provider_with_no_recorded_auth_status_is_not_shown_as_ok():
    """``auth_status: null`` means nothing measured it — not that it is healthy."""
    out = render_grant_rows([_row(auth_status=None)])
    assert "ok" not in out.split("\n")[-1]


def test_an_empty_grant_list_says_so_rather_than_printing_nothing():
    """Blank output is indistinguishable from a command that failed quietly."""
    assert "no grants" in render_grant_rows([])


def test_user_grants_report_names_the_regions_it_searched():
    """Personal connectors are region-local, so an empty table without the region
    list cannot be told apart from a fan-out that only looked in one database."""
    out = users_mod.render_grants({"items": [], "regions_scanned": ["global", "ae"]})
    assert "global" in out and "ae" in out


def test_connector_grants_report_names_the_space_it_read():
    """The command takes a UUID; a table with no space name on it cannot be checked
    against the space the operator meant."""
    out = connectors_mod.render_grants(
        {"space": {"id": "s-1", "name": "GFSS 2026", "region": "ae"}, "items": []}
    )
    assert "GFSS 2026" in out and "ae" in out


def test_both_commands_share_one_renderer():
    """⚠️ A property, not a behaviour: there must be exactly ONE grant-table
    implementation. Two would drift on precisely the columns above, and nothing in
    the tree would compare them — the failure mode the project rules documents for
    independently-invented helpers."""
    src_users = users_mod.render_grants.__code__.co_names
    src_conn = connectors_mod.render_grants.__code__.co_names
    assert "render_grant_rows" in src_users
    assert "render_grant_rows" in src_conn


def test_connectors_default_subject_is_still_the_plain_list():
    """`fm connectors` predates this change and must keep doing what it did.

    A new positional that displaced the default would break every existing
    invocation, and the breakage would read as a connector fault rather than a CLI
    change.
    """
    from formist_cli import cli

    assert connectors_mod.DEFAULT_SUBJECT == "list"
    # And the parser really does leave it unset, so the dispatcher's default is the
    # one that runs — asserting the constant alone would not prove that.
    assert cli.build_parser().parse_args(["connectors"]).subject is None
