"""``fm chat`` — the interactive REPL against the server-side agent.

WHAT THESE TESTS ARE FOR
-------------------------
``formist_cli/chat.py`` reuses ``formist_cli.chats`` for everything about READING a chat
(that module's own tests already cover completeness/privacy), and reuses
``formist_cli.logs.parse_sse`` for framing raw SSE text into records (that module's
own tests already cover multi-line data fields and the unterminated-frame
case). What is new here, and what these tests are built around, is the layer
on top of both: turning a run's SSE records into terminal output WITHOUT ever
reporting a dropped or partial stream as a normal, finished turn.

``TestConsumeTurnFraming`` exercises :func:`consume_turn` directly, over lists
of already-parsed records — the pure state machine, same style as
``tests/test_formist_cli_logs.py``'s ``TestSSEFrames``/``TestSilenceIsNotAmbiguous``.

``TestRunTurnEndToEnd`` drives :func:`run_turn` over FAKE raw SSE text through
a fake ``stream_fn`` — this is the one that proves the "partial SSE frame must
not print as a complete turn" property end to end: a stream that stops
mid-frame, with no terminating blank line and no terminal event ever having
arrived, must not exit quietly. ``parse_sse``'s own docstring says an
unterminated frame is still emitted (so data is never silently dropped); the
property under test here is what THIS module does with that emitted-but-junk
frame — it must not mistake it for a legitimate token or a finished run.
"""

from __future__ import annotations

import contextlib

import pytest

from formist_cli.chat import (
    OUT,
    STATE,
    ChatsError,
    TurnState,
    build_run_body,
    consume_turn,
    create_chat,
    render_incomplete,
    run_turn,
)
from formist_cli.logs import parse_sse
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CHAT = "7f3a1c22-0000-4000-8000-000000000001"


def _frame(event: str, data: dict | None = None, *, terminate: bool = True) -> list[str]:
    """SSE wire lines for one record, in ``formist_cli.logs.parse_sse``'s vocabulary."""
    import json as _json

    lines = [f"event: {event}"]
    if data is not None:
        lines.append(f"data: {_json.dumps(data)}")
    if terminate:
        lines.append("")
    return lines


class TestConsumeTurnFraming:
    """The pure state machine, driven over already-parsed records."""

    def test_tokens_stream_as_they_arrive(self):
        state = TurnState(chat_id=CHAT)
        records = [
            {"event": "on_agent_token", "data": {"delta": "Hel"}},
            {"event": "on_agent_token", "data": {"delta": "lo"}},
            {"event": "on_agent_run_completed", "data": {"status": "COMPLETED"}},
        ]
        out = list(consume_turn(state, records))
        assert out == [(OUT, "Hel"), (OUT, "lo")]
        assert state.terminal_seen is True
        assert state.status == "COMPLETED"

    def test_final_message_is_not_reprinted_once_tokens_were_shown(self):
        """Tokens are best-effort UX; on_chat_message carries the AUTHORITATIVE
        text (services/agent_run.py). Printing both would duplicate the reply."""
        state = TurnState(chat_id=CHAT)
        records = [
            {"event": "on_agent_token", "data": {"delta": "hi"}},
            {
                "event": "on_chat_message",
                "data": {"role": "ASSISTANT", "content": "hi there"},
            },
            {"event": "on_agent_run_completed", "data": {"status": "COMPLETED"}},
        ]
        out = list(consume_turn(state, records))
        assert out == [(OUT, "hi")]

    def test_final_message_is_the_fallback_when_no_tokens_ever_streamed(self):
        """A non-streaming backend (or a run with zero deltas) must still show
        its answer — it lives only in on_chat_message in that case."""
        state = TurnState(chat_id=CHAT)
        records = [
            {"event": "on_agent_run_started", "data": {"model": "x"}},
            {
                "event": "on_chat_message",
                "data": {"role": "ASSISTANT", "content": "the whole answer"},
            },
            {"event": "on_agent_run_completed", "data": {"status": "COMPLETED"}},
        ]
        out = list(consume_turn(state, records))
        assert (OUT, "the whole answer") in out

    def test_a_retry_resets_the_token_flag_and_marks_the_seam(self):
        """services/agent_events.py: a retry re-announces run_started and means
        every token so far belongs to an abandoned attempt. This terminal
        cannot un-print stale text, so it must fall back to the retried run's
        final message rather than assume the stale tokens already covered it."""
        state = TurnState(chat_id=CHAT)
        records = [
            {"event": "on_agent_token", "data": {"delta": "stale partial"}},
            {
                "event": "on_agent_run_started",
                "data": {"retry_attempt": 1, "reason": "rate limited"},
            },
            {
                "event": "on_chat_message",
                "data": {"role": "ASSISTANT", "content": "the real answer"},
            },
            {"event": "on_agent_run_completed", "data": {"status": "COMPLETED"}},
        ]
        out = list(consume_turn(state, records))
        assert any(kind is STATE and "retrying" in text for kind, text in out)
        assert (OUT, "the real answer") in out

    def test_run_failed_marks_terminal_and_carries_the_error(self):
        state = TurnState(chat_id=CHAT)
        out = list(
            consume_turn(state, [{"event": "on_agent_run_failed", "data": {"error": "boom"}}])
        )
        assert state.terminal_seen is True
        assert state.status == "FAILED"
        assert state.error == "boom"
        assert any(kind is STATE and "boom" in text for kind, text in out)

    def test_cancelled_is_terminal_but_not_an_error(self):
        state = TurnState(chat_id=CHAT)
        list(consume_turn(state, [{"event": "on_agent_run_completed", "data": {"status": "CANCELLED"}}]))
        assert state.terminal_seen is True
        assert state.status == "CANCELLED"
        assert state.error is None

    def test_nothing_after_a_terminal_event_is_consumed(self):
        """A stray event after the run's own end (should not happen, but a
        client trusting only what IT decided is terminal is the safer read)."""
        state = TurnState(chat_id=CHAT)
        records = [
            {"event": "on_agent_run_completed", "data": {"status": "COMPLETED"}},
            {"event": "on_agent_token", "data": {"delta": "should not appear"}},
        ]
        out = list(consume_turn(state, records))
        assert out == []

    def test_tool_events_are_state_not_output(self):
        state = TurnState(chat_id=CHAT)
        records = [
            {
                "event": "on_agent_tool_start",
                "data": {"tool": "search_items", "tool_id": "t1", "args": {}},
            },
            {
                "event": "on_agent_tool_end",
                "data": {"tool": "search_items", "tool_id": "t1", "ok": True, "result": {}},
            },
        ]
        out = list(consume_turn(state, records))
        assert all(kind is STATE for kind, _ in out)
        assert any("search_items" in text for _, text in out)

    def test_malformed_data_does_not_crash_or_emit(self):
        """A record whose 'data' is not a dict (parse_sse falls back to raw
        text when a frame's JSON does not parse) must be ignored, not raise."""
        state = TurnState(chat_id=CHAT)
        out = list(consume_turn(state, [{"event": "on_agent_token", "data": "not-a-dict"}]))
        assert out == []
        assert state.terminal_seen is False


class TestRenderIncomplete:
    def test_names_the_chat_and_the_missing_events(self):
        state = TurnState(chat_id=CHAT)
        text = render_incomplete(state)
        assert CHAT in text
        assert "on_agent_run_completed" in text
        assert "PARTIAL" in text


class TestBuildRunBody:
    def test_content_only_by_default(self):
        assert build_run_body("hello") == {"content": "hello"}

    def test_model_and_effort_ride_along_only_when_set(self):
        body = build_run_body("hi", model="model-x", effort="high")
        assert body == {"content": "hi", "model": "model-x", "effort": "high"}

    def test_empty_model_or_effort_stay_absent_not_null(self):
        body = build_run_body("hi", model="", effort=None)
        assert "model" not in body
        assert "effort" not in body


class TestCreateChat:
    def test_posts_to_the_space_chats_endpoint_and_returns_it(self):
        captured = {}

        def fake_post(target, path, body, *, token=None, **kwargs):
            captured["path"] = path
            captured["body"] = body
            captured["token"] = token
            return {"id": "new-chat-id", "title": "New chat"}

        chat = create_chat(LOCAL, SPACE, token="tok", post=fake_post)
        assert chat["id"] == "new-chat-id"
        assert captured["path"] == f"/projects/{SPACE}/chats"
        assert captured["token"] == "tok"
        assert "title" not in captured["body"]  # no client-chosen title

    def test_model_is_forwarded_when_given(self):
        captured = {}

        def fake_post(target, path, body, *, token=None, **kwargs):
            captured["body"] = body
            return {"id": "x"}

        create_chat(LOCAL, SPACE, token="tok", model="model-x", post=fake_post)
        assert captured["body"] == {"model": "model-x"}

    def test_a_response_with_no_id_is_refused_not_rendered(self):
        with pytest.raises(ChatsError):
            create_chat(LOCAL, SPACE, token="tok", post=lambda *a, **k: {"title": "oops"})


def _fake_stream(raw_lines, captured=None):
    @contextlib.contextmanager
    def _stream(target, path, body, *, token=None, params=None, read_timeout=None):
        if captured is not None:
            captured.update(target=target, path=path, body=body, token=token,
                             read_timeout=read_timeout)
        yield iter(raw_lines)

    return _stream


class TestRunTurnEndToEnd:
    """Raw SSE TEXT in, printed output + TurnState out — the whole path a real
    turn takes, minus the socket."""

    def test_a_complete_run_prints_the_reply_and_reports_no_warning(self, capsys):
        lines = [
            *_frame("on_agent_run_started", {"model": "x"}),
            *_frame("on_agent_token", {"delta": "Hello"}),
            *_frame("on_agent_token", {"delta": " world"}),
            *_frame("on_agent_run_completed", {"status": "COMPLETED"}),
        ]
        state = run_turn(
            LOCAL, SPACE, CHAT, "hi", token="tok", stream_fn=_fake_stream(lines)
        )
        captured = capsys.readouterr()
        assert captured.out == "Hello world\n"
        assert "PARTIAL" not in captured.err
        assert state.terminal_seen is True

    def test_a_connection_that_stops_mid_frame_is_reported_incomplete_not_done(self, capsys):
        """The property this module exists for: a partial final frame (no
        terminating blank line — the stream just stops, as it would on a
        dropped connection) must not read as a normal, finished turn."""
        lines = [
            *_frame("on_agent_token", {"delta": "partial reply"}),
            # A second frame cut off mid-flight: header line present, no data
            # line, no terminating blank line — exactly what a socket dying
            # between two writes looks like on the wire.
            "event: on_agent_token",
        ]
        state = run_turn(
            LOCAL, SPACE, CHAT, "hi", token="tok", stream_fn=_fake_stream(lines)
        )
        captured = capsys.readouterr()
        assert "partial reply" in captured.out
        assert state.terminal_seen is False
        assert "PARTIAL" in captured.err
        assert "on_agent_run_completed" in captured.err

    def test_a_stream_with_no_events_at_all_is_reported_incomplete(self, capsys):
        state = run_turn(LOCAL, SPACE, CHAT, "hi", token="tok", stream_fn=_fake_stream([]))
        captured = capsys.readouterr()
        assert state.terminal_seen is False
        assert "PARTIAL" in captured.err

    def test_the_run_body_reaches_the_stream_call(self):
        captured = {}
        lines = _frame("on_agent_run_completed", {"status": "COMPLETED"})
        run_turn(
            LOCAL, SPACE, CHAT, "hello there",
            token="tok", model="model-x", effort="high",
            stream_fn=_fake_stream(lines, captured),
        )
        assert captured["body"] == {
            "content": "hello there", "model": "model-x", "effort": "high",
        }
        assert captured["path"] == f"/projects/{SPACE}/chats/{CHAT}/run"
        assert captured["token"] == "tok"


class TestParseSseIsReusedNotReimplemented:
    """``consume_turn`` must compose with the SAME frame parser ``fm logs``
    uses — a second, subtly different SSE parser next to it is exactly the
    two-lanes-one-abstraction failure the project rules warns about."""

    def test_a_multi_line_data_field_still_arrives_as_one_record(self):
        # parse_sse joins multiple `data:` lines with '\n' before decoding —
        # proven here by round-tripping a run through the real function this
        # module imports, not a copy of it.
        lines = ['event: on_agent_token', 'data: {"delta":', 'data: "ok"}', ""]
        records = list(parse_sse(lines))
        assert records == [{"event": "on_agent_token", "data": {"delta": "ok"}}]
