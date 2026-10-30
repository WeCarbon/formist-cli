"""Tests for ``fm chat --message``: one non-interactive turn, streamed
compactly, with a final exit code and an optional JSON summary.

``consume_turn``/``run_turn`` (the state machine and the streaming loop)
are proven end to end elsewhere, with a fake ``stream_fn`` driven directly
into ``run_turn``. This file is the layer on top: ``cmd_chat``'s dispatch
to non-interactive mode, never entering the REPL, never creating a second
chat when one was given, and turning the run's outcome into an exit code
plus (with ``--json``) the summary object the module's own docstring
promises: ``{chat_id, tool_calls, failed, final_message}``.
"""

from __future__ import annotations

import contextlib
import json as json_mod

from formist_cli import chat as chat_mod
from formist_cli import context as context_mod
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CHAT = "7f3a1c22-0000-4000-8000-000000000001"


def _frame(event: str, data: dict | None = None, *, terminate: bool = True) -> list[str]:
    lines = [f"event: {event}"]
    if data is not None:
        lines.append(f"data: {json_mod.dumps(data)}")
    if terminate:
        lines.append("")
    return lines


def _fake_stream(raw_lines, captured: dict | None = None):
    @contextlib.contextmanager
    def _stream(target, path, body, *, token=None, params=None, read_timeout=None):
        if captured is not None:
            captured.update(target=target, path=path, body=body, token=token, read_timeout=read_timeout)
        yield iter(raw_lines)

    return _stream


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _chat_args(**overrides):
    defaults = dict(
        env="local", space=SPACE, chat_id=None, model=None, effort=None,
        message=None, json=False,
    )
    defaults.update(overrides)
    return _Args(**defaults)


def _patch_context(monkeypatch):
    monkeypatch.setattr(
        context_mod, "resolve",
        lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
    )


class TestMessageCreatesAChatWhenNoneGiven:
    def test_a_new_chat_is_created_then_the_turn_runs_against_it(self, monkeypatch, capsys):
        _patch_context(monkeypatch)
        posted = {}

        def fake_post(target, path, body, *, token=None, **kw):
            posted["path"] = path
            return {"id": "new-chat-id"}

        monkeypatch.setattr(chat_mod.api, "post", fake_post)

        lines = [
            *_frame("on_agent_token", {"delta": "hello"}),
            *_frame("on_agent_run_completed", {"status": "COMPLETED"}),
        ]
        captured: dict = {}
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines, captured))

        rc = chat_mod.cmd_chat(_chat_args(message="hi there"))

        assert rc == 0
        assert posted["path"] == f"/projects/{SPACE}/chats"
        assert captured["path"] == f"/projects/{SPACE}/chats/new-chat-id/run"
        assert capsys.readouterr().out == "hello\n"


class TestMessageContinuesAnExistingChatWithoutCreatingANewOne:
    def test_no_post_to_create_a_chat_when_an_id_was_given(self, monkeypatch, capsys):
        _patch_context(monkeypatch)

        def fail_post(*a, **k):
            raise AssertionError("must not create a new chat when --message names one")

        monkeypatch.setattr(chat_mod.api, "post", fail_post)
        lines = _frame("on_agent_run_completed", {"status": "COMPLETED"})
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))
        assert rc == 0


class TestMessageHoldsTheStreamOpenWithNoMeaningfulTimeout:
    def test_the_non_interactive_read_timeout_reaches_the_stream_call(self, monkeypatch):
        """A real run can take an hour, and the server ties its life to this
        client staying connected, so --message must not reuse the REPL's
        short interactive timeout."""
        _patch_context(monkeypatch)
        lines = _frame("on_agent_run_completed", {"status": "COMPLETED"})
        captured: dict = {}
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines, captured))

        chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))

        assert captured["read_timeout"] == chat_mod.NON_INTERACTIVE_READ_TIMEOUT
        assert captured["read_timeout"] != chat_mod.READ_TIMEOUT


class TestMessageJsonSummary:
    def test_the_summary_shape_matches_the_documented_contract(self, monkeypatch, capsys):
        _patch_context(monkeypatch)
        lines = [
            *_frame("on_agent_tool_start", {"tool": "search_items", "args": {}}),
            *_frame("on_agent_tool_end", {"tool": "search_items", "ok": True}),
            *_frame("on_chat_message", {"role": "ASSISTANT", "content": "the answer"}),
            *_frame("on_agent_run_completed", {"status": "COMPLETED"}),
        ]
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi", json=True))

        assert rc == 0
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload == {
            "chat_id": CHAT,
            "tool_calls": 1,
            "failed": False,
            "final_message": "the answer",
        }

    def test_streamed_tokens_are_what_final_message_reports_when_present(self, monkeypatch, capsys):
        """final_message must be the SAME text a non-JSON run streams to
        stdout: not a second, independently-derived copy of it."""
        _patch_context(monkeypatch)
        lines = [
            *_frame("on_agent_token", {"delta": "Hello"}),
            *_frame("on_agent_token", {"delta": " world"}),
            *_frame("on_agent_run_completed", {"status": "COMPLETED"}),
        ]
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi", json=True))

        assert rc == 0
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["final_message"] == "Hello world"


# ===========================================================================
# The required failure-path test: a FAILED run exits non-zero, both plain and
# --json.
# ===========================================================================
class TestMessageFailedRunExitsNonZero:
    def test_on_agent_run_failed_is_reported_and_non_zero(self, monkeypatch, capsys):
        _patch_context(monkeypatch)
        lines = _frame("on_agent_run_failed", {"error": "boom"})
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))

        assert rc == 1

    def test_json_failed_flag_is_true_on_a_failed_run(self, monkeypatch, capsys):
        _patch_context(monkeypatch)
        lines = _frame("on_agent_run_failed", {"error": "boom"})
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi", json=True))

        assert rc == 1
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["failed"] is True

    def test_a_successful_run_is_the_control_exit_code_zero(self, monkeypatch, capsys):
        """Without this, rc==1 above could just mean --message always fails."""
        _patch_context(monkeypatch)
        lines = _frame("on_agent_run_completed", {"status": "COMPLETED"})
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))
        assert rc == 0

    def test_a_dropped_stream_with_no_terminal_event_is_also_non_zero(self, monkeypatch, capsys):
        """Not FAILED, but not a success either: a run whose outcome is
        genuinely unknown must not exit 0."""
        _patch_context(monkeypatch)
        lines = list(_frame("on_agent_token", {"delta": "partial"}))
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))
        assert rc == 1


class TestReplIsNeverEnteredWithMessage:
    def test_the_repl_loop_is_not_called(self, monkeypatch, capsys):
        _patch_context(monkeypatch)

        def fail_repl(*a, **k):
            raise AssertionError("the REPL must not run when --message is given")

        monkeypatch.setattr(chat_mod, "_repl", fail_repl)
        lines = _frame("on_agent_run_completed", {"status": "COMPLETED"})
        monkeypatch.setattr(chat_mod.api, "stream_post", _fake_stream(lines))

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message="hi"))
        assert rc == 0

    def test_the_repl_IS_called_when_message_is_absent_the_control(self, monkeypatch):
        """Proves the monkeypatch above is meaningful: REPL still runs, and
        runs normally, on the ordinary (interactive) path."""
        _patch_context(monkeypatch)
        called = {}

        def fake_repl(target, space, chat_id, **kw):
            called["yes"] = True
            return 0

        monkeypatch.setattr(chat_mod, "_repl", fake_repl)
        monkeypatch.setattr(chat_mod, "get_chat", lambda *a, **k: {"id": CHAT, "kind": "USER_CHAT"})
        monkeypatch.setattr(chat_mod, "fetch_messages", lambda *a, **k: type("T", (), {"render": lambda self: ""})())

        rc = chat_mod.cmd_chat(_chat_args(chat_id=CHAT, message=None))
        assert rc == 0
        assert called.get("yes") is True
