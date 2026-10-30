"""``fm kb`` — ad-hoc knowledge-base search and read, from the operator's terminal.

Absorbs two scripts (EV-ABSORB.md §3.1, both "no new endpoint" — an existing
route already does the work):

* the retired ``kb_search.py`` — fanned several query terms over the project's
  synced knowledge base, aggregating hits by file client-side.
* the retired ``kb_read.py`` — matched a needle against a local Postgres query for
  ``File.path``, then read ``File.cached_md_path`` straight off local disk.

Both are now a thin client over the SAME member-gated routes the product uses:

* ``fm kb search <term>... --space <id>`` -> ``GET /projects/{id}/search?q=...``
  (``services/search.py::search_project`` via ``search_for_space``). The terms
  are joined with spaces into ONE query string, not sent one call per term: that
  module's own docstring says ranking is already per-term server-side (a hit
  matching more of the terms scores higher), so client-side aggregation across
  separate calls would just be redundant network round-trips for an answer the
  server already computes in one.
* ``fm kb read <needle> --space <id>`` -> the SAME search, then
  ``GET /projects/{id}/files/{file_id}/content`` (``services/user_acl.py::
  read_file_for_space``) for the first hit that actually carries a ``file_id``
  (a message or SharePoint-native hit does not, and is skipped rather than sent
  as a bogus UUID path param). This is a DELIBERATE change from the script it
  replaces — EV-ABSORB.md calls this one out by name as the case where the
  absorption should switch to the correct path rather than copy the old
  mechanism: a local ``cached_md_path`` read only ever worked from a box with
  that disk mounted (and in prod that disk has no volume behind it at all — see
  ``routes/meetings_center.py``'s ``get_meeting_transcript_pdf`` docstring for
  the same finding elsewhere), where the content route is blob-backed and
  reachable from anywhere ``ev`` runs. A needle that names a file (e.g. a
  filename fragment) still resolves it: filename hits are boosted server-side.

Read-only. There is no flag that makes either subject write.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

SUBJECTS = ("search", "read")

#: Mirrors the retired ``kb_read.py``'s own default truncation length.
DEFAULT_MAX_CHARS = 4000

#: Mirrors ``services/search.py``'s route-level default page size.
DEFAULT_SEARCH_LIMIT = 20


class KbError(Exception):
    """A refusal about the knowledge-base surface, written for an operator."""


def _subject_error(subject: object) -> KbError:
    return KbError(
        f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
        "  search  <term>...   rank the project's KB by these terms (space-joined)\n"
        "  read    <needle>    search for needle, then print the top file's content"
    )


_DESCRIPTION = """Ad-hoc knowledge-base search / read, over the SAME routes the
product's own search box and file viewer use — never a local disk or DB read.

  fm kb search <term>... --space <id>   rank the KB by these terms
  fm kb read <needle> --space <id>      search, then print the top file's content

Read-only. There is no flag that makes either subject write.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "kb",
        help="ad-hoc knowledge-base search / read (read-only)",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    from formist_cli.cli import _add_env, _add_space

    _add_env(parser)
    _add_space(parser)
    parser.add_argument("subject", help=f"one of: {', '.join(SUBJECTS)}")
    parser.add_argument(
        "query", nargs="+",
        help="search: one or more terms (space-joined into one query). "
             "read: a single needle (extra words are folded into the query too).",
    )
    parser.add_argument(
        "--max-chars", dest="max_chars", type=int, default=DEFAULT_MAX_CHARS,
        help=f"read: truncate the printed body to this many chars (default {DEFAULT_MAX_CHARS})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="search: cap the number of hits requested from the server",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


# ===========================================================================
# search
# ===========================================================================
def render_search(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return "(no hits)"
    lines = [f"=== {len(items)} hit(s) (of {payload.get('total', len(items))}) ==="]
    for item in items:
        label = item.get("path") or item.get("subject") or item.get("name") or "?"
        lines.append(f"[{item.get('source', '?')}] {label}")
        if item.get("snippet"):
            lines.append(f"    …{item['snippet']}…")
    return "\n".join(lines)


def _search(ctx: context.Context, query: str, *, limit: int | None) -> dict:
    params: dict[str, str] = {"q": query}
    if limit is not None:
        params["limit"] = str(limit)
    payload = api.get(ctx.target, f"/projects/{ctx.space}/search", token=ctx.token, params=params)
    if not isinstance(payload, dict):
        raise KbError(
            f"expected a JSON object from /projects/{{id}}/search, got "
            f"{type(payload).__name__}. That is a server contract change, not an empty result."
        )
    return payload


def _run_search(ctx: context.Context, args) -> int:
    query = " ".join(args.query)
    payload = _search(ctx, query, limit=args.limit)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_search(payload))
    return 0


# ===========================================================================
# read
# ===========================================================================
def _top_file_hit(items: list) -> dict | None:
    """The first hit that actually names a ``file_id`` — a message or
    SharePoint-native hit does not, and cannot be read through
    ``/files/{id}/content`` (that path param is a UUID; sending anything else
    422s instead of cleanly not-found)."""
    for item in items:
        if isinstance(item, dict) and item.get("file_id"):
            return item
    return None


def _run_read(ctx: context.Context, args) -> int:
    needle = " ".join(args.query)
    payload = _search(ctx, needle, limit=args.limit)
    hit = _top_file_hit(payload.get("items") or [])
    if hit is None:
        raise KbError(f"no file hit for {needle!r} — nothing to read.")
    file_id = str(hit["file_id"])
    content = api.get(
        ctx.target, f"/projects/{ctx.space}/files/{file_id}/content", token=ctx.token
    )
    if not isinstance(content, dict):
        raise KbError(
            f"expected a JSON object from /files/{{id}}/content, got "
            f"{type(content).__name__}."
        )
    if args.json:
        print(json.dumps(content, indent=2, ensure_ascii=False))
        return 0
    text = content.get("markdown") or ""
    path = content.get("path") or content.get("name") or file_id
    print(f"### {path}  ({len(text)} chars)\n")
    print(text[: args.max_chars])
    if len(text) > args.max_chars:
        print(f"\n…[truncated, {len(text) - args.max_chars} more chars]")
    return 0


def run(args) -> int:
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in SUBJECTS:
        raise _subject_error(getattr(args, "subject", None))
    ctx = context.resolve(args, need_space=True, need_token=True)
    if subject == "search":
        return _run_search(ctx, args)
    return _run_read(ctx, args)
