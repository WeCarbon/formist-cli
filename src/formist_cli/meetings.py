"""``fm meeting`` — meeting-bot client operations: refresh a transcript, join now.

Absorbs two scripts (EV-ABSORB.md §3.1, both "no new endpoint" — an existing
route already does the work):

* the retired ``ingest_transcript.py`` looped every APPOINTMENT task with a
  ``bot_id`` in one hardcoded project and force-refreshed its transcript from
  Attendee. ``fm meeting transcript-refresh <task-id> --space <id>`` does the
  SAME thing for ONE named task through
  ``GET /projects/{id}/meetings/{task_id}/transcript`` — that route already
  best-effort re-ingests before answering (see its own docstring in
  ``routes/meetings_center.py``), so this command is a bare client, not a
  re-implementation of the ingest logic. ``--all`` restores the script's
  "every task with a recording" sweep by listing meetings first
  (``GET /projects/{id}/meetings``) and refreshing each one whose
  ``has_recording`` is true.

* the retired ``join_meeting.py`` classified a pasted join URL (Teams/Zoom/Meet)
  and immediately dispatched a named Attendee bot into it.
  ``fm meeting join <url> --space <id>`` does the SAME thing through
  ``POST /projects/{id}/meetings/join-now``
  (``services/meeting_join.py::join_meeting_now``). Unlike the script — which
  ran straight through — this is a WRITE that can dispatch a real bot into a
  live meeting, so it follows ``fm``'s write convention: DRY RUN by default,
  ``--apply`` to actually do it.
"""

from __future__ import annotations

import argparse
import json
import sys

from formist_cli import api, context
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

SUBJECTS = ("transcript-refresh", "join")

#: Meetings the server considers "with a recording" carry no `bot_id` in the
#: list payload (routes/meetings_center.py's `_meeting_dict` publishes
#: `has_recording`, not the raw id) — this is the field `--all` filters on.
_LIST_PAGE_SIZE = 200


class MeetingsError(Exception):
    """A refusal about the meeting surface, written for an operator."""


def _subject_error(subject: object) -> MeetingsError:
    return MeetingsError(
        f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
        "  transcript-refresh <task-id>   refresh + print one meeting's transcript\n"
        "  join <url>                     dispatch a bot into a join URL (WRITE)"
    )


_DESCRIPTION = """Meeting-bot client operations.

  fm meeting transcript-refresh <task-id> --space <id>   refresh + print one transcript
  fm meeting transcript-refresh --all --space <id>       refresh every task with a bot
  fm meeting join <url> --space <id> [--apply]           dispatch a bot (WRITE; dry-run by default)
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "meeting",
        help="meeting-bot ops: refresh a transcript, join now (join is a WRITE)",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    from formist_cli.cli import _add_env, _add_space

    _add_env(parser, for_write=True)
    _add_space(parser)
    parser.add_argument("subject", help=f"one of: {', '.join(SUBJECTS)}")
    parser.add_argument(
        "arg", nargs="?", default=None,
        help="transcript-refresh: a task id (UUID). join: the meeting URL.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="transcript-refresh: refresh every meeting with a recording, not just one",
    )
    parser.add_argument("--bot-name", dest="bot_name", default=None, help="join: override the bot's display name")
    parser.add_argument(
        "--summary-skill-id", dest="summary_skill_id", default=None,
        help="join: skill id used to summarize the meeting afterwards",
    )
    parser.add_argument("--apply", action="store_true", help="join: perform the write (default: dry run)")
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


# ===========================================================================
# transcript-refresh
# ===========================================================================
def _render_transcript(payload: dict) -> str:
    lines = [f"file_id  {payload.get('file_id')}", f"path     {payload.get('path')}"]
    text = payload.get("markdown") or ""
    lines.append(f"markdown {len(text)} chars")
    return "\n".join(lines)


def _refresh_one(ctx: context.Context, task_id: str) -> dict:
    payload = api.get(ctx.target, f"/projects/{ctx.space}/meetings/{task_id}/transcript", token=ctx.token)
    if not isinstance(payload, dict):
        raise MeetingsError(
            f"expected a JSON object from /meetings/{{id}}/transcript, got {type(payload).__name__}."
        )
    return payload


def _run_transcript_refresh(ctx: context.Context, args) -> int:
    if args.all:
        payload = api.get(
            ctx.target, f"/projects/{ctx.space}/meetings", token=ctx.token,
            params={"limit": str(_LIST_PAGE_SIZE)},
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        items = [i for i in (items or []) if isinstance(i, dict) and i.get("has_recording")]
        if not items:
            print("0 meeting(s) with a recording — nothing to refresh.")
            return 0
        print(f"refreshing {len(items)} meeting(s) with a recording:")
        for item in items:
            task_id = str(item["id"])
            try:
                refreshed = _refresh_one(ctx, task_id)
            except api.ApiError as exc:
                print(f"- {item.get('subject')} ({task_id}): FAILED — {exc}")
                continue
            print(f"- {item.get('subject')} ({task_id}): file_id={refreshed.get('file_id')}")
        return 0

    if not args.arg:
        raise MeetingsError(
            "`fm meeting transcript-refresh` needs a task id, or --all.\n"
            "→ fm meeting transcript-refresh <task-id> --space <id>"
        )
    payload = _refresh_one(ctx, args.arg)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_render_transcript(payload))
    return 0


# ===========================================================================
# join
# ===========================================================================
def _run_join(ctx: context.Context, args) -> int:
    if not args.arg:
        raise MeetingsError(
            "`fm meeting join` needs a URL.\n→ fm meeting join <url> --space <id> [--apply]"
        )
    url = args.arg
    body: dict = {"url": url}
    if args.bot_name:
        body["bot_name"] = args.bot_name
    if args.summary_skill_id:
        body["summary_skill_id"] = args.summary_skill_id

    if not args.apply:
        bot_name_note = f", bot_name={args.bot_name!r}" if args.bot_name else ""
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would dispatch a bot into {url!r} in space {ctx.space} ({ctx.target.name})"
            f"{bot_name_note}.\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    payload = api.post(ctx.target, f"/projects/{ctx.space}/meetings/join-now", body, token=ctx.token)
    if not isinstance(payload, dict):
        raise MeetingsError(
            f"expected a JSON object from /meetings/join-now, got {type(payload).__name__}."
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        task = payload.get("task") or {}
        print(f"platform={payload.get('platform')}  task_id={task.get('id')}  subject={task.get('subject')}")
        if payload.get("already_joined"):
            print("bot already live for this meeting (no second bot dispatched)")
        else:
            print(f"bot_id={payload.get('bot_id')}")
    if payload.get("dispatch_error"):
        print(f"DISPATCH FAILED (task IS saved — fix and retry): {payload['dispatch_error']}")
        return 2
    return 0


def run(args) -> int:
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in SUBJECTS:
        raise _subject_error(getattr(args, "subject", None))
    ctx = context.resolve(args, for_write=(subject == "join"), need_space=True, need_token=True)
    if subject == "transcript-refresh":
        return _run_transcript_refresh(ctx, args)
    return _run_join(ctx, args)
