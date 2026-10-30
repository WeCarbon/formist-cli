"""``fm chat``: an interactive REPL against the server-side agent.

``fm chats`` (plural) reads history: it lists a space's chats and
prints a transcript. This module is the other half: it posts a turn
and streams the reply as it arrives, over the same route the browser
chat uses (an SSE endpoint). Fetching a chat and paging its transcript
are reused from :mod:`formist_cli.chats`, not re-implemented here.

Scope is the same as ``fm chats``: this command can continue a chat
the current session owns, or start a new one (always user-owned). It
cannot open another member's private chat, and it cannot run a turn
on a workspace chat someone else's automation created; those requests
are rejected server-side. There is no resolve-by-title, since chat
titles are auto-generated and duplicate heavily.

Token events are best-effort; the persisted final message is
authoritative. A dropped connection mid-run can leave the terminal
holding a partial reply with no visible sign that it is partial, so
this module tracks whether a terminal event
(``on_agent_run_completed`` / ``on_agent_run_failed``) was actually
seen (:class:`TurnState`) and says so loudly
(:func:`render_incomplete`) when the stream ends without one.

``parse_sse`` is reused from :mod:`formist_cli.logs` rather than
re-implemented, since it already handles the one hazard that matters
here: a multi-line SSE ``data:`` field is only complete once its
terminating blank line arrives.

Stdlib only: this module must not add a runtime dependency.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from formist_cli import api
from formist_cli.chats import ChatsError, fetch_messages, get_chat
from formist_cli.logs import OUT, STATE, Kind, parse_sse
from formist_cli.targets import Target, require_space_id

#: The two events that end a run. Seeing one is the only way to tell
#: "the turn finished" from "the connection stopped handing us bytes".
TERMINAL_EVENTS = frozenset({"on_agent_run_completed", "on_agent_run_failed"})

#: SSE read timeout for one interactive turn. Sized generously: the
#: endpoint pings periodically, but a turn can also sit silent for a
#: long tool call (a search, a document read) with nothing on the
#: wire in between, and that silence is not a sign of anything wrong.
READ_TIMEOUT = 300.0

#: SSE read timeout for ``fm chat --message``, effectively unbounded.
#: A real agent run can take an hour, and the server ends the run the
#: moment this client's socket disconnects, so a timeout sized for an
#: interactive turn would abort a run that was working fine. Large
#: but finite because Python's socket layer rejects an actual
#: infinite float.
NON_INTERACTIVE_READ_TIMEOUT = 6 * 60 * 60.0

#: How much of one tool call's arguments to keep. They are
#: identifiers (a file id, a path, a search query), not results, so
#: this adds nothing new in kind to what a run record already
#: carries. The cap exists because one tool's arguments are not an
#: identifier at all: a reported-feedback screenshot can carry a
#: data-url image, and an unbounded capture would put a megabyte of
#: it in every record.
TOOL_ARG_CAP = 300

#: Events whose payload carries the resolved model and run id.
#: ``on_agent_run_started`` reaches this stream only on a retry, so a
#: run that never retried learns its model from the message or the
#: terminal event instead.
_MODEL_BEARING_EVENTS = frozenset({
    "on_agent_run_started", "on_chat_message", "on_agent_run_completed",
})

StreamOpener = Callable[..., Any]
Poster = Callable[..., Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation: what was called, and on what.

    ``args`` is the JSON the server sent on the tool-start event,
    truncated to :data:`TOOL_ARG_CAP`. It is the argument, never the
    result, since the result can be the customer's own document
    text.
    """

    tool: str
    args: str

    def identifier(self) -> str | None:
        """The id or path this call names, if it names one.

        Returns ``None`` rather than a guess: a tool whose arguments
        carry no identifier (a search query, say) must not be
        reported as having opened something.
        """
        import json as _json

        try:
            parsed = _json.loads(self.args)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        for key in ("file_id", "path", "id", "file", "document_id"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


@dataclass
class TurnState:
    """One user turn's outcome, mutated in place as :func:`consume_turn`
    drains the SSE: a caller that stops draining early (a dropped
    connection, a KeyboardInterrupt) still needs an accurate record of
    what happened before that.
    """

    chat_id: str
    #: Whether any ``on_agent_token`` delta was printed for the run
    #: currently in progress (reset on a retry, see :func:`consume_turn`).
    printed_any_token: bool = False
    #: True only once a member of TERMINAL_EVENTS was actually seen.
    terminal_seen: bool = False
    status: str | None = None
    error: str | None = None
    #: The model the server said it ran, off ``on_agent_run_started``.
    #: Not the model that was asked for: the server falls back
    #: silently for an id it does not recognise, so reporting the
    #: requested id would report a value nobody measured. None until
    #: the server states one.
    model: str | None = None
    #: The server's id for this run, so a line in a report can be
    #: found again in the server's own logs (``fm logs --run <id>``).
    run_id: str | None = None
    #: Tokens the run reported. ``usage_measured`` is the server's own
    #: measured flag, and is the whole point of carrying these: the
    #: server emits 0/0 for a backend that reports nothing, and "0
    #: tokens" and "unknown tokens" are different facts. None until
    #: the server states one.
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_measured: bool | None = None
    #: The attachment chips the assistant message carries, the files
    #: this run produced. Read off ``on_chat_message``, which carries
    #: the same list the server persists, so it costs no second
    #: request.
    #:
    #: ``None`` means the event never arrived (a dropped stream) and
    #: ``[]`` means it arrived carrying none; those are different
    #: facts.
    attachments: list[dict] | None = None
    #: Every tool call this run made, in order, with its arguments.
    #: Populated from the tool-start event, which already carries
    #: ``args``, so this needs no server change.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: The exact concatenation of every OUT-kind chunk this run
    #: printed (streamed tokens, or the assistant-message fallback
    #: when none streamed), populated by :func:`run_turn`. This is
    #: ``fm chat --message``'s only way to report the reply in
    #: ``--json``.
    final_text: str = ""

    @property
    def total_tokens(self) -> int | None:
        """Tokens this run is known to have spent, or None when unknowable."""
        if not self.usage_measured:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def consume_turn(state: TurnState, records: Iterable[dict]) -> Iterator[tuple[Kind, str]]:
    """Drive one turn's parsed SSE records into printable ``(kind, text)`` pairs.

    ``OUT`` is the assistant's own words, meant for stdout, streamed
    as they arrive. ``STATE`` is everything else (tool calls, retries,
    errors), meant for stderr, so it never mixes into reply text a
    caller might want to pipe or paste.

    Stops as soon as a :data:`TERMINAL_EVENTS` member is seen. Whether
    the input ran out without one is left on ``state.terminal_seen``
    for the caller to read once this generator is exhausted.
    """
    for record in records:
        if state.terminal_seen:
            return
        name = record.get("event")
        data = record.get("data")
        payload = data if isinstance(data, dict) else {}

        # Recorded off whichever event states them, before any branch
        # can return. The resolved model is not knowable any other
        # way from the client, since the server falls back silently
        # for an id it does not recognise. ``on_agent_usage`` is
        # deliberately not one of these events, because its own
        # `model` is what the provider reported for the call, a
        # different fact with the same key.
        if name in _MODEL_BEARING_EVENTS:
            state.model = payload.get("model") or state.model
            state.run_id = payload.get("run_id") or state.run_id

        if name == "on_agent_token":
            delta = payload.get("delta")
            if delta:
                state.printed_any_token = True
                yield (OUT, delta)

        elif name == "on_agent_run_started":
            if payload.get("retry_attempt"):
                # A retry re-announces run_started, meaning every
                # token streamed so far belongs to an abandoned
                # attempt. A terminal cannot un-print what already
                # scrolled by, so this marks the seam instead of
                # silently gluing the retried reply onto the stale
                # one.
                state.printed_any_token = False
                reason = payload.get("reason") or "rate limited"
                yield (STATE, f"\n[retrying: {reason}]")

        elif name == "on_agent_usage":
            # Recorded, not printed. `measured` is carried verbatim: a
            # False here means the backend reported nothing, not that
            # the run was free.
            state.input_tokens = payload.get("input_tokens")
            state.output_tokens = payload.get("output_tokens")
            state.usage_measured = payload.get("measured")

        elif name == "on_agent_tool_start":
            # Recorded structurally; the printed line is unchanged, so
            # `fm chat`'s output and anything parsing that trace keep
            # working.
            state.tool_calls.append(ToolCall(
                tool=str(payload.get("tool") or "?"),
                args=json.dumps(payload.get("args"), default=str)[:TOOL_ARG_CAP],
            ))
            yield (STATE, f"  -> {payload.get('tool') or '?'}")

        elif name == "on_agent_tool_end":
            mark = "ok" if payload.get("ok") else "ERROR"
            yield (STATE, f"  <- {payload.get('tool') or '?'} {mark}")

        elif name == "on_chat_message" and payload.get("role") == "ASSISTANT":
            # The attachment chips, captured before the printing
            # branch below: they must be recorded whether or not the
            # tokens already streamed.
            raw_attachments = payload.get("attachments")
            state.attachments = (
                [a for a in raw_attachments if isinstance(a, dict)]
                if isinstance(raw_attachments, list) else []
            )
            # The authoritative final text, which the server persists
            # exactly. Tokens are best-effort, so this is the
            # fallback for a run that streamed none (a non-streaming
            # backend, or a retry that landed with no further
            # deltas); printed only then, so a real answer is never
            # silently missing, and never duplicated when the deltas
            # already showed it.
            if not state.printed_any_token:
                content = payload.get("content") or ""
                if content:
                    yield (OUT, content)

        elif name in TERMINAL_EVENTS:
            state.terminal_seen = True
            if name == "on_agent_run_failed":
                state.status = "FAILED"
                state.error = payload.get("error")
                yield (STATE, f"\n[error] {state.error}")
            else:
                state.status = payload.get("status") or "COMPLETED"
                if state.status == "CANCELLED":
                    yield (STATE, "\n[cancelled]")


def render_incomplete(state: TurnState) -> str:
    """Render the warning for an SSE input that ended without a terminal event.

    Never say a turn finished on the strength of nothing more
    arriving: that is exactly as true of a connection that just died
    as of one that ended tidily.
    """
    return (
        f"chat {state.chat_id}: the stream ended without the server "
        "confirming this turn finished (no on_agent_run_completed / "
        "on_agent_run_failed arrived); the reply above may be PARTIAL.\n"
        f"Run `fm chats {state.chat_id} --messages` to read what was actually "
        "persisted."
    )


def build_run_body(
    content: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    attachments: Sequence[dict] | None = None,
) -> dict:
    """Build the POST body for ``/chats/{id}/run``.

    Only ``content`` is required. ``model``/``effort``/``attachments``
    ride along only when the operator asked for them: an unset flag
    reaches the server as an absent field, so its own default
    applies, never an explicit ``null``.

    ``attachments`` is the same list the browser sends when a person
    drops an image into the chat. It is passed through verbatim
    rather than reshaped here, since composing a second attachment
    encoder here would test something other than the product's own.

    An empty list is dropped, not sent: ``[]`` and "absent" mean the
    same thing to the server, and sending ``[]`` would make a
    text-only turn look, in a server log, like one that carried
    attachments and lost them.
    """
    body: dict = {"content": content}
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    if attachments:
        body["attachments"] = list(attachments)
    return body


def create_chat(
    target: Target,
    space: str,
    *,
    token: str | None,
    model: str | None = None,
    post: Poster | None = None,
) -> dict:
    """Start a new chat (``POST /projects/{id}/chats``) and return it.

    No ``title`` is sent: chat titles are auto-generated, and this
    command always addresses the chat it just created by the id
    handed back, never by a title.
    """
    space = require_space_id(space)
    post = post or api.post
    body: dict = {}
    if model:
        body["model"] = model
    chat = post(target, f"/projects/{space}/chats", body, token=token)
    if not isinstance(chat, dict) or not chat.get("id"):
        raise ChatsError(
            f"POST /projects/{space}/chats returned no chat id (got {chat!r})."
        )
    return chat


def run_turn(
    target: Target,
    space: str,
    chat_id: str,
    content: str,
    *,
    token: str | None,
    model: str | None = None,
    effort: str | None = None,
    attachments: Sequence[dict] | None = None,
    stream_fn: StreamOpener | None = None,
    out: Any = None,
    err: Any = None,
    read_timeout: float | None = None,
) -> TurnState:
    """POST one user turn and print the reply as it streams. Returns the outcome.

    Printing lives here, not one layer up in the REPL loop, since the
    property this module exists for (a dropped or partial SSE input
    must not read as a finished turn) belongs to consuming the
    stream. Keeping it here also lets a test drive canned SSE text
    through the same path the REPL uses, with no terminal involved.

    ``out``/``err`` default to ``None`` and are resolved to
    ``sys.stdout`` / ``sys.stderr`` inside the call, not in the
    signature, so a test fixture that swaps either stream for the
    duration of one call still takes effect.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    space = require_space_id(space)
    stream_fn = stream_fn or api.stream_post
    path = f"/projects/{space}/chats/{chat_id}/run"
    body = build_run_body(content, model=model, effort=effort, attachments=attachments)
    state = TurnState(chat_id=chat_id)

    try:
        timeout = READ_TIMEOUT if read_timeout is None else read_timeout
        with stream_fn(target, path, body, token=token, read_timeout=timeout) as lines:
            for kind, text in consume_turn(state, parse_sse(lines)):
                if kind is OUT:
                    print(text, end="", flush=True, file=out)
                    state.final_text += text
                else:
                    print(text, file=err, flush=True)
    except api.StreamClosed as exc:
        print(f"\nerror: {exc}", file=err)

    print(file=out)  # the reply always ends its own line, however the run ended
    if not state.terminal_seen:
        print(render_incomplete(state), file=err)
    return state


# ===========================================================================
# The CLI surface. One `add_parser` per module.
# ===========================================================================
def _print_context(target: Target, space: str, chat_id: str, *, token: str | None) -> None:
    """Print what `fm chat <id>` shows before the first prompt.

    Reuses :mod:`formist_cli.chats`' own reader rather than a second
    one: the 404 it can raise here already explains the chat-privacy
    rule, and the transcript it prints already carries its own
    completeness warning if the space's history exceeds one page.
    """
    chat = get_chat(target, space, chat_id, token=token)
    print(
        f"{chat.get('id')}  {chat.get('kind', '?')}  {chat.get('title') or ''}",
        file=sys.stderr,
    )
    transcript = fetch_messages(target, space, chat_id, token=token)
    print(transcript.render(), file=sys.stderr)
    print(file=sys.stderr)


def _repl(
    target: Target,
    space: str,
    chat_id: str,
    *,
    token: str | None,
    model: str | None,
    effort: str | None,
    input_fn: Callable[[str], str] = input,
    stream_fn: StreamOpener | None = None,
) -> int:
    print(
        f"fm chat: {chat_id}  ({target.name})\n"
        "type a message and press enter; Ctrl-D or /exit to quit",
        file=sys.stderr,
    )
    while True:
        try:
            line = input_fn("you> ")
        except EOFError:
            print(file=sys.stderr)
            break
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        print("fm> ", end="", flush=True)
        try:
            run_turn(
                target,
                space,
                chat_id,
                text,
                token=token,
                model=model,
                effort=effort,
                stream_fn=stream_fn,
            )
        except KeyboardInterrupt:
            print("\n[interrupted]", file=sys.stderr)
    return 0


def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "chat",
        help="interactive REPL against the server-side agent (WRITE)",
        description=(
            "Opens an interactive conversation with the server-side agent, "
            "the same one the browser chat runs, over an SSE endpoint. Give "
            "a chat id to continue that conversation (its recent transcript "
            "prints first, via `fm chats`' own reader); omit it to start a "
            "new one.\n\n"
            "Scope is the same as `fm chats` (this reuses its transport and "
            "its privacy rule, not a second one): you can only continue a "
            "chat you may read under the member route, your own chats plus "
            "workspace-scoped ones your role can see, and only run a turn "
            "on a chat you own (a workspace chat is read-only). A "
            "background run someone else triggered is invisible here too, "
            "on purpose; the API never reveals that it exists. There is no "
            "resolve-by-title: chat titles are auto-generated and "
            "duplicate heavily.\n\n"
            "This creates chats, spends the project's agent quota and "
            "persists real messages, so, like `fm upload` / `fm update`, "
            "the target is never inherited from `fm use`: pass --env (or "
            "--host) explicitly.\n\n"
            "--message TEXT switches to non-interactive mode: sends "
            "exactly this one turn (creating a chat first if no id was "
            "given), streams it compactly (assistant text on stdout, one "
            "line per tool call on stderr), and exits when the run "
            "finishes, no REPL, no prompt. The HTTP stream is held open "
            "for the whole run with no meaningful read timeout, because "
            "the server ends the run the moment this client disconnects "
            "and a real run can take an hour. Exit code is non-zero if "
            "the run failed (on_agent_run_failed) or the stream ended "
            "without a terminal event. --json then prints one final "
            "summary object instead of streaming to the terminal: "
            "{chat_id, tool_calls, failed, final_message}."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument(
        "chat_id", nargs="?", help="continue this chat (UUID); omit to start a new one"
    )
    p.add_argument("--model", default=None, help="override the chat's model for this run")
    p.add_argument(
        "--effort", default=None, help="low|medium|high|xhigh (ignored by models with no knob)"
    )
    p.add_argument(
        "--message", default=None, metavar="TEXT",
        help="send this one message non-interactively and exit when the run "
        "finishes, instead of opening the REPL",
    )
    p.add_argument(
        "--json", action="store_true",
        help="with --message: print a final summary "
        "{chat_id, tool_calls, failed, final_message} instead of streaming",
    )


def cmd_chat(args) -> int:
    """Resolve or create the chat, then either run the REPL or, with
    ``--message``, run exactly one non-interactive turn and exit."""
    from formist_cli import context

    eff = context.resolve(args, for_write=True, need_space=True, need_token=True)
    token = eff.token
    chat_id = args.chat_id

    if getattr(args, "message", None):
        return _run_message(eff.target, eff.space, chat_id, args.message, token=token, args=args)

    if chat_id:
        _print_context(eff.target, eff.space, chat_id, token=token)
    else:
        chat = create_chat(eff.target, eff.space, token=token, model=args.model)
        chat_id = str(chat.get("id"))
        print(f"  started new chat {chat_id}", file=sys.stderr)

    return _repl(
        eff.target, eff.space, chat_id, token=token, model=args.model, effort=args.effort
    )


def _run_message(
    target: Target, space: str, chat_id: str | None, content: str, *, token: str | None, args
) -> int:
    """Run one turn non-interactively, held open for however long the
    run actually takes (see :data:`NON_INTERACTIVE_READ_TIMEOUT`).

    No REPL banner and no transcript print: this mode is for
    scripting, and exits with a status that says whether the run
    worked.

    ``--json`` prints exactly one object and nothing else on stdout:
    the streamed assistant text is captured into an in-memory buffer
    instead of being written live, so a caller parsing stdout as JSON
    never has to split it away from text that would otherwise have
    printed ahead of it. Tool-call lines still go to stderr either
    way, since they are diagnostics, not part of the ``--json``
    contract.
    """
    if not chat_id:
        chat = create_chat(target, space, token=token, model=args.model)
        chat_id = str(chat.get("id"))
        print(f"  started new chat {chat_id}", file=sys.stderr)

    as_json = bool(getattr(args, "json", False))
    captured_out = io.StringIO() if as_json else None

    state = run_turn(
        target,
        space,
        chat_id,
        content,
        token=token,
        model=args.model,
        effort=args.effort,
        read_timeout=NON_INTERACTIVE_READ_TIMEOUT,
        out=captured_out,
    )

    failed = state.status == "FAILED"
    incomplete = not state.terminal_seen

    if as_json:
        print(json.dumps(
            {
                "chat_id": chat_id,
                "tool_calls": len(state.tool_calls),
                "failed": failed,
                "final_message": state.final_text,
            },
            indent=2, ensure_ascii=False,
        ))
    return 1 if (failed or incomplete) else 0
