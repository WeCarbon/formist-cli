"""``fm files``: the file-row operations surface (``merge``, ``reevaluate``, ``download``).

Three different jobs on the same noun, kept as real argparse
subcommands rather than one flat parser, because ``merge`` and
``reevaluate`` disagree on what ``--apply`` means: ``reevaluate
--apply`` is a flag, ``merge --apply`` takes the ``report_token`` a
prior dry run printed. One parser cannot carry both spellings of one
option name.

--- fm files merge ------------------------------------------------------

Merges two duplicate File rows that share one remote id. The decision
of which row survives, and what it carries, lives on the server; this
module is a client and holds no copy of it, since two independent
answers to "which of these rows survives" is precisely the kind of
disagreement that leaves a half-merged row behind.

This talks to ``/projects/{space}/admin/...`` rather than the
admin-only helpers, because files are region-local: the admin helpers
refuse a non-Global prod target before a socket opens, which is
correct for the platform-admin tier, but would refuse the only region
that actually holds the data. ``fm participants dedupe`` and
``fm airtable`` make the same call for the same reason.

The safety choreography:

* a dry run always runs, including under ``--apply``: it is printed
  first, and what is submitted is the ``report_token`` of the report
  just printed.
* ``--apply`` takes that token, not a bare flag, since the pair can
  change between the dry run and the apply (the connector refreshes
  every 30 minutes); a stale token gets a 409 and nothing is written.
* ``--apply`` never inherits an environment from ``fm use``.
* one pair per call, and ``--reason`` is required, enforced
  server-side too: there is deliberately no ``--all`` and no batch
  file of pairs, since deduplication is a manual, judgement-based
  decision, and naming two ids is the judgement the rule asks for.
* the dry run describes: both rows' identity columns side by side with
  conflicts marked, every reference that would move, which row's
  content the merged row would carry, and which blob keys are shared
  (protected) versus exclusive (destroyed with the dropped row).
  Nothing in it is a command to run on its own.

--- fm files reevaluate -------------------------------------------------

Re-derives whether a File row's stored skip verdict still matches the
current processing criteria (the size cap, the supported-format list,
and so on), through the routes the product already exposes:

* no ``--file``: ``POST /projects/{id}/files/reevaluate`` (a
  project-wide sweep, admin-only, bounded per call, resumable via
  ``--after-id``)
* ``--file <id>``: ``POST /projects/{id}/files/{file_id}/reevaluate``
  (one row, any writer)

Dry run by default on both forms: the sweep route already defaults to
a dry run server-side, but the single-file route does not, so this
client always sends its own ``dry_run`` rather than trusting whichever
default the server happens to pick. ``--apply`` flips it.

``only_skipped`` / ``--limit`` / ``--after-id`` are sweep-only; the
single-file body rejects unknown fields, so they are never sent when
``--file`` is given (sending them would 422 instead of being silently
ignored).

--- fm files download ---------------------------------------------------

A pure read: every file under ``--folder``, recursively, downloaded
into ``--out`` with its subfolder structure preserved.

This does not use the server's own ``folder=`` filter for that,
because that filter matches only a file's immediate parent folder; a
file one level deeper is invisible to it. So this command instead
pages through the space's whole (unfiltered) file listing and filters
``File.path`` client-side for everything under ``--folder``, the only
way to get "under a folder, including its subfolders" out of a
listing whose server-side filter deliberately does not cascade.

Each match is downloaded through the same per-file download route the
product's own file preview uses, one request per file; a
connector-backed row without a durable local copy is fetched from the
source on demand by that same route, so this command never talks to a
connector directly.

Every download is checked against the size the listing already
reported for that row. A size mismatch is a per-file failure: the
file is still written to disk, since the bytes already arrived, but
it is named in the failure list rather than counted as a quiet
success.

One file failing does not stop the rest, the same reasoning
`fm upload DIR` takes. Exit code is non-zero iff at least one file
failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE, add_admin_flag, require_object
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

#: The subcommands ``fm files`` dispatches to.
SUBJECTS = ("merge", "reevaluate", "download")


class FilesError(Exception):
    """A refusal about the file surface, written for an operator."""


_DESCRIPTION = """Re-decide the skip verdict for this project's file rows under
today's processing criteria: the mechanism that notices a raised size cap or a
newly-supported format actually reached already-synced rows, which nothing
else does.

  fm files reevaluate --space <id>                    project-wide sweep (dry run)
  fm files reevaluate --space <id> --apply             sweep, for real
  fm files reevaluate --space <id> --file <file-id>    one row (dry run)
  fm files reevaluate --space <id> --file <file-id> --apply

DRY RUN by default on both forms. --apply is what writes.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    from formist_cli.cli import _add_env, _add_space

    parser = subparsers.add_parser(
        "files",
        help="merge two duplicate File rows, re-apply today's processing criteria, or download a folder",
        description=(
            "fm files merge       merge two duplicate File rows in a space (WRITE)\n"
            "fm files reevaluate  re-apply today's file-processing criteria (WRITE)\n"
            "fm files download    download every file under a folder, recursively (READ)\n\n"
            "merge/reevaluate are DRY RUN by default; download is a plain read with no "
            "--apply of its own. Run `fm files <subcommand> --help` for the full "
            "contract of each; they are different jobs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subjects = parser.add_subparsers(dest="subject", metavar="{merge,reevaluate,download}")
    subjects.required = True

    merge = subjects.add_parser(
        "merge",
        help="merge two duplicate File rows in a space",
        description=(
            "Merges the File row named by --drop into the one named by --keep: "
            "every live reference (task links, the search index row, chip lists, "
            "status points, chat attachments, non-terminal proposals) moves onto "
            "--keep, --keep fills its empty columns from --drop, and --drop is "
            "hard-deleted, not trashed. Audit history is never rewritten; "
            "--drop's columns survive in the delete audit row's before-snapshot.\n\n"
            "DRY RUN BY DEFAULT. --apply takes the report_token this run printed; "
            "if either row has changed since (the connector refreshes every 30 "
            "minutes), the server answers 409 and writes nothing.\n\n"
            "ONE PAIR PER CALL, and --reason is REQUIRED under --apply: one "
            "sentence saying why these two rows are the same file. There is no "
            "'merge them all' shape, since deduplication is a manual, "
            "judgement-based decision, and a request without a reason is "
            "refused 422 by the server.\n\n"
            "BLOB SAFETY. A markdown blob the two rows SHARE is removed from the "
            "dropped row before it is destroyed, so the hard-delete cannot take the "
            "survivor's copy with it. A blob that is exclusively the dropped row's "
            "own, including an UPLOAD's stored bytes, IS deleted immediately, "
            "along with the row; that is no longer a separate, later decision."
        ),
        epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(merge)
    _add_space(merge)
    merge.add_argument(
        "--keep",
        required=True,
        metavar="FILE_ID",
        help="the File row that SURVIVES. Read the dry run before choosing: the "
        "survivor keeps its own markdown surfaces unless it has none at all",
    )
    merge.add_argument(
        "--drop",
        required=True,
        metavar="FILE_ID",
        help="the File row merged into --keep and then HARD-deleted, not "
        "trashed. Its references move first; its columns survive only in the "
        "delete audit row's snapshot",
    )
    merge.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="REQUIRED under --apply: one sentence saying why these two rows are the "
        "same file. Becomes the audit row's explanation",
    )
    merge.add_argument(
        "--apply",
        metavar="REPORT_TOKEN",
        default=None,
        help="merge, if both rows still match the report_token this run printed",
    )
    merge.add_argument("--json", action="store_true", help="machine-readable output")
    add_admin_flag(merge)

    reevaluate = subjects.add_parser(
        "reevaluate",
        help="re-apply today's file-processing criteria to synced rows (WRITE)",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(reevaluate, for_write=True)
    _add_space(reevaluate)
    reevaluate.add_argument("--file", default=None, help="reevaluate ONE file id instead of the whole project")
    reevaluate.add_argument("--apply", action="store_true", help="write (default: dry run, counts only)")
    reevaluate.add_argument(
        "--only-skipped", dest="only_skipped", action="store_true", default=True,
        help="sweep only: rows that currently carry a skip reason (default)",
    )
    reevaluate.add_argument(
        "--include-processed", dest="only_skipped", action="store_false",
        help="sweep only: also examine rows with NO skip reason (after TIGHTENING a criterion)",
    )
    reevaluate.add_argument(
        "--no-rederive", dest="no_rederive", action="store_true",
        help="mark rows for the next sync instead of re-deriving them now",
    )
    reevaluate.add_argument("--limit", type=int, default=None, help="sweep only: cap this call's batch size")
    reevaluate.add_argument("--after-id", dest="after_id", default=None, help="sweep only: resume after this row id")
    reevaluate.add_argument("--json", action="store_true", help="raw JSON instead of a report")

    download = subjects.add_parser(
        "download",
        help="download every file under a folder, recursively (READ)",
        description=(
            "Lists the space's files under --folder, RECURSIVELY, and downloads "
            "each one into --out, preserving the subfolder structure it found "
            "them in.\n\n"
            "The server's own folder= filter (GET /projects/{id}/files) is "
            "EXACT-MATCH ONLY: it deliberately does not cascade into "
            "subfolders, so this command pages through the space's WHOLE file "
            "listing instead and filters by path client-side. See the module "
            "docstring.\n\n"
            "Each file's downloaded size is checked against what the listing "
            "reported; a mismatch is a per-file FAILURE, not a silently short "
            "file. One file failing does not stop the rest; the exit code is "
            "non-zero iff at least one failed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(download)
    _add_space(download)
    download.add_argument(
        "--folder", required=True, metavar="PATH",
        help="absolute folder path, e.g. /Photos/Day 1 (or / for the whole space)",  # system-folder-ok: an example in --help text, not a path the code uses
    )
    download.add_argument(
        "--out", required=True, metavar="DIR", help="local directory to download into",
    )
    download.add_argument("--json", action="store_true", help="machine-readable summary")
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def cmd_files(args) -> int:
    """Dispatch ``fm files <subject>``. Nothing is shared between the branches
    except this line; see the module docstring for why they are separate parsers."""
    subject = getattr(args, "subject", None)
    if subject == "reevaluate":
        return _run_reevaluate(args)
    if subject == "download":
        return _run_download(args)
    return _run_merge(args)


FILE_MERGE_PATH = "/projects/{space}/admin/files/merge"


def _report(body: object) -> dict:
    """The merge report from a response body, or a refusal naming what came back."""
    envelope = require_object(body, what="the file merge endpoint", error=FilesError)
    if "report_token" not in envelope:
        raise FilesError(
            "the file merge endpoint returned an object with no `report_token` key: "
            f"{str(envelope)[:200]}"
        )
    return envelope


def fetch_report(target, space: str, keep: str, drop: str, *, token: str | None, get=None) -> dict:
    """The dry run. Writes nothing."""
    from urllib.parse import urlencode

    from formist_cli import api

    get = get or api.get
    path = f"{FILE_MERGE_PATH.format(space=space)}?{urlencode({'keep': keep, 'drop': drop})}"
    return _report(get(target, path, token=token))


def submit(
    target,
    space: str,
    keep: str,
    drop: str,
    report_token: str,
    *,
    token: str | None,
    reason: str | None,
    post=None,
) -> dict:
    """Merge this pair, if it still matches ``report_token``.

    Sends ``reason`` as-is and lets the server refuse (422) a blank
    one, rather than duplicating that validation here.
    """
    from urllib.parse import urlencode

    from formist_cli import api

    post = post or api.post
    path = f"{FILE_MERGE_PATH.format(space=space)}?{urlencode({'keep': keep, 'drop': drop})}"
    body: dict[str, Any] = {"report_token": report_token, "reason": reason}
    return _report(post(target, path, body, token=token))


def render(report: dict, *, out=None) -> None:
    """DESCRIBE only. Nothing printed here is a decision the tool has taken."""
    out = out or sys.stdout
    refusals = report.get("refusals") or []
    if refusals:
        print("REFUSED: nothing can be merged:", file=out)
        for refusal in refusals:
            print(f"  - {refusal}", file=out)
        return

    if report.get("idempotent"):
        print(f"ALREADY MERGED: {report.get('idempotent_reason')}", file=out)
        return

    print(
        f"space {report.get('project_id')} | keep {report.get('keep_id')} | "
        f"drop {report.get('drop_id')}",
        file=out,
    )
    print("\n## both rows, side by side (x marks a conflict)\n", file=out)
    print("| field | keep | drop | |", file=out)
    print("|---|---|---|---|", file=out)
    for row in report.get("fields") or []:
        mark = "x" if row.get("conflict") else ("+" if row.get("fills_a_gap") else "")
        print(
            f"| {row.get('field')} | {row.get('keep')!r} | {row.get('drop')!r} | {mark} |",
            file=out,
        )

    updates = report.get("updates") or {}
    if updates:
        print(
            f"\nwould write onto the survivor: {', '.join(sorted(updates))}",
            file=out,
        )
    for conflict in report.get("conflicts") or []:
        # NEVER folded into a count. A conflict is the reason a human is reading
        # this, and a report that said "2 conflicts" while silently picking a side
        # would be a check that cannot fail.
        print(
            f"  CONFLICT {conflict.get('field')}: keeping {conflict.get('keeper')!r}, "
            f"discarding {conflict.get('loser')!r}",
            file=out,
        )

    group = report.get("content_group") or {}
    if group:
        print(
            f"\n## markdown surfaces: taken from the {group.get('winner')} row\n"
            f"{group.get('reason')}",
            file=out,
        )
        for name, value in sorted((group.get("discarded") or {}).items()):
            print(f"  discarding {name} = {value!r} (from {group.get('discarded_from')})", file=out)

    refs = report.get("references") or {}
    print("\n## references\n", file=out)
    if refs:
        for key, count in sorted(refs.items()):
            print(f"  {key}: {count}", file=out)
    else:
        print("  no live references on the dropped row", file=out)

    print(
        "\n## blob artifacts (a SHARED one is protected; an EXCLUSIVE one is "
        "destroyed with the row)\n",
        file=out,
    )
    for art in report.get("artifacts") or []:
        shared = "SHARED with the survivor" if art.get("shared_key") else "not shared"
        print(
            f"  {art.get('surface')}: {shared}"
            + (f", releasable ({art.get('bytes') or '?'} bytes)" if art.get("releasable") else ""),
            file=out,
        )
        print(f"      {art.get('action')}", file=out)


def _run_merge(args) -> int:
    from formist_cli import privileges
    from formist_cli.cli import _effective, _token_for

    # No member-level equivalent: hard-deleting the dropped row is
    # irreversible, which is also why this is the one subject of `fm files`
    # that needs --admin at all (`reevaluate`/`download` do not).
    privileges.require_admin_flag("fm files merge", bool(getattr(args, "admin", False)))

    eff = _effective(args, for_write=bool(args.apply))
    target, space = eff.target, eff.space
    token = _token_for(eff)

    report = fetch_report(target, space, args.keep, args.drop, token=token)
    if not args.json:
        render(report)
    if not args.apply:
        if args.json:
            print(json.dumps(report, indent=2))
        elif not report.get("refusals") and not report.get("idempotent"):
            print(
                f"\nRe-run with --reason \"<why these are one file>\" --apply "
                f"{report.get('report_token')} --host <host> to merge this pair."
            )
        return 0

    if report.get("refusals"):
        raise FilesError(
            "REFUSING TO APPLY: the dry run above says this pair cannot be merged. "
            "Nothing was sent."
        )
    if report.get("idempotent"):
        print("\nalready merged, no request was sent.")
        return 0
    if args.apply != report.get("report_token"):
        raise FilesError(
            "REFUSING TO APPLY: this is not the report you were shown.\n"
            f"  you passed : {args.apply}\n"
            f"  report now : {report.get('report_token')}\n"
            "One of the two rows changed between your dry run and this call (the "
            "connector refreshes every 30 minutes). Read the report above and apply "
            "that token instead."
        )

    applied = submit(
        target, space, args.keep, args.drop, args.apply, token=token, reason=args.reason
    )
    if args.json:
        print(json.dumps(applied, indent=2))
        return 0
    render(applied)
    if applied.get("applied"):
        print(
            f"\nmerged. {applied.get('drop_id')} was permanently deleted, "
            f"every reference had already moved onto {applied.get('keep_id')}. It is "
            "NOT recoverable; its columns survive only in the delete audit row's "
            "before-snapshot."
        )
        return 0
    print("\nthe server did not report the merge as applied; see above.", file=sys.stderr)
    return 1


def _render(payload: dict, *, dry_run: bool) -> str:
    lines = [f"mode  {'DRY RUN (writes nothing)' if dry_run else 'APPLIED'}"]
    for key in ("to_fill", "applied", "examined"):
        if key in payload:
            lines.append(f"{key:<10} {payload[key]}")
    counts = payload.get("counts")
    if isinstance(counts, dict) and counts:
        lines.append("")
        for action, n in sorted(counts.items()):
            lines.append(f"  {action:<20} {n}")
    if "item" in payload:
        item = payload.get("item") or {}
        lines.append(f"file      {item.get('id')}  {item.get('name')}")
    if "reevaluation" in payload:
        lines.append(f"outcome   {payload.get('reevaluation')}")
    if payload.get("next_cursor"):
        lines.append(f"\nmore rows exist, resume with --after-id {payload['next_cursor']}")
    return "\n".join(lines)


def _run_reevaluate(args) -> int:
    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)
    dry_run = not bool(args.apply)

    if args.file:
        body: dict = {"dry_run": dry_run, "rederive": not bool(args.no_rederive)}
        path = f"/projects/{ctx.space}/files/{args.file}/reevaluate"
    else:
        body = {
            "dry_run": dry_run,
            "only_skipped": bool(args.only_skipped),
            "rederive": not bool(args.no_rederive),
        }
        if args.limit is not None:
            body["limit"] = args.limit
        if args.after_id is not None:
            body["after_id"] = args.after_id
        path = f"/projects/{ctx.space}/files/reevaluate"

    payload = api.post(ctx.target, path, body, token=ctx.token)
    if not isinstance(payload, dict):
        raise FilesError(
            f"expected a JSON object from {path}, got {type(payload).__name__}. "
            "That is a server contract change, not an empty result."
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_render(payload, dry_run=dry_run))
    return 0


# ===========================================================================
# fm files download: every file under a folder, recursively (READ)
# ===========================================================================

#: The server's own list-envelope page size cap. Paging at this size
#: minimises round trips without depending on the server ever raising
#: its own cap. Shared with the one other module that pages the same
#: listing, so the two cannot drift apart silently.
from formist_cli.meetings import _LIST_PAGE_SIZE  # noqa: E402


def _normalize_folder(folder: str) -> str:
    """``folder`` as an absolute path with no trailing slash (``/`` for
    the space root). Matches the server's own normalisation, even
    though this module does not use the server's folder filter (see
    the module docstring): a file's own ``path`` column is stored the
    same way, so the comparison has to start from the same shape.
    """
    stripped = folder.strip("/")
    return f"/{stripped}" if stripped else "/"


def is_under_folder(folder: str, file_path: str) -> bool:
    """True iff ``file_path`` sits at or below ``folder``, including
    every subfolder, which is the one thing the server's own
    ``folder=`` filter refuses to do (see the module docstring).
    ``folder="/"`` matches every path in the space."""
    norm = _normalize_folder(folder)
    if norm == "/":
        return True
    path = file_path or ""
    return path == norm or path.startswith(f"{norm}/")


def relative_download_path(folder: str, file_path: str) -> str:
    """``file_path``'s path relative to ``folder``, safe to join onto ``--out``.

    Refuses (rather than silently sanitising) anything that would
    resolve outside ``--out``, a ``..`` segment or an empty result,
    since a path taken from a server response and joined onto a local
    directory without that check can write somewhere else entirely.
    """
    norm = _normalize_folder(folder)
    path = (file_path or "").replace("\\", "/")
    if norm != "/" and (path == norm or path.startswith(f"{norm}/")):
        rel = path[len(norm):].lstrip("/")
    else:
        rel = path.lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise FilesError(
            f"refusing to download {file_path!r}; its path does not resolve to "
            f"a location under --out ({rel!r})"
        )
    return "/".join(parts)


def list_files_under(target, space: str, folder: str, *, token: str | None, get=None) -> list[dict]:
    """Every file in ``space`` whose path is under ``folder``, recursively:
    paged through the space's whole (unfiltered) file listing
    (``GET /projects/{id}/files``) and filtered client-side by path. See
    the module docstring for why the server's own ``folder=`` param
    cannot be used for this (it is exact-match only).
    """
    get = get or api.get
    out: list[dict] = []
    offset = 0
    while True:
        payload = get(
            target, f"/projects/{space}/files", token=token,
            params={"limit": str(_LIST_PAGE_SIZE), "offset": str(offset)},
        )
        envelope = require_object(payload, what="the files listing", error=FilesError)
        items = [i for i in (envelope.get("items") or []) if isinstance(i, dict)]
        out.extend(items)
        total = envelope.get("total")
        offset += len(items)
        if not items or total is None or offset >= int(total):
            break
    return [f for f in out if is_under_folder(folder, f.get("path") or "")]


def _run_download(args) -> int:
    from pathlib import Path

    ctx = context.resolve(args, need_space=True, need_token=True)
    target, space = ctx.target, ctx.space
    out_dir = Path(args.out)

    items = list_files_under(target, space, args.folder, token=ctx.token)
    out_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    failed: list[tuple[str, str]] = []
    for item in items:
        file_id = item.get("id")
        path = item.get("path") or ""
        try:
            rel = relative_download_path(args.folder, path)
        except FilesError as exc:
            print(f"FAILED  {path}  {exc}", file=sys.stderr)
            failed.append((path, str(exc)))
            continue

        dest = out_dir / rel
        try:
            data, _disposition = api.download(
                target, f"/projects/{space}/files/{file_id}/download", token=ctx.token
            )
            expected_size = item.get("size")
            if isinstance(expected_size, int) and len(data) != expected_size:
                raise FilesError(
                    f"downloaded {len(data)} bytes, expected {expected_size}; "
                    "do not trust this file"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except Exception as exc:  # noqa: BLE001 - one file must not end the run
            print(f"FAILED  {rel}  {exc}", file=sys.stderr)
            failed.append((rel, str(exc)))
            continue
        print(f"downloaded  {rel}  ({len(data)} bytes)")
        downloaded.append(rel)

    if args.json:
        print(json.dumps(
            {
                "folder": args.folder,
                "out": str(out_dir),
                "downloaded": len(downloaded),
                "failed": [{"path": rel, "reason": reason} for rel, reason in failed],
                "total": len(items),
            },
            indent=2, ensure_ascii=False,
        ))
    print(
        f"\n{len(downloaded)} downloaded, {len(failed)} failed, {len(items)} total "
        f"under {args.folder!r}",
        file=sys.stderr,
    )
    if failed:
        print("failed:", file=sys.stderr)
        for rel, reason in failed:
            print(f"  {rel}: {reason}", file=sys.stderr)
        return 1
    return 0
