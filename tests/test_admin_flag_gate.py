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

import urllib.request

import pytest

from formist_cli import cli, privileges, tokens

SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"

#: One parseable argv per LEVEL_ADMIN command, minus `--admin`. Positionals
#: are filled with whatever makes argparse accept the line; none of them is
#: ever read, because the gate must refuse before anything downstream runs.
ADMIN_ONLY_COMMANDS = {
    "instance": ["instance", "--host", "evane"],
    "users": ["users", "--host", "evane"],
    "logs": ["logs", "--host", "evane"],
    "space-plan": ["space-plan", "--host", "evane", "--space", SPACE],
    "usage-tokens": ["usage-tokens", "--host", "evane"],
    "alerts": ["alerts", "--host", "evane"],
    "messages": ["messages", "--host", "evane"],
    "settings": ["settings", "--host", "evane"],
    "update": ["update", "--host", "evane", "--space", SPACE, "payload.json"],
    "airtable": ["airtable", "--host", "evane", "--space", SPACE, "direction"],
    "whatsapp": ["whatsapp", "--host", "evane"],
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
