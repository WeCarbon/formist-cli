"""Reading an agent run, and never overstating how much of it you read.

A background agent run persists as an ordinary chat, same tables and
transcript shape as an interactive conversation, just a different kind.
They are not uniformly space-wide: a scheduler- or webhook-initiated run is
workspace-scoped, while a run a person asked for is private to them. Both
are background agents, so "I can see it in the list" and "I can read its
transcript" are different questions with different answers, routinely, for
the same kind of row. A space-wide run is in turn readable only by project
admins and owners, not by every member, so a member session can see an
empty list in a space that plainly has agent runs, and nothing is wrong.
That is a third answer to "why did I see nothing", alongside "no runs
exist" and "they are someone else's private runs", and it is
indistinguishable from either without knowing your role.

This reads the member route rather than the admin one, because the admin
listing has no privacy predicate and returns metadata only, with no admin
route for chat messages at all; a listing from there could show a chat
whose transcript then 404s. The member route lists exactly what the
transcript route will serve. A 404 on another member's private chat is
deliberate (never a 403, so the API never reveals that the chat exists),
and `fm` does not route around it; what it adds is saying so, because a
404 on an id you were just shown otherwise reads as a broken tool.

The other thing this module is built around: the message-listing endpoint
caps a page, and an agent run can exceed it in turns. A page that measured
itself must say so rather than let a capped page read as the whole
transcript. So completeness is treated as a claim, sourced from the
server's reported total and from nothing else. :attr:`Transcript.complete`
is False when that total is absent, because "I could not tell" and "it is
complete" are different facts, and the natural shorthand
(``len(messages) >= (total or 0)``) collapses them in the unsafe
direction, reporting complete for every truncated page it cannot measure.

Stdlib only: `fm` must add no runtime dependency.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from formist_cli import context
from formist_cli.cli_support import default_fetch as _default_fetch
from formist_cli.targets import Target, require_space_id

#: What ``parse_pagination`` clamps a page to (``utils/list_query.py:60``). Asking
#: for more is silently reduced, so this is the real page size and paging is the
#: only way past it.
MESSAGE_PAGE = 200

#: Safety bound on the paging walk. A server that keeps claiming more rows than it
#: returns is a server bug; spinning forever in the client is a worse way to find
#: out than stopping and reporting the transcript as incomplete.
MAX_PAGES = 200

#: The command line speaks kebab-case, the API speaks the ``ChatKind`` enum.
_KIND_ALIASES = {
    "background-agent": "BACKGROUND_AGENT",
    "user-chat": "USER_CHAT",
}

VALID_KINDS = tuple(sorted(set(_KIND_ALIASES.values())))


class ChatsError(Exception):
    """A refusal about chats, written for an operator.

    Deliberately NOT an ``api.ApiError`` subclass: a caller distinguishing "the
    server said something" from "fm decided something" must be able to, and a
    privacy explanation is the second kind.
    """


Fetcher = Callable[..., Any]


def canonical_kind(kind: str | None) -> str | None:
    """Normalise a ``--kind`` value to the enum spelling. ``None`` stays ``None``.

    Both spellings are accepted because both are things a person reasonably types;
    an unrecognised one is refused rather than passed through, since the server
    would answer 422 and the operator would be debugging the wrong layer.
    """
    if kind is None:
        return None
    raw = kind.strip()
    mapped = _KIND_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    if raw.upper() in VALID_KINDS:
        return raw.upper()
    raise ChatsError(
        f"unknown chat kind {kind!r}. Valid: "
        f"{', '.join(sorted(_KIND_ALIASES))} (or the enum spellings "
        f"{', '.join(VALID_KINDS)})."
    )


@dataclass(frozen=True)
class Transcript:
    """A chat's messages, and an honest account of whether that is all of them.

    :attr:`total` is the server's count, or ``None`` when the envelope carried
    none. It is never defaulted to a number: a default would make the completeness
    test answerable in a case where nothing answered it.
    """

    messages: list[dict]
    total: int | None

    @property
    def complete(self) -> bool:
        """True only when the server stated a total and we hold all of it."""
        if self.total is None:
            return False
        return len(self.messages) >= self.total

    def render(self) -> str:
        """The transcript, with its completeness stated rather than implied."""
        lines = [
            f"[{m.get('created_at', '')}] {str(m.get('role', '?')):<9} "
            f"{m.get('content', '')}"
            for m in self.messages
        ]
        lines.append("")
        if self.total is None:
            lines.append(
                f"{len(self.messages)} turn(s) shown. The server reported no "
                "total, so fm could not tell whether this is the whole "
                "transcript.\n"
                "-> `--all-messages` pages until the server stops adding rows."
            )
        elif not self.complete:
            lines.append(
                f"showing {len(self.messages)} of {self.total} turns: this "
                f"transcript is INCOMPLETE. The list endpoint caps a page at "
                f"{MESSAGE_PAGE}.\n"
                "-> `--all-messages` to page through the rest."
            )
        else:
            lines.append(f"{self.total} turn(s), the whole transcript.")
        return "\n".join(lines)


def _envelope(payload: object, *, what: str) -> tuple[list[dict], int | None]:
    """``(items, total)`` from a list envelope; ``total`` stays ``None`` if absent."""
    if not isinstance(payload, dict):
        raise ChatsError(
            f"listing {what} returned {type(payload).__name__}, not a list envelope."
        )
    items = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
    total = payload.get("total")
    return items, total if isinstance(total, int) else None


def list_chats(
    target: Target,
    space: str,
    *,
    token: str | None,
    kind: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    fetch: Fetcher | None = None,
) -> tuple[list[dict], int | None]:
    """The chats this session may read in ``space``, newest first.

    Filtered server-side. The ``?kind=`` filter is applied as a view filter
    layered on the privacy predicate; it never widens what is visible.
    Re-deriving that filter here would both measure the page and put a
    second copy of a rule next to the one the server owns.
    """
    space = require_space_id(space)
    fetch = fetch or _default_fetch

    params = {"limit": str(limit), "offset": str(offset)}
    canonical = canonical_kind(kind)
    if canonical:
        params["kind"] = canonical
    if archived:
        params["archived"] = "true"

    payload = fetch(
        target, f"/projects/{space}/chats", token=token, params=params
    )
    return _envelope(payload, what="chats")


def latest_chat(
    target: Target,
    space: str,
    *,
    token: str | None,
    kind: str | None = None,
    fetch: Fetcher | None = None,
) -> dict:
    """The most recently updated readable chat, or a refusal.

    Exists to remove a transcription: the common question is "what did the
    last run do", and answering it by copying a UUID off the line above is
    the failure this is meant to end.

    The ordering is the server's (``updated_at`` desc, then ``created_at``
    desc, then ``id``), and this takes the first row rather than re-sorting
    locally. A second ordering is a second answer, and when the two
    disagree the operator is shown a different run than the one they asked
    for, with nothing indicating it.
    """
    items, _total = list_chats(
        target, space, token=token, kind=kind, limit=1, fetch=fetch
    )
    if not items:
        described = f" of kind {kind!r}" if kind else ""
        raise ChatsError(
            f"no readable chat{described} in this space ({target.name}).\n"
            "A background run triggered by another member is private to them and "
            "is not listed here, see `fm chats --help`."
        )
    return items[0]


def get_chat(
    target: Target,
    space: str,
    chat_id: str,
    *,
    token: str | None,
    fetch: Fetcher | None = None,
) -> dict:
    """One chat's metadata, translating a 404 into the rule that caused it.

    ``GET /chats/{id}`` returns an envelope, ``{"chat": {...}, "messages":
    [...]}``, not a bare chat, unlike the list endpoint's items. This
    refuses rather than falling back to ``.get("chat", payload)``, because
    accepting both shapes would let a mismatch between the two render every
    field as ``None`` instead of raising. The ``messages`` the envelope
    carries are a capped recent tail with no ``total`` and are deliberately
    ignored: a transcript comes from the paginated endpoint, which can say
    how much of it you have.
    """
    space = require_space_id(space)
    fetch = fetch or _default_fetch
    try:
        payload = fetch(
            target, f"/projects/{space}/chats/{chat_id}", token=token
        )
    except Exception as exc:  # narrowed immediately by _explain_404
        _explain_404(exc, target, chat_id)
        raise
    if not isinstance(payload, dict):
        raise ChatsError(f"chat {chat_id} returned {type(payload).__name__}.")
    chat = payload.get("chat")
    if not isinstance(chat, dict):
        raise ChatsError(
            f"chat {chat_id}: the response has no 'chat' object "
            f"(keys: {sorted(payload)}).\n"
            "GET /chats/{id} is expected to return {'chat': ..., 'messages': ...}. "
            "Refusing rather than rendering every field as None."
        )
    return chat


def fetch_messages(
    target: Target,
    space: str,
    chat_id: str,
    *,
    token: str | None,
    all_pages: bool = False,
    limit: int = MESSAGE_PAGE,
    fetch: Fetcher | None = None,
) -> Transcript:
    """A chat's turns, and whether they are all of them.

    One page by default. A transcript is the largest thing this tool prints
    and the endpoint caps a page anyway, so paging is opt-in, but a partial
    result is always marked partial, which is the part that is not optional.
    """
    space = require_space_id(space)
    fetch = fetch or _default_fetch
    path = f"/projects/{space}/chats/{chat_id}/messages"

    messages: list[dict] = []
    total: int | None = None
    offset = 0

    for _page in range(MAX_PAGES):
        try:
            payload = fetch(
                target,
                path,
                token=token,
                params={"limit": str(limit), "offset": str(offset)},
            )
        except Exception as exc:
            _explain_404(exc, target, chat_id)
            raise

        items, total = _envelope(payload, what="messages")
        messages.extend(items)

        if not all_pages:
            break
        # Stop on a page that adds nothing, even if `total` still claims more.
        # The alternative is trusting a count over the rows actually delivered,
        # which spins forever against a server that is wrong about one of them.
        if not items or total is None or len(messages) >= total:
            break
        offset += len(items)

    return Transcript(messages=messages, total=total)


def _explain_404(exc: Exception, target: Target, chat_id: str) -> None:
    """Re-raise a 404 as the access rule behind it. Anything else is left alone.

    Only a 404 carries this meaning. Attaching the privacy explanation to
    any other status would be a confident wrong answer, which costs more
    than the bare status it replaced: the operator stops looking for the
    real cause.
    """
    from formist_cli import api

    if not isinstance(exc, api.ApiError) or getattr(exc, "status", None) != 404:
        return
    raise ChatsError(
        f"404 from {target.name} for chat {chat_id}.\n"
        "This is one of two things, and the server will not say which:\n"
        "  - no such chat in this space; or\n"
        "  - the chat is private to another member (owner_scope=USER, user_id "
        "not you).\n"
        "\n"
        "The second is deliberate: the API answers 404 rather than 403 so it "
        "never reveals that another member's private chat exists, and being a "
        "platform-admin does not widen this.\n"
        "\n"
        "A background run is private when a person triggered it directly; "
        "scheduler- and webhook-initiated runs are workspace-scoped instead. "
        "Both are background-agent rows, so this 404 is an ordinary outcome, "
        "not an edge case.\n"
        "\n"
        "A workspace-scoped background run is also 404 unless your session is "
        "an admin or owner of the space. If `fm chats` shows you no runs at "
        "all in a space that plainly has them, that is the likeliest reason.\n"
        "-> `fm chats` lists what this session may actually read."
    ) from None


# ===========================================================================
# The CLI surface. Per-module `add_parser` is the convention: it is the shape
# that survives a three-way merge, since adding a command then costs the
# top-level parser one line instead of a block two branches both land in.
# ===========================================================================
def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space
    p = sub.add_parser(
        "chats",
        help="list a space's chats / agent runs, or read one's transcript",
        description=(
            "Every agent run persists as a chat, so this reads both the "
            "background agents list and an interactive conversation.\n\n"
            "Omit the id to list; give one to show it. --messages adds the "
            "transcript.\n\n"
            "Scope: this reads the member route, so it shows exactly what your "
            "session may read: your own chats plus every workspace-scoped one. A "
            "background run someone else triggered is private to them and "
            "answers 404, deliberately, so the API never reveals it exists. "
            "Platform-admin does not bypass that; there is no admin route for "
            "chat messages at all.\n\n"
            "A transcript can be long and the list endpoint caps a page at 200. "
            "A partial transcript is always marked partial; --all-messages "
            "pages through the rest."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument(
        "chat_id", nargs="?", help="chat UUID (from `fm chats`); omit to list"
    )
    p.add_argument("--kind", help="background-agent | user-chat (default: both)")
    p.add_argument("--archived", action="store_true", help="list archived chats")
    p.add_argument(
        "--latest",
        action="store_true",
        help="address the newest matching chat instead of naming an id",
    )
    p.add_argument(
        "--messages", action="store_true", help="also print the transcript (one page)"
    )
    p.add_argument(
        "--all-messages",
        dest="all_messages",
        action="store_true",
        help="page the WHOLE transcript, however long",
    )
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true", help="machine-readable output")


def cmd_chats(args) -> int:
    """List the space's chats, or show one, and never overstate a transcript.

    Listing and showing are one command because they are one question asked at two
    zoom levels, and because the id that identifies a chat comes from the listing
    directly above it. `--latest` removes even that transcription for the common
    case ("what did the last run do"), and prints the id it resolved to so the
    answer is checkable.

    There is no resolve-by-title. Chat titles are auto-generated and
    duplicate heavily, since every run of one connector sync shares a
    title, so an exactly-one-or-refuse rule would correctly refuse almost
    every time, which is a command that looks broken rather than one that
    is careful.
    """
    # Lazy: `cli` imports this module, so a module-level import here would
    # be a cycle.

    eff = context.resolve(args, need_space=True, need_token=True)
    token = eff.token
    chat_id = args.chat_id

    if args.latest:
        if chat_id:
            raise ChatsError(
                "give a chat id or --latest, not both; they are two ways of "
                "naming the same argument, and if they disagree there is no "
                "sensible winner."
            )
        chat = latest_chat(
            eff.target, eff.space, token=token, kind=args.kind
        )
        chat_id = str(chat.get("id"))
        # To stderr, and printed before anything else: --latest resolved an
        # argument the operator did not type, so the value it chose is part of
        # the answer, not a detail.
        print(f"  --latest resolved to chat {chat_id}", file=sys.stderr)

    if not chat_id:
        return _list_chats(args, eff, token)

    chat = get_chat(eff.target, eff.space, chat_id, token=token)
    transcript = None
    if args.messages or args.all_messages:
        transcript = fetch_messages(
            eff.target,
            eff.space,
            chat_id,
            token=token,
            all_pages=args.all_messages,
        )

    if args.json:
        out = dict(chat)
        if transcript is not None:
            out["messages"] = transcript.messages
            out["messages_total"] = transcript.total
            # The completeness verdict travels WITH the data. A JSON consumer
            # cannot see the warning line render() prints, and a truncated list
            # with no flag beside it is the failure this command is built around.
            out["messages_complete"] = transcript.complete
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"{chat.get('id')}  {chat.get('kind', '?')}")
    print(f"  title        {chat.get('title')}")
    print(f"  owner_scope  {chat.get('owner_scope', '?')}"
          f"   user_id={chat.get('user_id')}")
    if chat.get("kind") == "BACKGROUND_AGENT":
        print(f"  agent_status {chat.get('agent_status', '?')}")
        print(f"  triggered_by {chat.get('triggered_by_name') or chat.get('trigger_kind') or '?'}")
        if chat.get("last_error"):
            print(f"  last_error   {chat.get('last_error')}")
    print(f"  updated_at   {chat.get('updated_at', '')}")

    if transcript is not None:
        print()
        print(transcript.render())
    return 0


def _list_chats(args, eff, token) -> int:
    items, total = list_chats(
        eff.target,
        eff.space,
        token=token,
        kind=args.kind,
        archived=args.archived,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(
            {"items": items, "total": total}, indent=2, ensure_ascii=False, default=str
        ))
        return 0

    for item in items:
        status = item.get("agent_status") or ""
        print(f"{item.get('id')}  {str(item.get('kind', '?')):<16} {status:<10}"
              f" {item.get('updated_at', ''):<28} {item.get('title') or ''}")
    if not items:
        print("(no readable chats in this space)")
    # Say what was NOT shown. The same rule the transcript keeps: a page that
    # measured itself must not read as the table.
    if isinstance(total, int) and total > len(items):
        print(
            f"\nshowing {len(items)} of {total}, raise --limit to see more.",
            file=sys.stderr,
        )
    return 0
