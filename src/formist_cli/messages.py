"""``fm messages`` — the group-mailbox message surface: duplicate cleanup
(``dedupe-scan`` / ``dedupe``) and the inline-image backfill
(``refresh-inline-images``).

    GET  /admin/messages/dedupe-scan                         read-only census, every OUTLOOK connection
    POST /admin/messages/dedupe                               dry run unless --apply
    POST /admin/spaces/{space_id}/messages/refresh-inline-images   dry run unless --apply

WHAT THE DUPLICATES ARE (dedupe-scan / dedupe). `services/message_sync.py`
decided "does this conversation already have a row?" with a SELECT and then
INSERTed — against a NON-unique index, while two schedulers called it
concurrently. Two passes that both missed wrote two rows; every later pass
then updated whichever one came back first, and the other froze where it
was. Measured on prod 2026-09-07 (a real space / ae): 248 EMAIL rows, 69
duplicate groups covering 205 of them, 136 orphans, and 112 of the 117
messages that had stopped syncing sitting inside one of those groups.

The race itself is fixed. `dedupe`/`dedupe-scan` is the cleanup for what it
already wrote.

⚠️ `dedupe` WRITES when given --apply. It SOFT-deletes (`deleted_at` +
`deleted_by`), never hard-deletes, so clearing those two columns restores
every retired row — but it hides rows a user can see, and it leaves a
`platform_audit_log` entry either way.

READ FIRST, THEN ACT. `dedupe-scan` is read-only and answers the question the
freshness probe explicitly left open: it measured ONE space, while the defect
was architectural and applies to any project with a group mailbox. Run it
before authorising a write anywhere.

⚠️ REGION (dedupe/dedupe-scan). `message` and `connection` are region-local,
so `--env ae` for a real AE-region space. `--host evane` would read the control plane's own
database and honestly report zero.

WHAT THE INLINE-IMAGE BACKFILL IS (refresh-inline-images). Outlook group-
conversation posts reference inline images (a company logo, social-media
icons in a signature) as ``<img src="cid:...">`` — a scheme a browser cannot
resolve on its own. ``services/message_sync.py`` now rewrites those
references to ``data:`` URIs at SYNC time, but that only reaches a
conversation the next time it is actually re-synced. This backfill re-syncs
already-stored messages BY THEIR OWN remote_id (never Graph's
``/conversations`` LIST — D319), so it also reaches a conversation Graph's
list endpoint has since gone quiet on.

DRY RUN BY DEFAULT for `refresh-inline-images` too — ``--apply`` flips it,
same convention as `dedupe`. Idempotent: a message whose body_html was
already rewritten (no more ``cid:``) is not a candidate on the next dry run.

``api.admin_post``/``api.admin_get``, not the plain ``api`` client — this is
the GLOBAL-only ``/admin`` surface, same rule as ``fm i18n-backfill`` /
``fm space-plan``.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.targets import require_space_id

SUBJECTS = ("dedupe-scan", "dedupe", "refresh-inline-images")
DEFAULT_SUBJECT = "dedupe-scan"

SCAN_ENDPOINT = "/admin/messages/dedupe-scan"
DEDUPE_ENDPOINT = "/admin/messages/dedupe"
REFRESH_INLINE_IMAGES_ENDPOINT = "/admin/spaces/{space}/messages/refresh-inline-images"


class MessagesError(Exception):
    """A refusal about this surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise MessagesError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}."
        )
    return value


# ===========================================================================
# Rendering — dedupe-scan / dedupe
# ===========================================================================
def render_scan(payload: dict) -> str:
    lines = [
        f"OUTLOOK connections: {payload.get('connections', 0)}"
        f"   with duplicates: {payload.get('connections_with_duplicates', 0)}",
        f"duplicate groups:    {payload.get('groups', 0)}"
        f"   rows a dedupe would retire: {payload.get('rows_to_soft_delete', 0)}",
        "",
    ]
    rows = payload.get("detail") or []
    if not rows:
        return "\n".join(lines) + "no OUTLOOK connections on this instance."
    lines.append(f"{'connection':38s} {'groups':>7s} {'in groups':>10s} {'retire':>7s}")
    for row in rows:
        lines.append(
            f"{row.get('connection_id', ''):38s} {row.get('groups', 0):7d} "
            f"{row.get('rows_in_groups', 0):10d} {row.get('rows_to_soft_delete', 0):7d}"
        )
    return "\n".join(lines)


def render_dedupe(payload: dict) -> str:
    applied = bool(payload.get("applied"))
    head = "APPLIED" if applied else "DRY RUN — nothing was written"
    lines = [
        f"{head}   connection={payload.get('connection_id')}",
        f"groups={payload.get('groups', 0)}  rows in groups={payload.get('rows_in_groups', 0)}"
        f"  rows to retire={payload.get('rows_to_soft_delete', 0)}",
        "",
        "dependents of the rows that would be retired:",
    ]
    for key, value in (payload.get("dependents") or {}).items():
        lines.append(f"    {key:28s} {value}")
    lines.append("")
    for group in payload.get("detail") or []:
        lines.append(f"  {group.get('remote_id')}")
        lines.append(
            f"      KEEP {group.get('keep')}  last_synced={group.get('keep_last_synced_at')}"
        )
        # strict=True on purpose: the two lists are built from the same loop in
        # services/message_dedupe.py and a length mismatch would mean the plan is
        # describing a row it did not measure. Better a traceback than a report
        # that silently drops the last dropped row.
        for row_id, synced in zip(
            group.get("drop") or [],
            group.get("drop_last_synced_at") or [],
            strict=True,
        ):
            lines.append(f"      drop {row_id}  last_synced={synced}")
    if not applied and payload.get("plan_token"):
        lines.append("")
        lines.append(
            f"plan token:   {payload['plan_token']}   (quote it back: "
            f"--plan-token {payload['plan_token']})"
        )
        lines.append(
            "              ⚠ it fingerprints the keep/drop of the group(s) you "
            "named. A sync tick"
        )
        lines.append(
            "                that makes a DIFFERENT row the keeper changes it, and "
            "the apply is refused 409."
        )
    if applied:
        lines.append("")
        lines.append(f"reason:       {payload.get('reason')!r}")
        lines.append(f"groups applied: {payload.get('groups_applied')}")
        if payload.get("groups_skipped"):
            lines.append(
                f"groups NOT named (left alone — pass --group to include): "
                f"{payload.get('groups_skipped')}"
            )
        if payload.get("unknown_groups"):
            lines.append(
                f"⚠ requested groups that are not currently duplicates (already "
                f"merged, or a mistyped remote_id): {payload.get('unknown_groups')}"
            )
        lines.append(f"soft-deleted: {payload.get('soft_deleted', 0)}")
        lines.append(f"re-pointed:   {payload.get('repointed')}")
        conflicts = payload.get("status_point_conflicts") or 0
        if conflicts:
            lines.append(
                f"status points NOT re-pointed (the keeper already has one at that "
                f"stage): {conflicts}"
            )
    return "\n".join(lines)


# ===========================================================================
# Rendering — refresh-inline-images
# ===========================================================================
def render_refresh_inline_images(payload: dict) -> str:
    """The report. Target and mode first, then whatever this run's shape carries."""
    target = payload.get("target") or {}
    lines = [
        f"space     {target.get('space_name') or '?'}  ({target.get('space_id')})",
        f"project   {target.get('project_name') or '?'}  ({target.get('project_id')})",
        f"region    {target.get('region')}   (resolved by the server from the "
        "Global space registry — not from --env)",
        "",
        f"mode          {'DRY RUN (writes nothing)' if payload.get('dry_run') else 'APPLIED'}",
        f"connections   {payload.get('connections', 0)} OUTLOOK connection(s) in this space",
        f"candidates    {payload.get('candidates', 0)} message(s) with a dangling cid:",
    ]

    if payload.get("dry_run"):
        samples = payload.get("sample_remote_ids") or []
        if samples:
            lines += ["", "sample remote_ids (up to 20):"]
            lines += [f"  {rid}" for rid in samples]
        lines += ["", "NOTHING WAS WRITTEN. To run exactly this:",
                   "  " + next_command(payload)]
    else:
        lines += [
            "",
            f"refreshed  {payload.get('refreshed', 0)}",
            f"unchanged  {payload.get('unchanged', 0)}",
            f"gone       {payload.get('gone', 0)}   "
            "(conversation no longer resolves by id — D319's hard case)",
            f"errors     {payload.get('errors', 0)}",
        ]
    return "\n".join(lines)


def next_command(payload: dict) -> str:
    """The exact command that applies what a dry run just showed."""
    target = payload.get("target") or {}
    return (
        f"fm messages refresh-inline-images --env <env> "
        f"--space {target.get('space_id')} --apply"
    )


# ===========================================================================
# CLI
# ===========================================================================
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
    parser.add_argument(
        "--connection",
        default=None,
        help="the OUTLOOK connection id to dedupe (required for `dedupe`)",
    )
    parser.add_argument(
        "--space",
        default=None,
        help="space UUID, never a name (required for `refresh-inline-images`)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="`refresh-inline-images` only: cap how many candidate messages a "
        "REAL run re-syncs in one call",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=None,
        metavar="REMOTE_ID",
        help="`dedupe` only: a duplicate group's remote_id (as printed by the "
        "dry run), repeatable. REQUIRED under --apply — no whole-connection "
        "apply (owner rule, 2026-09-09). Ignored on a dry run",
    )
    parser.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="`dedupe` only: REQUIRED under --apply — one sentence saying why "
        "the named group(s) are the same duplicate cluster. Becomes the audit "
        "row's explanation",
    )
    parser.add_argument(
        "--plan-token",
        default=None,
        metavar="TOKEN",
        help="`dedupe` only: REQUIRED under --apply — the `plan token` the dry "
        "run printed for the SAME --group keys. The server recomputes it from "
        "live rows and refuses (409) if a sync tick has changed which row would "
        "be kept. Ignored on a dry run",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write (retire duplicate rows / re-sync candidates); "
        "without it this is a dry run",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


_HELP = "group-mailbox messages: duplicate cleanup + the inline-image backfill (admin)"

_DESCRIPTION = """Two admin surfaces over the group-mailbox `message` table:

  ev messages dedupe-scan --env ae                                      read-only, every connection
  ev messages dedupe --env ae --connection <id>                         dry run for one connection
  ev messages dedupe --env ae --connection <id> \\
      --group <remote_id> [--group <remote_id> ...] \\
      --reason "<why these are the same conversation>" \\
      --plan-token <token from the dry run> --apply                     merge exactly those groups

  ev messages refresh-inline-images --env ae --space <uuid>            dry run
  ev messages refresh-inline-images --env ae --space <uuid> --apply    the real run

dedupe / dedupe-scan — find and collapse the duplicate `message` rows the
un-locked mailbox sync produced (69 groups / 136 orphan rows in a real prod space,
measured 2026-09-07). Keep rule: per (connection, remote conversation) the
row with the newest last_synced_at survives (ties break on the lowest id) —
the same rule the sync itself now uses to pick which row to update, so the
two cannot disagree. Losers are SOFT-deleted (deleted_at + deleted_by).
Clearing those two columns restores them. Proposal/comment/status-point rows
that named a retired row are re-pointed at the survivor; audit and metering
rows are deliberately left alone. Region-local data: use --env ae for a real
AE-region space, not --host evane.

⚠️ NO WHOLE-CONNECTION APPLY (owner rule, 2026-09-09 — services/dedupe_guard.py).
--apply merges ONLY the groups named by --group (repeatable, the remote_id the
dry run printed), and needs --reason: one sentence saying why the named
group(s) are the same duplicate cluster. `--apply` with no --group, an empty
list, or a wildcard is refused 422. The dry run still describes every group in
the connection regardless — only the WRITE is scoped.

refresh-inline-images — re-embed inline images (company logo, social icons)
in already-synced messages whose body_html still carries a dangling `cid:`,
by re-syncing each one by its OWN remote_id (never Graph's /conversations
LIST — see D319), through the same idempotent upsert path a normal sync uses.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "messages",
        help=_HELP,
        description=_DESCRIPTION,
        epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def _run_dedupe_scan(args) -> int:
    ctx = context.resolve(args, need_token=True)
    payload = api.admin_get(ctx.target, SCAN_ENDPOINT, token=ctx.token)
    if not isinstance(payload, dict):
        raise MessagesError(f"{SCAN_ENDPOINT} returned a non-object response: {payload!r}")
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render_scan(payload))
    return 0


def _run_dedupe(args) -> int:
    connection = (getattr(args, "connection", None) or "").strip()
    if not connection:
        raise MessagesError(
            "`fm messages dedupe` needs --connection <id>. There is deliberately no "
            "all-connections apply: run `fm messages dedupe-scan` first and act on "
            "one measured blast radius at a time."
        )
    # for_write=True EVEN FOR THE DRY RUN. `context.resolve(for_write=True)` is
    # what refuses to inherit the target from `fm use`, and a command that can
    # write must never pick its environment from a remembered preference — not
    # even on the run that happens not to write. One flag decides whether this
    # is production.
    ctx = context.resolve(args, for_write=True, need_token=True)
    body: dict = {"connection_id": connection, "apply": bool(args.apply)}
    if args.apply:
        # Forwarded as-given, including None/empty — the server is the ONE place
        # that refuses (422) a missing/empty/wildcard `groups` or a blank
        # `reason` (services/dedupe_guard.py, owner rule 2026-09-09). Duplicating
        # that check here would be the exact drift the project rules warns against.
        body["groups"] = getattr(args, "group", None)
        body["reason"] = getattr(args, "reason", None)
        # Forwarded as-given for the same reason as the two above: the server
        # recomputes the token from live rows and is the ONE place that refuses
        # (409 `plan-stale`). A client-side "did you pass one?" check here would
        # be a second copy of a rule the server already owns — and the client
        # cannot compute the token anyway, which is the whole point of it.
        body["plan_token"] = getattr(args, "plan_token", None)
    payload = api.admin_post(ctx.target, DEDUPE_ENDPOINT, body, token=ctx.token)
    if not isinstance(payload, dict):
        raise MessagesError(f"{DEDUPE_ENDPOINT} returned a non-object response: {payload!r}")
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render_dedupe(payload))
    return 0


def _run_refresh_inline_images(args) -> int:
    # for_write on BOTH paths, same rule as `fm i18n-backfill` / `fm messages
    # dedupe`: a dry run of a command whose whole subject is a prod write
    # should resolve its environment the same way --apply will.
    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)
    space = require_space_id(str(ctx.space))
    path = REFRESH_INLINE_IMAGES_ENDPOINT.format(space=space)

    body: dict = {"dry_run": not bool(args.apply)}
    if getattr(args, "limit", None) is not None:
        body["limit"] = args.limit

    payload = api.admin_post(ctx.target, path, body, token=ctx.token)
    if not isinstance(payload, dict):
        raise MessagesError(
            f"{REFRESH_INLINE_IMAGES_ENDPOINT} returned a non-object response: {payload!r}"
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(render_refresh_inline_images(payload))
    return 0


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    if subject == "dedupe-scan":
        return _run_dedupe_scan(args)
    if subject == "dedupe":
        return _run_dedupe(args)
    return _run_refresh_inline_images(args)
