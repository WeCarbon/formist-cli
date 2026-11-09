"""``fm settings``: argument parsing and rendering, offline.

The HTTP half is ``tests/test_admin_settings_route.py``; this covers the part an
operator types, and in particular the refusals, since a CLI that guesses at
``scheduler.disabled=maybe`` is how someone ends up believing a switch is off.

Run: uv run pytest tests/test_formist_cli_settings.py -q
"""

from __future__ import annotations

import pytest

from formist_cli import cli
from formist_cli import settings as settings_mod
from formist_cli.settings import SettingsError, canonical_subject, parse_assignments


def test_fm_settings_is_now_a_redirect_to_fmdev():
    """`settings` moved to curist-server's `fmdev` (C1105/C1114): `fm
    settings` is a one-release stub (`cli.cmd_moved_to_fmdev`), not
    `settings_mod.run` any more. The real parser this test used to assert
    on now lives only in fmdev, built by calling `settings_mod.add_parser`
    directly (unchanged -- this module's own parser-building code is not
    touched by the move, only `fm`'s registration of it)."""
    assert cli._COMMANDS["settings"] is cli.cmd_moved_to_fmdev
    parsed = cli.build_parser().parse_args(
        ["settings", "set", "--host", "evane", "scheduler.disabled=true"]
    )
    assert parsed.command == "settings"


def test_settings_error_is_caught_by_the_shared_dispatch():
    """``SettingsError`` was never in ``USER_ERRORS`` (found while wiring
    `settings` into curist-server's `fmdev`, which calls `settings.run`
    directly through this same shared `cli.main`/`USER_ERRORS` machinery):
    a typo like `fm settings set scheduler.disabled=maybe` would have
    raised past every `except` clause and printed a bare traceback instead
    of the clean "error: ..." line every other refusal gets. Asserted
    structurally (whether the tuple names the class), since `settings`
    itself no longer runs through `fm`'s own dispatch to exercise this
    end-to-end; `fmdev`'s own tests exercise it live."""
    assert SettingsError in cli.USER_ERRORS


@pytest.mark.parametrize("subject", ["show", "set", "clear"])
def test_every_documented_subject_is_accepted(subject):
    assert canonical_subject(subject) == subject


def test_no_subject_means_show():
    assert canonical_subject(None) == "show"


def test_an_unknown_subject_is_refused():
    with pytest.raises(SettingsError, match="unknown subcommand"):
        canonical_subject("enable")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_the_truthy_vocabulary(raw):
    assert parse_assignments([f"a.b={raw}"]) == {"a.b": True}


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_the_falsy_vocabulary(raw):
    assert parse_assignments([f"a.b={raw}"]) == {"a.b": False}


def test_a_value_outside_both_vocabularies_is_refused_not_guessed():
    with pytest.raises(SettingsError, match="is not a boolean"):
        parse_assignments(["scheduler.disabled=maybe"])


def test_an_assignment_without_an_equals_sign_is_refused():
    with pytest.raises(SettingsError, match="expected field=value"):
        parse_assignments(["scheduler.disabled"])


def test_show_marks_the_fields_the_file_decides():
    rendered = settings_mod.render_show(
        {
            "instance": "ca-evane-api-ae--0000021",
            "file": "/app/local/admin_settings.json",
            "file_exists": True,
            "scope_note": "per replica and per region",
            "fields": [
                {"field": "scheduler.disabled", "value": True, "overridden": True},
                {"field": "mcp.enabled", "value": False, "overridden": False},
            ],
        }
    )
    assert "* scheduler.disabled" in rendered
    assert "  mcp.enabled" in rendered
    assert "ca-evane-api-ae--0000021" in rendered
    assert "per replica and per region" in rendered


def test_patch_renders_each_transition():
    rendered = settings_mod.render_patch(
        {
            "instance": "host-1",
            "file": "/app/local/admin_settings.json",
            "applied": [{"field": "scheduler.disabled", "before": False, "after": True}],
            "unchanged": ["mcp.enabled"],
            "cleared": [],
        }
    )
    assert "scheduler.disabled: false -> true" in rendered
    assert "mcp.enabled: unchanged" in rendered


def test_patch_says_so_when_nothing_moved():
    assert "(nothing changed)" in settings_mod.render_patch(
        {"instance": "h", "file": "f", "applied": [], "unchanged": [], "cleared": []}
    )
