"""``fm chat`` — an interactive REPL against the server-side agent.

WHAT THIS IS, AND WHAT IT IS NOT
---------------------------------
``fm chats`` (plural, ``evtool/chats.py``) reads history: it lists a space's
chats/agent runs and prints a transcript. This module is the other half — it
WRITES a turn and watches the reply arrive, over the same route the browser
chat uses: ``POST /projects/{id}/chats/{id}/run`` (``routes/agent.py``), which
streams ``text/event-stream`` (see that module's header for the event
vocabulary this file consumes).

This is a thin client over that route, not a second implementation of it.
Fetching a chat, reading its transcript and paging past the 200-row cap are
all ``evtool/chats.py``'s job and are reused, not re-derived, here
(:func:`formist_cli.chats.get_chat`, :func:`formist_cli.chats.fetch_messages`). What is
new in this module is the WRITE half: creating a chat, POSTing a turn, and
turning the SSE it streams back into terminal output as it arrives rather than
after the whole reply is in.

SCOPE — THE SAME ONE ``fm chats`` HAS, NOT A WIDER ONE
--------------------------------------------------------
``routes/agent.py``'s docstring: "Auth + privacy reuse routes/chats.py's gates:
only a project member may run, and only on their OWN (USER-scoped) chat (a
WORKSPACE chat is read-only -> 403)." So this command can:

  * continue a chat this session owns (a private ``USER_CHAT``, or one it
    started itself), and
  * start a brand-new one (always ``USER``-owned — ``POST /chats`` has no way
    to create a ``WORKSPACE`` chat; see ``routes/chats.py::create_chat``).

It CANNOT open another member's private chat (404, on purpose — see
``evtool/chats.py``'s module docstring) and it cannot run a turn against a
``WORKSPACE`` chat someone else's automation created (403 — those are
read-only to everyone). There is also still no resolve-by-title: titles are
auto-generated and duplicate heavily, so this command takes an id or starts a
new one, exactly like ``fm chats``.

THE PROPERTY THIS FILE IS BUILT AROUND
----------------------------------------
``services/agent_run.py``: "Token events are best-effort (live UX); the
persisted answer is the ``final`` content." So a dropped connection mid-run
can leave this terminal holding a PARTIAL reply with no way to tell, from the
printed text alone, that it is partial — the same shape ``evtool/logs.py``
built an entire state machine to avoid for the event firehose ("a quiet stream
and a dead one look identical unless something makes them not").

The fix is the same one: track whether a TERMINAL event
(``on_agent_run_completed`` / ``on_agent_run_failed``) was actually seen
(:class:`TurnState`), and if the SSE input runs out — or drops mid-frame, via
``formist_cli.logs.parse_sse``'s own "an unterminated frame is still emitted"
behaviour — without one, say so loudly (:func:`render_incomplete`) instead of
letting the reply read as a normal, finished turn.

``parse_sse`` (``evtool/logs.py``) is reused rather than re-implemented: it
already owns the one hazard that matters here — a multi-line SSE ``data:``
field is only a complete record once its terminating blank line arrives, and a
second, slightly-different frame parser next to it is exactly the kind of
two-lanes-one-abstraction split the project rules warns about.

Stdlib only — ``ev`` must add no runtime dependency.
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

#: The two events that end a run (``services/agent_events.py`` / the terminal
#: branches of ``routes/agent.py``'s ``event_generator``). Seeing one is the
#: only thing that distinguishes "the turn finished" from "the connection
#: stopped handing us bytes" — see the module docstring.
TERMINAL_EVENTS = frozenset({"on_agent_run_completed", "on_agent_run_failed"})

#: SSE read timeout for one INTERACTIVE turn. Sized generously rather than
#: derived from a keepalive period the way ``evtool/logs.py`` sizes ``fm logs``'
#: timeout: sse-starlette pings this endpoint too, but a turn can also sit
#: silent for a long tool call (a KB search, a document read) with nothing on
#: the wire in between, and that silence is not a sign of anything wrong.
READ_TIMEOUT = 300.0

#: SSE read timeout for ``fm chat --message`` — effectively unbounded. A real
#: agent run can take an hour (routes/agent.py: "minutes for a real run", and
#: Starlette ties the generator's life to the RESPONSE — the server ends the
#: run the moment this client's socket disconnects), so a client-side timeout
#: sized for an interactive REPL turn would ABORT a run that was working fine,
#: which is the opposite of what a non-interactive, unattended invocation
#: needs. Large-but-finite rather than infinite because Python's socket layer
#: rejects an actual infinite float; nothing legitimate should run longer.
NON_INTERACTIVE_READ_TIMEOUT = 6 * 60 * 60.0

#: How much of one tool call's arguments to keep. They are IDENTIFIERS — a file
#: id, a path, a search query — not results, and the run record already carries
#: document titles and SharePoint URLs inside the answers themselves, so this adds
#: nothing new in kind. The cap exists because one tool's argument is not an
#: identifier at all: ``report_feedback`` can carry a ``data:image/png;base64,…``
#: screenshot, and an unbounded capture would put a megabyte of it in every record.
TOOL_ARG_CAP = 300

#: Events whose payload carries routes/agent.py's ``resolved_model`` (and the
#: run id). ``on_agent_run_started`` reaches this stream only on a RETRY — the
#: first one is emitted to the dispatcher before the response begins — so a run
#: that never retried learns its model from the message or the terminal event.
_MODEL_BEARING_EVENTS = frozenset({
    "on_agent_run_started", "on_chat_message", "on_agent_run_completed",
})

StreamOpener = Callable[..., Any]
Poster = Callable[..., Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation: what was called, and on what.

    ``args`` is the JSON the server put on ``on_agent_tool_start`` (see
    ``services/agent_events.py:85-91``), truncated to :data:`TOOL_ARG_CAP`. It is
    the ARGUMENT and never the result — the result is the customer's document
    text, and these runs go against a real tenant.
    """

    tool: str
    args: str

    def identifier(self) -> str | None:
        """The id or path this call names, if it names one.

        ⚠️ Returns None rather than a guess. A tool whose arguments carry no
        identifier — a search query, say — must not be reported as having opened
        something, and "no identifier in the arguments" and "opened nothing" are
        different facts.
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
    """One user turn's outcome, mutated in place as :func:`consume_turn` drains
    the SSE — the same shape as ``formist_cli.logs.Subscription``, and for the same
    reason: a caller that stops draining early (a dropped connection, a
    KeyboardInterrupt) still needs an accurate record of what happened before
    that, and a generator cannot both yield values along the way and return a
    final one a plain ``for`` loop can read.
    """

    chat_id: str
    #: Whether ANY ``on_agent_token`` delta was printed for the run currently in
    #: progress (reset on a retry — see :func:`consume_turn`).
    printed_any_token: bool = False
    #: True only once a member of TERMINAL_EVENTS was actually seen.
    terminal_seen: bool = False
    status: str | None = None
    error: str | None = None
    #: The model the SERVER said it ran, off ``on_agent_run_started``. NOT the
    #: model that was asked for: ``services/agent_run.py::resolve_model`` falls
    #: back SILENTLY for an id it does not recognise, so an operator (or a
    #: benchmark — ``evtool/benchmark.py``) that reports the requested id is
    #: reporting a value nobody measured. None until the server states one.
    model: str | None = None
    #: The server's id for this run, so a line in a report can be found again in
    #: the server's own logs (``fm logs --run <id>``).
    run_id: str | None = None
    #: Tokens the run reported (``on_agent_usage``). ``usage_measured`` is the
    #: server's own ``measured`` flag and is the whole point of carrying these:
    #: routes/agent.py emits 0/0 for a backend that reports nothing, and "0
    #: tokens" and "unknown tokens" are different facts. A caller enforcing a
    #: budget must not read an unmeasured run as a free one — that zero-meaning-
    #: unknown shape is what hid five days of runaway spend (curist-metering-
    #: blindness). None until the server states one.
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_measured: bool | None = None
    #: The attachment chips the ASSISTANT message carries — the files this run
    #: PRODUCED, as ``routes/agent.py``'s ``_collect_run_file`` recorded them
    #: ({kind, source, ref, name, path, mime_type, download_url}). Read off
    #: ``on_chat_message``, which carries the same list the route persists, so it
    #: costs no second request.
    #:
    #: ⚠️ ``None`` means the event never arrived (a dropped stream) and ``[]``
    #: means it arrived carrying none. Those are different facts, and the second
    #: is the one that catches the failure they exist for: measured on the live
    #: invitation-letter run of 2026-09-08, TWO letters were generated and filed
    #: and the assistant message carried ONE chip. Nothing was red — the user
    #: simply could not click the second letter — because every tool's own return
    #: was correct and the loss happened on the way to the message.
    attachments: list[dict] | None = None
    #: Every tool call this run made, in order, with its arguments. Populated
    #: from ``on_agent_tool_start``, which already carries ``args`` — so this
    #: needs no server change. It is what turns "did the agent open the document
    #: it cited" from an inference into a join.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: The exact concatenation of every OUT-kind chunk this run printed
    #: (streamed tokens, or the ``on_chat_message`` fallback when none
    #: streamed) — populated by :func:`run_turn`, not by :func:`consume_turn`
    #: itself (which stays a pure generator with no side channel). This is
    #: `fm chat --message`'s only way to report the reply in ``--json``: that
    #: mode has no terminal for the streamed text to land on.
    final_text: str = ""

    @property
    def total_tokens(self) -> int | None:
        """Tokens this run is KNOWN to have spent, or None when unknowable."""
        if not self.usage_measured:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def consume_turn(state: TurnState, records: Iterable[dict]) -> Iterator[tuple[Kind, str]]:
    """Drive one turn's parsed SSE records into printable ``(kind, text)`` pairs.

    ``OUT`` is the assistant's own words, meant for stdout, streamed as they
    arrive. ``STATE`` is everything else — tool calls, retries, errors — meant
    for stderr, so it is never mixed into the reply text a caller might want to
    pipe or paste.

    Stops as soon as a :data:`TERMINAL_EVENTS` member is seen. Whether the
    input ran out WITHOUT one is not this function's call — that fact lives on
    ``state.terminal_seen`` for the caller to read once this generator is
    exhausted, exactly as ``formist_cli.logs.consume`` leaves ``sub.confirmed`` for
    its caller.
    """
    for record in records:
        if state.terminal_seen:
            return
        name = record.get("event")
        data = record.get("data")
        payload = data if isinstance(data, dict) else {}

        # Recorded off whichever event states them, before any branch can return.
        # ⚠️ The RESOLVED model is not knowable any other way from the client:
        # ``services/agent_run.py::resolve_model`` falls back SILENTLY for an id
        # it does not recognise, so "the model I asked for" is not evidence of
        # what ran. The three events below all carry routes/agent.py's
        # ``resolved_model``; ``on_agent_usage`` is deliberately NOT one of them,
        # because its ``model`` is what the PROVIDER reported for the call, which
        # is a different fact wearing the same key.
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
                # services/agent_events.py: a retry RE-ANNOUNCES run_started, and
                # means every token streamed so far belongs to an abandoned
                # attempt. A terminal cannot un-print what already scrolled by, so
                # this marks the seam instead of silently gluing the retried reply
                # onto the stale one.
                state.printed_any_token = False
                reason = payload.get("reason") or "rate limited"
                yield (STATE, f"\n[retrying: {reason}]")

        elif name == "on_agent_usage":
            # Recorded, not printed. `measured` is carried verbatim: a False here
            # means the backend reported nothing, NOT that the run was free.
            state.input_tokens = payload.get("input_tokens")
            state.output_tokens = payload.get("output_tokens")
            state.usage_measured = payload.get("measured")

        elif name == "on_agent_tool_start":
            # Recorded structurally; the PRINTED line is unchanged, so `fm chat`'s
            # output and everything parsing that trace keep working.
            state.tool_calls.append(ToolCall(
                tool=str(payload.get("tool") or "?"),
                args=json.dumps(payload.get("args"), default=str)[:TOOL_ARG_CAP],
            ))
            yield (STATE, f"  → {payload.get('tool') or '?'}")

        elif name == "on_agent_tool_end":
            mark = "ok" if payload.get("ok") else "ERROR"
            yield (STATE, f"  ← {payload.get('tool') or '?'} {mark}")

        elif name == "on_chat_message" and payload.get("role") == "ASSISTANT":
            # The attachment chips, captured BEFORE the printing branch below —
            # they must be recorded whether or not the tokens already streamed.
            raw_attachments = payload.get("attachments")
            state.attachments = (
                [a for a in raw_attachments if isinstance(a, dict)]
                if isinstance(raw_attachments, list) else []
            )
            # The AUTHORITATIVE final text — routes/agent.py persists exactly
            # this. Tokens are "best-effort" (services/agent_run.py's own
            # docstring), so this is the fallback for a run that streamed NONE
            # (a non-streaming backend, or a retry that landed with no further
            # deltas) — printed only then, so a real answer is never silently
            # missing, and never duplicated when the deltas already showed it.
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
    """The warning for an SSE input that ended WITHOUT a terminal event.

    Never say a turn finished on the strength of "nothing more arrived" — that
    is exactly as true of a connection that just died as of one that ended
    tidily, and treating them alike is the one failure this module exists to
    avoid (``evtool/logs.py`` states the identical rule for the event
    firehose).
    """
    return (
        f"⚠️ chat {state.chat_id}: the stream ended without the server "
        "confirming this turn finished (no on_agent_run_completed / "
        "on_agent_run_failed arrived) — the reply above may be PARTIAL.\n"
        f"→ `fm chats {state.chat_id} --messages` to read what was actually "
        "persisted."
    )


def build_run_body(
    content: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    attachments: Sequence[dict] | None = None,
) -> dict:
    """The POST body for ``/chats/{id}/run`` (``routes/agent.py::AgentRunRequest``).

    Only ``content`` is required. ``model``/``effort``/``attachments`` ride along
    exactly when the operator asked for them — an unset flag reaches the server
    as an ABSENT field (so its own default applies), never an explicit ``null``,
    the same "unset is absent, not empty" rule ``evtool/logs.py::build_params``
    states for its own query parameters.

    ``attachments`` is the SAME list the browser sends when a person drops an
    image into the chat: ``routes/agent.py::AgentRunRequest.attachments``, whose
    entries ``services/agent_vision.py::build_image_parts`` resolves into image
    content blocks (``{"ref_kind": "file", "ref": "<File uuid>", ...}``). It is
    passed through verbatim rather than reshaped here, because the moment this
    module starts composing its own attachment dicts it is testing a second
    encoder instead of the product's — the same reason nothing here assembles a
    prompt. :func:`formist_cli.benchmark.attachment_descriptor` builds the entries,
    from a File row the server itself handed back.

    ⚠️ An EMPTY list is dropped, not sent. ``[]`` and "absent" mean the same
    thing to the route, and sending ``[]`` would make every text-only turn look,
    in a server log or a captured request, like a turn that carried attachments
    and lost them.
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
    """Start a new USER chat (``POST /projects/{id}/chats``) and return it.

    No ``title`` is sent. Chat titles are auto-generated
    (``evtool/chats.py``'s reason for having no resolve-by-title applies here
    too), and this command always addresses the chat it just created by the id
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
    """POST one user turn and print the reply AS IT STREAMS. Returns the outcome.

    Printing lives here, not one layer up in the REPL loop, because the
    property under test — a dropped or partial SSE input must not read as a
    finished turn — belongs to CONSUMING the stream, not to the loop that reads
    the next line from the keyboard. Keeping it here also lets a test drive
    canned SSE text through the exact path the REPL uses, with no terminal
    involved.

    ``out``/``err`` default to ``None`` and are resolved to ``sys.stdout`` /
    ``sys.stderr`` INSIDE the call, not in the signature: a default bound at
    def time captures whatever ``sys.stdout`` was at import, which is not what
    a test fixture that swaps ``sys.stdout`` for the duration of one call (e.g.
    pytest's ``capsys``) has replaced — the classic mutable-default-via-module-
    global trap, just with a builtin stream instead of a list.
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
# The CLI surface. Per-module `add_parser` (team-lead ruling, 2026-08-27) — see
# evtool/chats.py's identical section for why.
# ===========================================================================
def _print_context(target: Target, space: str, chat_id: str, *, token: str | None) -> None:
    """What `fm chat <id>` shows before the first prompt.

    Reuses ``formist_cli.chats``' own reader rather than a second one: the 404 it
    can raise here already explains the chat-privacy rule, and the transcript
    it prints already carries its own completeness warning
    (``Transcript.render``) if the space's history exceeds one page.
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
        f"fm chat — {chat_id}  ({target.name})\n"
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
            "Opens an interactive conversation with the server-side agent — "
            "the same one the browser chat runs — over "
            "POST /projects/{id}/chats/{id}/run (routes/agent.py), which streams "
            "text/event-stream. Give a chat id to continue that conversation (its "
            "recent transcript prints first, via `fm chats`' own reader); omit it "
            "to start a new one.\n\n"
            "SCOPE, same as `fm chats` (this reuses its transport and its privacy "
            "rule, not a second one): you can only continue a chat you may READ "
            "under the member route — your own chats, plus WORKSPACE-scoped ones "
            "your role can see — and only RUN a turn on a USER chat you own (a "
            "WORKSPACE chat is read-only → 403). A background run someone else "
            "triggered is invisible here too, on purpose — the API never reveals "
            "that it exists. There is no resolve-by-title: chat titles are "
            "auto-generated and duplicate heavily.\n\n"
            "Requires an explicit --env: this creates chats, spends the project's "
            "agent quota and persists real messages, so — like `fm upload` / "
            "`fm update` — the target is never inherited from `fm use`.\n\n"
            "--message TEXT switches to NON-INTERACTIVE mode: sends exactly this "
            "one turn (creating a chat first if no id was given), streams it "
            "compactly (assistant text on stdout, one line per tool call on "
            "stderr), and exits when the run finishes — no REPL, no prompt. The "
            "HTTP stream is held open for the WHOLE run with no meaningful read "
            "timeout, because the server ends the run the moment this client "
            "disconnects and a real run can take an hour. Exit code is non-zero "
            "if the run failed (on_agent_run_failed) or the stream ended without "
            "a terminal event. --json then prints one final summary object "
            "instead of streaming to the terminal: "
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
    """``fm chat`` — resolve or create the chat, then either REPL or, with
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
    """``fm chat --message`` — one turn, non-interactively, held open for
    however long the run actually takes (see :data:`NON_INTERACTIVE_READ_TIMEOUT`).

    No REPL banner and no transcript print: this mode is for scripting, and its
    whole contract is "stream this one turn compactly, then exit with a status
    that says whether it worked" — see the module's ``add_parser`` description.

    ``--json`` prints EXACTLY ONE object and nothing else on stdout: the
    streamed assistant text is captured into an in-memory buffer instead of
    being written live (``run_turn``'s own ``out=`` parameter exists for
    precisely this), so a caller parsing stdout as JSON never has to split it
    away from the text that would otherwise have printed ahead of it. Tool-call
    lines still go to stderr either way — they are diagnostics, not the
    contract ``--json`` describes.
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
