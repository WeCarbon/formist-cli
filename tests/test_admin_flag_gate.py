"""The ``--admin`` gate, across every command whose WHOLE surface is
platform-admin (``privileges.LEVEL_ADMIN``).

Owner's decision (C814, 2026-10-30): a command with no user-level meaning
(platform user management, the event stream, plans/alerts/usage across
spaces, ...) must refuse without ``--admin``, before any request, with one
short neutral line, exit non-zero. This is tested once per command through
the real ``fm`` entry point (``cli.main``), where the gate actually lives,
not through each module's own ``run``/``cmd_*`` (which a direct unit test
calls with no CLI dispatch at all, and so never sees this gate).

``--admin`` also has to work both before and after the subcommand, the same
duality ``--host`` already has; that is pinned separately below.
"""

from __future__ import annotations

import argparse
import urllib.request

import pytest

from formist_cli import cli, privileges, tokens

SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"

#: One parseable argv per LEVEL_ADMIN command, minus `--admin`. Positionals
#: are filled with whatever makes argparse accept the line; none of them is
#: ever read, because the gate must refuse before anything downstream runs.
#:
#: Ten commands that used to be here (instance/users/logs/space-plan/
#: usage-tokens/alerts/settings/update/airtable/whatsapp) moved to
#: curist-server's `fmdev` (C1105/C1114): none had a non-admin meaning in
#: `fm`, so this gate -- the refusal path for a command with NO member-level
#: equivalent -- no longer applies to them in `fm` at all; they are
#: LEVEL_LOCAL redirect stubs now (see `tests/test_moved_to_fmdev.py`), and
#: fmdev enforces the same gate they tested here, on the same modules.
ADMIN_ONLY_COMMANDS = {
    "messages": ["messages", "--host", "evane"],
    "participants": ["participants", "--host", "evane", "--space", SPACE, "dedupe"],
}


def test_every_level_admin_command_has_a_row_here():
    """Both directions, same shape as ``test_privileges.py``: this list must
    track the table, not quietly fall behind it."""
    admin_commands = {
        name for name, p in privileges.TABLE.items() if p.level == privileges.LEVEL_ADMIN
    }
    assert set(ADMIN_ONLY_COMMANDS) == admin_commands


@pytest.fixture
def no_socket(monkeypatch):
    """Fail the test if the gate lets a request through."""
    opened: list[str] = []

    def _traced(request, **kwargs):
        opened.append(getattr(request, "full_url", str(request)))
        raise AssertionError("the --admin gate let a request through")

    monkeypatch.setattr(urllib.request, "urlopen", _traced)
    # Irrelevant to the gate itself (it fires first), but kept absent so a
    # regression that removed the gate would fail on "no socket", not hang
    # on a real token lookup.
    monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
    monkeypatch.setattr(cli.context.tokens, "load_for", lambda env: (None, None))
    return opened


@pytest.mark.parametrize("command,argv", ADMIN_ONLY_COMMANDS.items(), ids=list(ADMIN_ONLY_COMMANDS))
def test_refuses_without_admin_before_any_request(command, argv, no_socket, capsys):
    rc = cli.main(argv)
    assert no_socket == [], f"{command} reached the network without --admin"
    assert rc != 0, f"{command} must exit non-zero without --admin"
    err = capsys.readouterr().err
    assert "--admin" in err
    assert "platform administrator" in err.lower()


@pytest.mark.parametrize("command,argv", ADMIN_ONLY_COMMANDS.items(), ids=list(ADMIN_ONLY_COMMANDS))
def test_admin_before_the_subcommand_also_satisfies_the_gate(command, argv, monkeypatch, capsys):
    """``fm --admin <command>`` must count the same as ``fm <command>
    --admin``, the same duality ``--host`` already has. Proven by getting
    PAST the gate (reaching a different, later refusal: no stored token)
    rather than by inspecting the flag directly, since the gate and the
    flag are both private to ``cli.py``."""
    name, rest = argv[0], argv[1:]
    monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
    monkeypatch.setattr(cli.context.tokens, "load_for", lambda env: (None, None))
    cli.main(["--admin", name, *rest])
    err = capsys.readouterr().err
    assert "needs --admin" not in err, (
        f"`fm --admin {command}` tripped the gate; --admin before the "
        "subcommand did not reach the namespace"
    )


# --- `requires_admin=` lets another front end's own table govern this gate too ---------
def test_requires_admin_param_overrides_which_table_the_flag_gate_consults(monkeypatch, capsys):
    """Found wiring curist-server's `fmdev` (C1105/C1114), which reuses this
    `main()` under a different `prog` and a different command table: before
    this parameter existed, the flag gate always consulted `fm`'s OWN
    `privileges.requires_admin`, a silent no-op for any caller whose command
    names never collided with `fm`'s (every evdev command, until it started
    reusing ten of fm's own names) -- so moving those ten from LEVEL_ADMIN to
    LEVEL_LOCAL in `fm`'s table, correct for `fm` where they are redirect
    stubs now, would have silently dropped the flag requirement for evdev's
    real versions of the same ten too. Proven directly: a fake table saying
    "always admin" refuses even a command `fm`'s own table has never heard
    of, and a fake table saying "never admin" waves one of fm's own real
    LEVEL_ADMIN commands straight through.
    """
    monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
    monkeypatch.setattr(cli.context.tokens, "load_for", lambda env: (None, None))

    parser = argparse.ArgumentParser(prog="fmdev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("not-a-fm-command")

    rc = cli.main(
        ["not-a-fm-command"],
        parser=parser,
        commands={"not-a-fm-command": lambda a: 0},
        check_privileges=lambda *a, **k: None,
        requires_admin=lambda name: True,
    )
    assert rc == 2
    capsys.readouterr()  # clear, so the next call's own output is checked in isolation

    rc = cli.main(
        ["participants", "--host", "evane", "--space", SPACE, "dedupe"],
        check_privileges=lambda *a, **k: None,
        requires_admin=lambda name: False,
    )
    assert "needs --admin" not in capsys.readouterr().err, (
        "a table saying this command is never admin must get past the flag "
        "gate (whatever happens next -- offline, this reaches a real "
        "network call and fails for an unrelated reason, exit code 2 "
        "included, which is why this checks the MESSAGE, not `rc`)"
    )


def test_the_refusal_names_the_callers_own_prog_not_a_hardcoded_fm():
    parser = argparse.ArgumentParser(prog="fmdev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whatsapp")
    rc, err = None, None
    import io
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = cli.main(
            ["whatsapp"],
            parser=parser,
            commands={"whatsapp": lambda a: 0},
            requires_admin=lambda name: True,
        )
    err = buf.getvalue()
    assert rc == 2
    assert "`fmdev whatsapp` needs --admin" in err
    assert "`fm whatsapp`" not in err
