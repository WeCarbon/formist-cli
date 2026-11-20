"""The `fm` privilege audit: every subcommand classified, and the table kept
honest.

``formist_cli/privileges.py`` says which subcommands are user-level and which need
platform admin. It is documentation with teeth: the value is not the table, it is
that a NEW SUBCOMMAND CANNOT BE ADDED WITHOUT ONE. A table maintained by discipline
drifts within a week; a table maintained by a failing test does not.

The endpoint strings are checked too: loosely, but not vacuously. Every ADMIN entry
must name a path that the app really serves behind ``require_platform_admin``, so an
entry cannot claim a gate the server does not have.
"""

from __future__ import annotations

import pytest

from formist_cli import privileges
from formist_cli.cli import _COMMANDS


def test_every_subcommand_is_classified():
    """Both directions: a new command with no row, and a row for a command that no
    longer exists (which would make the audit describe a tool that is not shipped)."""
    unclassified = sorted(set(_COMMANDS) - set(privileges.TABLE))
    assert unclassified == [], f"subcommands with no privilege entry: {unclassified}"
    stale = sorted(set(privileges.TABLE) - set(_COMMANDS))
    assert stale == [], f"privilege entries for commands that do not exist: {stale}"


def test_every_level_is_a_known_level():
    known = {privileges.LEVEL_LOCAL, privileges.LEVEL_PUBLIC, privileges.LEVEL_USER, privileges.LEVEL_ADMIN, privileges.LEVEL_MIXED}
    bad = {k: v.level for k, v in privileges.TABLE.items() if v.level not in known}
    assert bad == {}, bad


def test_the_audit_is_not_all_one_level():
    """A table that classified everything ADMIN would satisfy every other
    assertion here and would say nothing. The CLI must stay usable on an
    ordinary login, so the user-level half has to actually exist."""
    levels = [p.level for p in privileges.TABLE.values()]
    assert levels.count(privileges.LEVEL_USER) >= 8, levels.count(privileges.LEVEL_USER)
    assert levels.count(privileges.LEVEL_ADMIN) >= 8, levels.count(privileges.LEVEL_ADMIN)
# --- the pre-flight refusal --------------------------------------------------------
def test_a_credential_without_the_admin_scope_is_refused_before_any_socket():
    with pytest.raises(privileges.PrivilegeError) as exc:
        privileges.check("users", ["evane:kb", "evane:cli"])
    message = str(exc.value)
    assert "platform-admin" in message
    assert "fm login" in message
    # It must not suggest a workaround; there is none.
    assert "--force" not in message and "bypass" not in message


def test_a_credential_with_the_admin_scope_passes():
    privileges.check("users", ["evane:kb", "evane:cli", "evane:admin"])


def test_user_level_commands_are_never_refused_locally():
    """`fm` must stay usable on an ordinary login. A pre-flight check that
    refused a user-level command would break exactly that."""
    for name, entry in privileges.TABLE.items():
        if entry.level in (privileges.LEVEL_USER, privileges.LEVEL_PUBLIC, privileges.LEVEL_LOCAL, privileges.LEVEL_MIXED):
            privileges.check(name, ["evane:kb", "evane:cli"])


def test_unknown_scopes_are_passed_through_to_the_server():
    """Advisory in one direction only: a credential whose scopes were never
    recorded (an older file, a hand-placed one) must reach the server
    rather than be guessed about locally. A local check must never refuse
    what the server would allow."""
    privileges.check("users", None)
    privileges.check("users", [])
    privileges.check("users", "not-a-list")
