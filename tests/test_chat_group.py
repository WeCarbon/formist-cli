"""Tests for ``fm chat``: the noun group ``fm chats``/``fm chat`` folded
into.

Unlike the other three noun groups, `chat` is not a NEW name a set of old
commands alias to -- `chat` already existed, bare (``fm chat [chat_id]``),
and keeps meaning exactly that; `list`/`send` are new ways to reach the
same two behaviours (`fm chats`' and the old `fm chat`'s) under one name.
So there are three shapes to cover: the new `list`, the new `send`, and
the legacy bare form, which must still work identically and only gets a
hint, never a refusal.
"""

from __future__ import annotations

import argparse

from formist_cli import chat as chat_mod
from formist_cli import chats as chats_mod
from formist_cli import cli


def _args(**over):
    base = {"subject_or_id": None, "second_arg": None}
    base.update(over)
    return argparse.Namespace(**base)


def test_list_calls_cmd_chats_with_chat_id_from_second_arg(monkeypatch):
    seen = {}
    monkeypatch.setattr(chats_mod, "cmd_chats", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_chat_grouped(_args(subject_or_id="list", second_arg="c-123"))
    assert rc == 0
    assert seen["args"].chat_id == "c-123"


def test_send_calls_cmd_chat_with_chat_id_from_second_arg(monkeypatch):
    seen = {}
    monkeypatch.setattr(chat_mod, "cmd_chat", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_chat_grouped(_args(subject_or_id="send", second_arg="c-456"))
    assert rc == 0
    assert seen["args"].chat_id == "c-456"


def test_bare_legacy_id_still_works_and_warns_but_does_not_refuse(monkeypatch, capsys):
    """`fm chat <id>` (no subject) is the form that predates `list`/`send`.
    It must keep running exactly as before -- through `chat_mod.cmd_chat`,
    the same function `send` now also reaches -- with only a hint, since
    `chat` was never deprecated, only extended.
    """
    seen = {}
    monkeypatch.setattr(chat_mod, "cmd_chat", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_chat_grouped(_args(subject_or_id="c-raw-id"))
    assert rc == 0
    assert seen["args"].chat_id == "c-raw-id"
    err = capsys.readouterr().err
    assert "fm chat <id>" in err
    assert "fm chat send <id>" in err


def test_bare_no_args_starts_a_new_chat_silently(monkeypatch, capsys):
    """Omitting the id (``fm chat`` on its own) always meant "start a new
    chat"; that is unambiguous and not deprecated, so it gets no hint.
    """
    seen = {}
    monkeypatch.setattr(chat_mod, "cmd_chat", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_chat_grouped(_args(subject_or_id=None))
    assert rc == 0
    assert seen["args"].chat_id is None
    assert capsys.readouterr().err == ""


def _alias_handler(fn):
    """See the identical helper in ``test_skill_group.py``: ``_COMMANDS`` is
    a dict literal, so the real handler is read out of the wrapper's
    closure, not re-looked-up off the module.
    """
    i = fn.__code__.co_freevars.index("handler")
    return fn.__closure__[i].cell_contents


def test_chats_alias_is_wired_to_the_real_cmd_chats():
    assert _alias_handler(cli._COMMANDS["chats"]) is chats_mod.cmd_chats


def test_chats_alias_warns_then_runs_the_handler(capsys):
    calls = []
    wrapped = cli._deprecated_alias("chats", "chat list", lambda a: calls.append(a) or 0)
    rc = wrapped(argparse.Namespace())
    assert rc == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "`fm chats`" in err
    assert "chat list" in err


def test_chat_and_chats_are_both_still_registered():
    assert "chat" in cli._COMMANDS
    assert "chats" in cli._COMMANDS
    assert cli._COMMANDS["chat"] is cli.cmd_chat_grouped
