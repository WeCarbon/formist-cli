"""``fm logs``: a live subscription to the server's whole event bus.

This is a tap, not a log: it shows what the server emits from now on,
over ``/admin/events/*``. There is no ``--since`` and there cannot be
one, since no durable event store exists on the server side to replay
from; for history, use `fm audit-log`, `fm chats` or `fm connectors`
instead. ``/admin/events`` is served from the Global region only.

This command must never invoke the ``az`` CLI, so there is one
transport and one credential, and it works for anyone with a session
token and no Azure login at all; a test enforces this structurally.

A live firehose is not a log: the event bus is in-process and touches
no database, so a redeploy resets its counters, and this command can
never answer what happened at a specific past time.

A healthy, quiet stream and a connection that never opened look
identical, an iterator that is not yielding, so this module keeps
three things apart deliberately:

* a subscription reaches live only once it is handed the server's own
  preamble (:meth:`Subscription.confirm`); nothing renders a
  "connected" line from what the process merely intended to do.
* ending is a reason, not an absence: :data:`Ended` distinguishes a
  deadline from an interrupt from a server that went away, and each
  gets a different exit code.
* a stream with no liveness signal is refused up front
  (:func:`require_liveness_signal`): with ``keepalive=0`` a text
  stream puts nothing on the wire while the bus is quiet, and at that
  point idle and dead are indistinguishable.

Stdlib only: this module must not add a runtime dependency.
"""

from __future__ import annotations

import argparse
import enum
import json
import sys
import time
from collections.abc import Callable, Iterable, Iterator

from formist_cli import api, context
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.targets import Target, is_admin_env

#: The ``/admin/events/*`` surface.
SUBJECTS = ("stream", "rates", "jobs", "activity", "types")

DEFAULT_SUBJECT = "stream"

#: Printed to stderr before a stream starts. The most likely
#: misreading of this tool is that it is a log, so the correction goes
#: where the operator is looking, and it names somewhere to go.
LIVE_ONLY_NOTE = (
    "This is a LIVE tap, not a log: it shows what the server emits from now on.\n"
    "  The event bus is in-process and this stream touches no database, so it "
    "cannot tell you what happened yesterday, and rates/jobs/activity are "
    "counters since the last boot.\n"
    "  For what already happened: `fm audit-log` (durable, per space), "
    "`fm chats` (an agent run's own transcript), `fm connectors` (the last sync "
    "run per connector). A crash before the first emit() is in none of them."
)

#: What leaves the room when this runs on a shared screen.
#:
#: In ``--help`` rather than in a docstring, because the disclosure is
#: only useful before the command runs.
STREAM_CARRIES = """WHAT THE STREAM CARRIES: read before sharing your screen

REDACTED by the server:
  credentials   any key containing password/secret/token/api_key/cookie/jwt/...,
                plus any value shaped like a Bearer JWT, whatever its key
  user content  content/body/text/transcript/summary/prompt/args/result/... become a
                SHAPE marker (<str:4210>), never previewed

NOT REDACTED. These reach your terminal in full:
  join_url      a meeting's join link. This is a CAPABILITY, not a name:
                whoever reads it can enter the meeting. It is not saved by
                an 80-char clip either; a real Teams join URL measured 69
                characters, so it prints ENTIRE, ?p= passcode included.
                A clip is not a redaction.
  author_name   the author's display name, or their email address when no
                display name was set, under a key that does not say so.
  subject       a meeting's title.  name  a space's name.
  error/reason  160-char previews of failure detail, URLs and paths included.
  ids           project/entity/user/file UUIDs, truncated to 8 chars.

This gap is known and intentionally not closed: logs are not redacted
here, because operators need unredacted output to diagnose issues.

So: credentials and message bodies are safe to show. MEETING JOIN LINKS, EMAIL
ADDRESSES, MEETING TITLES AND SPACE NAMES ARE NOT. Narrow with --types first.

Scope, so this is not read as bigger than it is: /admin/events is
platform-admin-only, and a platform admin can already read space data through
/admin/*. This is scrollback hygiene, not an authorization boundary."""


class LogsError(Exception):
    """A refusal about the event surface, written for an operator."""


# ===========================================================================
# Subject selection
# ===========================================================================
def canonical_subject(subject: str | None) -> str:
    """Normalise the positional subject. ``None`` means the stream."""
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise LogsError(
            f"unknown subject {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  stream    live firehose\n"
            "  rates     one-shot rate snapshot\n"
            "  jobs      per background job: last run, counts, failures\n"
            "  activity  what is in flight right now\n"
            "  types     the event-type vocabulary"
        )
    return value


def subject_path(subject: str) -> str:
    return f"/admin/events/{subject}"


def require_admin_region(target: Target) -> None:
    """Refuse an admin call aimed at a prod env that does not serve ``/admin``,
    before sending it.

    Same rule as :func:`formist_cli.api.admin_get`: ``/admin`` is
    served from the Global region (and, as its own single-region
    deployment, Formist); other regions answer 410. Repeated here
    because the streaming path does not go through ``admin_get``, and
    a rule enforced on one of several callers is a rule the others do
    not have.
    """
    if target.is_prod and not is_admin_env(target.env):
        raise LogsError(
            f"/admin/events is served from the GLOBAL region only; sending this "
            f"to {target.name} would return 410.\n"
            "Use `--host evane`. The event bus you want is the Global "
            "instance's; a region's own bus is not exposed separately."
        )


def build_params(
    *,
    types: str | None = None,
    exclude: str | None = None,
    project: str | None = None,
    source: str | None = None,
    only_work: bool = False,
    mode: str | None = None,
    as_json: bool = False,
    interval: int | None = None,
    keepalive: int | None = None,
) -> dict[str, str]:
    """Build the query params for ``/admin/events/*``, in the route's vocabulary.

    Every filter is passed through rather than applied here: filtering
    a stream client-side is worse than filtering a list client-side,
    since a line dropped by the client was never counted anywhere, so
    nothing contradicts the impression that you saw everything.

    An unset filter is absent, never empty: ``types=""`` is a filter
    matching nothing, which is the opposite of no filter.

    No credential is ever placed here. The server deliberately refuses
    a ``?token=`` query parameter so the JWT stays out of URLs, access
    logs, proxy logs and shell history; the token travels in the
    Authorization header instead.
    """
    params: dict[str, str] = {"format": "json" if as_json else "text"}
    if types:
        params["types"] = types
    if exclude:
        params["exclude"] = exclude
    if project:
        params["project_id"] = project
    if source:
        params["source"] = source
    if only_work:
        params["only_work"] = "1"
    if mode:
        params["mode"] = mode
    if interval is not None:
        params["interval"] = str(interval)
    if keepalive is not None:
        params["keepalive"] = str(keepalive)
    return params


# ===========================================================================
# Liveness: the only thing that separates idle from dead
# ===========================================================================
#: sse-starlette's fallback ping period on the server: ``ping=max(keepalive or 30, 1)``.
SSE_PING_FALLBACK = 30


def require_liveness_signal(keepalive: int, as_json: bool) -> None:
    """Refuse a text stream that would put nothing on the wire while idle.

    With ``keepalive=0`` the server's text generator emits no status
    line, so a quiet bus and a dead socket become byte-for-byte
    identical and no amount of care in this file can tell them apart
    afterwards.

    JSON is deliberately exempt, and this is not laxness: sse-starlette
    pings on its own schedule regardless of the requested keepalive, so
    a JSON stream is never silent.
    """
    if keepalive == 0 and not as_json:
        raise LogsError(
            "--keepalive 0 is refused for a text stream.\n"
            "With no keepalive the server sends nothing at all while the bus is "
            "quiet, so an idle stream and a dropped connection look identical, "
            "which is the one thing this command exists to prevent.\n"
            "Use a positive --keepalive, or --json (sse-starlette pings on its "
            "own schedule there, so that stream is never silent)."
        )


def effective_ping(keepalive: int, as_json: bool) -> int:
    """How often the server promises to put something on the wire, in its own terms."""
    if as_json:
        return max(keepalive or SSE_PING_FALLBACK, 1)
    return keepalive


def read_timeout_for(keepalive: int, as_json: bool) -> float:
    """Socket read timeout: tolerate exactly one missed keepalive, then give up.

    Derived from the server's period rather than picked here. Too
    short and a healthy idle stream is torn down and reported as a
    drop; too long, or absent, and a dead socket hangs forever looking
    like a quiet one.
    """
    return effective_ping(keepalive, as_json) * 2 + 5


# ===========================================================================
# The subscription's own state
# ===========================================================================
class Phase(enum.Enum):
    OPENING = "opening"
    LIVE = "live"
    ENDED = "ended"


class Ended(enum.Enum):
    #: ``--for N`` elapsed. The normal, successful way to stop.
    DEADLINE = "deadline"
    #: Ctrl-C. Also normal.
    INTERRUPTED = "interrupted"
    #: The server or the network went away. Not normal, and not a quiet stream.
    SERVER_CLOSED = "server-closed"


class Kind(enum.Enum):
    #: Stream payload, stdout, pipeable.
    OUT = "out"
    #: The subscription talking about itself, stderr, never in the data.
    STATE = "state"


OUT = Kind.OUT
STATE = Kind.STATE

#: The two literal shapes of the text preamble.
#:
#: Matched on their literal text rather than on "a leading ``#``",
#: since the keepalive status line also begins with ``#``. A
#: leading-hash rule would swallow the status line into the handshake
#: on an idle stream, i.e. the code proving the connection is alive
#: would eat the one line proving it is alive. If the server ever
#: changes this banner the handshake stops confirming, which is the
#: safe direction: "cannot verify what this is connected to" rather
#: than a silently eaten line.
BANNER_HEAD = "# evane admin event firehose"
BANNER_MODE = "# mode="


class Subscription:
    """What this process is listening to, and whether it actually is.

    Deliberately not a dataclass of plain fields: :attr:`phase` may
    only advance through :meth:`confirm` and :meth:`end`, so "we are
    connected" cannot be set by an assignment somebody adds later in a
    hurry.
    """

    def __init__(
        self,
        *,
        env: str,
        api_base: str,
        path: str,
        filters: dict[str, str],
        clock: Callable[[], float] = time.monotonic,
    ):
        self.env = env
        self.api_base = api_base
        self.path = path
        self.filters = dict(filters)
        self.phase = Phase.OPENING
        #: The server's own words, read back off the wire. The only
        #: evidence that this subscription exists. ``None`` until it
        #: arrives.
        self.server_preamble: str | None = None
        self.events = 0
        self.status_lines = 0
        self.ended_reason: Ended | None = None
        self.elapsed = 0.0
        self._clock = clock
        self._started = clock()

    @property
    def confirmed(self) -> bool:
        return self.server_preamble is not None

    def confirm(self, preamble: str) -> None:
        """Promote to LIVE using the server's own preamble. The only such path.

        An empty preamble is refused rather than accepted-and-ignored:
        whitespace is not the server identifying itself, and a
        subscription confirmed by nothing is exactly the state this
        class exists to make unrepresentable.
        """
        if not preamble or not preamble.strip():
            raise LogsError(
                "the server sent an empty preamble: refusing to report this "
                "connection as live. Nothing identified the deployment, so there "
                "is nothing to trust about what follows."
            )
        if self.phase is Phase.ENDED:
            raise LogsError("cannot confirm a subscription that has already ended")
        self.server_preamble = preamble.strip()
        self.phase = Phase.LIVE

    def count_event(self) -> None:
        self.events += 1

    def count_status(self) -> None:
        self.status_lines += 1

    def end(self, reason: Ended, *, elapsed: float | None = None) -> None:
        self.elapsed = self._clock() - self._started if elapsed is None else elapsed
        self.ended_reason = reason
        self.phase = Phase.ENDED


def _filter_summary(sub: Subscription) -> str:
    shown = {k: v for k, v in sub.filters.items() if k != "format"}
    return " ".join(f"{k}={v}" for k, v in sorted(shown.items())) or "no filters (everything)"


def render_opening(sub: Subscription) -> str:
    """Printed before the request, so a hang has a visible subject.

    Everything here is what the client intends; nothing in it may be
    reused to claim the connection succeeded.
    """
    return (
        f"subscribing to {sub.api_base}{sub.path}  [{sub.env}]\n"
        f"  {_filter_summary(sub)}\n"
        f"  waiting for the server's banner..."
    )


def render_live(sub: Subscription) -> str:
    """The connected line, built only from what came back off the wire.

    Refuses on an unconfirmed subscription rather than falling back to
    ``sub.api_base``: that fallback would print a confident,
    correct-looking "connected" line for a socket that never produced
    a byte, indistinguishable from a genuine connection.
    """
    if not sub.confirmed:
        raise LogsError(
            "refusing to render a live line for an unconfirmed subscription: "
            "the server has not identified itself yet. (This is a bug if you "
            "are seeing it: nothing should reach here before the banner.)"
        )
    return f"LIVE: the server says: {sub.server_preamble}"


def render_ended(sub: Subscription) -> str:
    """How this stopped. Every branch says what the operator now knows.

    The two zero-event endings must not read alike: a quiet bus and a
    connection that never opened are the same picture at the terminal
    and different facts about the system.
    """
    seconds = f"{sub.elapsed:.1f}s"
    counted = f"{sub.events} events"

    if not sub.confirmed:
        return (
            f"NOT LISTENING: after {seconds} the server never sent its banner.\n"
            f"  You were not subscribed. This is not a quiet bus: nothing arrived "
            f"at all, not even the deployment header every stream opens with.\n"
            f"  Check `fm doctor --host {sub.env}`; if that is healthy, the "
            f"request reached something that is not this API."
        )

    if sub.ended_reason is Ended.SERVER_CLOSED:
        return (
            f"STREAM CLOSED BY THE SERVER after {seconds} ({counted}).\n"
            f"  This is not the same as a quiet stream: you have stopped being "
            f"told anything, and there is no replay, since the bus is "
            f"in-process and buffers nothing for a subscriber that has gone.\n"
            f"  Re-run to resubscribe. Events emitted in between are lost."
        )

    how = "you stopped it" if sub.ended_reason is Ended.INTERRUPTED else "the --for budget elapsed"
    tail = (
        f"  The bus was genuinely quiet: {sub.status_lines} keepalives arrived, so "
        f"the connection was live throughout and had nothing to report."
        if sub.events == 0
        else ""
    )
    return (
        f"done after {seconds} ({counted}), {how}.\n"
        f"  was listening to: {sub.server_preamble}"
        + (f"\n{tail}" if tail else "")
    )


def exit_code(sub: Subscription) -> int:
    """A status the caller can branch on, since the text scrolls away.

    A dropped connection must not exit 0.
    """
    if not sub.confirmed:
        return 2
    if sub.ended_reason is Ended.SERVER_CLOSED:
        return 1
    if sub.ended_reason is Ended.INTERRUPTED:
        return 130
    return 0


# ===========================================================================
# Wire -> operator
# ===========================================================================
def parse_sse(lines: Iterable[str]) -> Iterator[dict]:
    """Turn SSE frames into one record per frame.

    A line starting with ``:`` is an SSE comment and carries no data;
    sse-starlette's ``ping`` emits exactly that, and passing those
    through would put non-events into a stream whose only purpose is
    being machine-readable.

    A frame with no terminating blank line is still emitted: a stream
    ends when the connection does, not on a tidy boundary, so
    swallowing the last frame would lose data precisely when something
    went wrong.
    """
    event: str | None = None
    data: list[str] = []

    def flush() -> Iterator[dict]:
        nonlocal event, data
        if data:
            raw = "\n".join(data)
            try:
                parsed: object = json.loads(raw)
            except json.JSONDecodeError:
                # Carried as text rather than dropped: the operator can see it was
                # not JSON, where a silent drop just loses a frame.
                parsed = raw
            yield {"event": event or "message", "data": parsed}
        event, data = None, []

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line.startswith(":"):
            continue
        if not line:
            yield from flush()
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    yield from flush()


def consume(
    sub: Subscription, lines: Iterable[str], *, as_json: bool = False
) -> Iterator[tuple[Kind, str]]:
    """Drive ``sub`` over ``lines``, yielding ``(kind, text)``.

    Tagged rather than printed so the whole path is testable without
    capturing a terminal, and so the caller decides which stream each
    kind goes to: payload on stdout (pipeable), the subscription
    talking about itself on stderr (never in the data).

    Neither ``ready``/``status`` frames nor ``#`` status lines count
    as events. They are the connection describing itself, and counting
    them would make a perfectly idle stream report activity, the
    mirror image of the defect this command is built around.
    """
    if as_json:
        yield from _consume_json(sub, lines)
    else:
        yield from _consume_text(sub, lines)


def _consume_text(sub: Subscription, lines: Iterable[str]) -> Iterator[tuple[Kind, str]]:
    banner: list[str] = []
    for line in lines:
        if sub.phase is Phase.OPENING:
            if line.startswith(BANNER_HEAD):
                banner.append(line)
                continue
            if banner and line.startswith(BANNER_MODE):
                banner.append(line)
                sub.confirm("\n".join(banner))
                yield (STATE, render_live(sub))
                continue
            if banner:
                # A banner head with no mode line: confirm on what did arrive
                # rather than discard it, then fall through and treat this line as
                # ordinary traffic.
                sub.confirm("\n".join(banner))
                yield (STATE, render_live(sub))
        if line.startswith("#"):
            sub.count_status()
            yield (STATE, line)
            continue
        sub.count_event()
        yield (OUT, line)


def _consume_json(sub: Subscription, lines: Iterable[str]) -> Iterator[tuple[Kind, str]]:
    for record in parse_sse(lines):
        name = record.get("event")
        if name == "ready" and sub.phase is Phase.OPENING:
            sub.confirm(_render_ready(record.get("data")))
            yield (STATE, render_live(sub))
            continue
        if name == "status":
            sub.count_status()
            yield (STATE, json.dumps(record, ensure_ascii=False, sort_keys=True))
            continue
        sub.count_event()
        yield (OUT, json.dumps(record, ensure_ascii=False, sort_keys=True))


def _render_ready(data: object) -> str:
    """Render the ``ready`` frame's deployment identity as one line.

    Serialised from the frame rather than composed from what was asked
    for: the key names in the output are the server's, so a reader can
    tell this came off the wire.
    """
    if isinstance(data, dict):
        deployment = data.get("deployment")
        if isinstance(deployment, dict) and deployment:
            body = " ".join(f"{k}={v}" for k, v in sorted(deployment.items()))
            since = data.get("events_since_boot")
            tail = f", {since} events since boot" if since is not None else ""
            return f"ready: {body}{tail}"
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def follow(
    lines: Iterable[str],
    *,
    stop_after: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_expire: Callable[[], None] | None = None,
) -> Iterator[str]:
    """Yield ``lines`` until ``stop_after`` seconds have passed.

    The clock is injected so the bound can be tested without waiting.

    ``stop_after=0`` stops immediately and is not treated as
    unbounded: falsy-vs-``None`` is exactly where a bound silently
    becomes no bound.

    ``on_expire`` fires only when the deadline stopped this, never
    when the input ran out: one is success and one is a dropped
    connection, and from outside a generator they look identical.

    The check happens per arriving line, so ``--for N`` fires within
    one keepalive period of N, not at N exactly. That is inherent to a
    blocking iterator, and it is a second reason ``--keepalive 0`` is
    refused: with no keepalive there is no next line to check the
    clock on.
    """
    if stop_after is None:
        yield from lines
        return
    deadline = clock() + stop_after
    for line in lines:
        if clock() >= deadline:
            if on_expire is not None:
                on_expire()
            return
        yield line


# ===========================================================================
# CLI
# ===========================================================================
_HELP = "live tap on the server's whole event bus (admin, Global-only)"

_DESCRIPTION = """Subscribe to the server's event bus and print what it emits, live.

This is a tap, not a log. It shows what happens from now on; nothing here can
tell you what happened yesterday. The bus is in-process and touches no
database, so nothing is buffered and there is no --since to give you; for
history use `fm audit-log`, `fm chats` or `fm connectors`.

/admin/events is served from the Global region only; --env ae answers 410 and
is refused before sending.

Subjects:
  stream    (default) the live firehose
  rates     one-shot: what is this server repeating right now
  jobs      one-shot: per background job, last run, counts, failures, no-ops
  activity  one-shot: what is in flight right now, plus outbound-call counters
  types     one-shot: the event-type vocabulary, for filling in --types
"""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject",
        nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--types", help="CSV of event types; 'on_meeting_*' and '@background' allowed")
    parser.add_argument("--exclude", help="CSV of event types to drop (same glob rules)")
    parser.add_argument("--space", help="only this space's events (UUID)")
    parser.add_argument("--source", help="CSV: scheduler,api,webhook,internal")
    parser.add_argument(
        "--work-only",
        action="store_true",
        help="hide ticks that ran but moved nothing",
    )
    parser.add_argument(
        "--mode",
        choices=("raw", "digest", "both", "jobs", "activity"),
        default=None,
        help="stream shape (default: raw); digest catches a repeating loop raw lines cannot show",
    )
    parser.add_argument("--json", action="store_true", help="SSE JSON records instead of text lines")
    parser.add_argument(
        "--for",
        dest="for_seconds",
        type=float,
        default=None,
        help="stop after N seconds (fires on the next line, so within one keepalive of N)",
    )
    parser.add_argument(
        "--keepalive",
        type=int,
        default=30,
        help=(
            "server status-line period, seconds. 0 is refused for a text stream: "
            "with nothing on the wire, idle and dropped are indistinguishable"
        ),
    )
    parser.add_argument("--interval", type=int, default=None, help="digest/jobs emit period, seconds")


def add_parser(subparsers) -> argparse.ArgumentParser:
    """Register ``fm logs``."""
    parser = subparsers.add_parser(
        "logs",
        help=_HELP,
        description=_DESCRIPTION,
        epilog=STREAM_CARRIES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    """The same parser, detached, so ``--help`` can be rendered in a test."""
    return _build_standalone_parser(add_parser)


def run(args) -> int:
    """Resolve the target, subscribe, and report honestly."""
    subject = canonical_subject(getattr(args, "subject", None))
    as_json = bool(getattr(args, "json", False))
    keepalive = int(getattr(args, "keepalive", 30))
    require_liveness_signal(keepalive, as_json)

    ctx = context.resolve(args, need_token=True)
    target = ctx.target
    require_admin_region(target)
    token = ctx.token
    assert token is not None  # need_token=True guarantees it; narrows for pyright

    params = build_params(
        types=getattr(args, "types", None),
        exclude=getattr(args, "exclude", None),
        project=ctx.space,
        source=getattr(args, "source", None),
        only_work=bool(getattr(args, "work_only", False)),
        mode=getattr(args, "mode", None),
        as_json=as_json,
        interval=getattr(args, "interval", None),
        keepalive=keepalive if subject == "stream" else None,
    )

    if subject != "stream":
        # The one-shot views are a plain GET; there is no subscription to be
        # ambiguous about, so none of the machinery above applies.
        body = api.get(target, subject_path(subject), token=token, params=params)
        print(body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False))
        return 0

    print(LIVE_ONLY_NOTE, file=sys.stderr)
    return _run_stream(target, subject_path(subject), token=token, params=params,
                       as_json=as_json, keepalive=keepalive,
                       for_seconds=getattr(args, "for_seconds", None))


def _run_stream(
    target: Target,
    path: str,
    *,
    token: str,
    params: dict[str, str],
    as_json: bool,
    keepalive: int,
    for_seconds: float | None,
) -> int:
    sub = Subscription(env=target.env, api_base=target.api_base, path=path, filters=params)
    print(render_opening(sub), file=sys.stderr)

    hit_deadline = False

    def expired() -> None:
        nonlocal hit_deadline
        hit_deadline = True

    try:
        with api.stream(
            target, path, token=token, params=params,
            read_timeout=read_timeout_for(keepalive, as_json),
        ) as lines:
            bounded = follow(lines, stop_after=for_seconds, on_expire=expired)
            for kind, text in consume(sub, bounded, as_json=as_json):
                if kind is OUT:
                    print(text, flush=True)
                else:
                    print(text, file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        sub.end(Ended.INTERRUPTED)
    except api.StreamClosed as exc:
        # Loud, and distinct from a quiet stream: the transport already knows this
        # was not a tidy end, so do not let it arrive as a plain iterator stop.
        print(f"error: {exc}", file=sys.stderr)
        sub.end(Ended.SERVER_CLOSED)
    else:
        sub.end(Ended.DEADLINE if hit_deadline else Ended.SERVER_CLOSED)

    print(render_ended(sub), file=sys.stderr)
    return exit_code(sub)
