"""Tests for the ten commands that moved to curist-server's `fmdev` (`evdev`
renamed, C1105/C1114): ``instance``, ``users``, ``logs``, ``space-plan``,
``usage-tokens``, ``alerts``, ``settings``, ``update``, ``airtable``,
``whatsapp``. None had a non-admin meaning in `fm`, so none of them run
here any more; `fm` keeps a one-release redirect stub under each name.

``messages``/``participants`` are checked too, but to prove the OPPOSITE:
they stay real commands in `fm` (they retire with C1073/C1110 instead, not
this move), so a regression that swept them in by mistake fails here.
"""

from __future__ import annotations

import urllib.request

import pytest

from formist_cli import cli, privileges

MOVED = cli._MOVED_TO_FMDEV


def test_exactly_these_ten_moved():
    assert set(MOVED) == {
        "instance", "users", "logs", "space-plan", "usage-tokens",
        "alerts", "settings", "update", "airtable", "whatsapp",
    }


@pytest.mark.parametrize("name", MOVED)
def test_each_moved_command_is_the_stub_in_the_command_table(name):
    assert cli._COMMANDS[name] is cli.cmd_moved_to_fmdev


@pytest.mark.parametrize("name", MOVED)
def test_each_moved_command_is_level_local_now(name):
    """LEVEL_LOCAL is true of the STUB (prints a message, touches no API),
    not a relaxation of what the real command needed -- fmdev enforces the
    same gate on the same modules this table once enforced for `fm`.
    """
    assert privileges.level_for(name) == privileges.LEVEL_LOCAL


@pytest.mark.parametrize("name", ["messages", "participants"])
def test_messages_and_participants_did_not_move(name):
    """These retire with C1073/C1110 instead; a change that swept them into
    the fmdev move by mistake must fail here, not pass quietly.
    """
    assert privileges.level_for(name) == privileges.LEVEL_ADMIN
    assert cli._COMMANDS[name] is not cli.cmd_moved_to_fmdev


@pytest.fixture
def no_socket(monkeypatch):
    def _explode(*a, **kw):
        raise AssertionError("the stub reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)


@pytest.mark.parametrize("name", MOVED)
def test_running_the_old_command_redirects_without_touching_the_network(
    name, no_socket, capsys
):
    rc = cli.main([name])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"`fm {name}`" in err
    assert f"fmdev {name}" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["users", "--limit", "5"],
        ["whatsapp", "--env", "ae"],
        ["instance", "--host", "evane"],
        ["settings", "set", "--host", "evane", "scheduler.disabled=true"],
    ],
    ids=["flag-first-positional", "flag-first-whatsapp", "flag-first-instance", "subject-then-flags"],
)
def test_any_shape_of_the_old_invocation_redirects_rather_than_erroring(
    argv, no_socket, capsys
):
    """A naive `nargs=argparse.REMAINDER` positional does not actually
    absorb a LEADING option-looking token (argparse classifies it as an
    unmatched option before any positional claims it), so `fm whatsapp
    --env ae` -- this command's own historical example -- used to raise
    "unrecognized arguments" instead of showing the redirect. Guards that
    regression directly, for every shape of leading token.
    """
    rc = cli.main(argv)
    assert rc == 1
    err = capsys.readouterr().err
    assert f"`fm {argv[0]}`" in err


def test_admin_flag_does_not_change_the_redirect(no_socket, capsys):
    """`fm --admin users` must not somehow reach the real (gone) command
    just because the strongest credential was asked for.
    """
    rc = cli.main(["--admin", "users"])
    assert rc == 1
    assert "`fm users`" in capsys.readouterr().err


def test_add_instance_parser_and_add_update_parser_are_public_for_fmdev():
    """fmdev (curist-server) builds its OWN `instance`/`update` subcommands
    by calling these directly, reusing the exact parser `fm` used to have
    rather than a second copy of it; this is the one-time smoke test that
    they still build a working parser on their own, detached from `fm`'s
    own (stubbed) registration of the same names.
    """
    import argparse

    from formist_cli.cli import add_instance_parser, add_update_parser

    holder = argparse.ArgumentParser(prog="smoke")
    sub = holder.add_subparsers(dest="command")
    add_instance_parser(sub)
    add_update_parser(sub)

    parsed = holder.parse_args(["instance", "--host", "evane"])
    assert parsed.command == "instance"
    assert parsed.host == "evane"

    parsed = holder.parse_args(["update", "ops.json", "--host", "evane", "--space", "s-1"])
    assert parsed.command == "update"
    assert parsed.payload == "ops.json"
