"""``fm participants dedupe`` - merge duplicate people in a space.

The decision lives on the server; this module is a client and holds no
copy of it. That split is not stylistic: two answers to "are these the
same person" is precisely how the duplicates this repairs were created,
one row at a time.

This talks to ``/projects/{space}/admin/...`` and not the plain admin
helpers, because those refuse a non-Global prod target before a socket
opens, which is correct for the literal ``/admin`` tier (that tier is
Global-only). Participants are region-local, so this endpoint is
project-scoped and must reach ``ae``; sending it through the admin
helpers would refuse the only target that has the data.

The safety choreography:

A dry run always runs, including under ``--apply``. It is printed first,
and what is submitted is the ``plan_token`` of the plan that was printed.

``--apply`` takes the token, not a bare flag. The connector that produced
these duplicates runs every 30 minutes, so "merge what is duplicated now"
is a different set from "merge what you just showed me". A stale token is
a 409 and nothing is written.

``--apply`` never inherits an environment from ``fm use``. A remembered
environment is one somebody forgets they set.

No whole-plan apply, enforced server-side and refused with 422 if this CLI
is bypassed: ``--apply`` merges only the groups named by ``--group``
(repeatable), and needs ``--reason``, one sentence saying why the named
group(s) are one person. There is no "merge everything" shape. Naming a
group is what makes it mergeable, record-id or same-name alike; a shared
name is still never auto-merged, a human still has to name it.

``--review`` prints the describe data, not a decision: every row's id,
created_at and provenance, every identity field side by side across the
group's rows with conflicts marked, the tool's keeper suggestion and why,
and the evidence (session participations, audit history) a human weighs
before naming a group. Nothing in it is a command to run.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE, require_object

#: Deliberately not named `ENDPOINT`: other modules each own a constant by
#: that name with nothing comparing them, and a third copy of that name
#: would make the collision worse. Same decision `skill_library.py` took
#: (`SKILL_LIBRARY_SYNC_ENDPOINT`).
DEDUPE_PATH = "/projects/{space}/admin/participants/dedupe"

SUBJECTS = ("dedupe",)


class ParticipantsError(Exception):
    """A refusal or a malformed answer, phrased for whoever has to fix it."""


def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "participants",
        help="find and merge duplicate participants in a space",
        description=(
            "Groups a space's participants by the SOURCE record id a connector "
            "stamped on them (data.<connector>.record_id) and, advisory only, by "
            "normalised name. DESCRIBES ONLY: nothing here is a decision.\n\n"
            "DRY RUN BY DEFAULT. --apply takes the plan_token this run printed; "
            "if the duplicates have changed since (the sync runs every 30 "
            "minutes), the server answers 409 and writes nothing.\n\n"
            "NO WHOLE-PLAN APPLY. --apply merges ONLY "
            "the groups named by --group (repeatable), record-id or same-name "
            "alike, and needs --reason: one sentence saying why the named "
            "group(s) are one person. There is no 'merge everything' shape, a "
            "request naming no group, an empty list, or a wildcard is refused "
            "422 by the server.\n\n"
            "Merging keeps the OLDEST row, unions every non-empty value onto it "
            "(nothing is lost), re-points every live reference, and SOFT-deletes "
            "the rest. Audit history is never rewritten. --review prints the full "
            "describe data (per-row provenance, side-by-side identity fields with "
            "conflicts marked, the keeper suggestion) as markdown."
        ),
        epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument("subject", choices=SUBJECTS, help="what to operate on (today: dedupe)")
    p.add_argument(
        "--group",
        action="append",
        default=None,
        metavar="KEY",
        help=(
            "a group key to include ('airtable:recXXXX' or 'name:<normalised "
            "name>'), repeatable. On a dry run: scopes what is described (and "
            "the plan_token, to exactly these groups). Under --apply: REQUIRED, "
            "the exact groups to merge, and nothing else"
        ),
    )
    p.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help=(
            "REQUIRED under --apply: one sentence saying why the named group(s) "
            "are the same person. Becomes the audit row's explanation"
        ),
    )
    p.add_argument(
        "--apply",
        metavar="PLAN_TOKEN",
        default=None,
        help="merge, if the duplicates still match the plan_token this run printed",
    )
    p.add_argument(
        "--drop",
        action="append",
        default=None,
        metavar="GROUP=data.path",
        help=(
            "do NOT carry one merged-in value onto the survivor, e.g. "
            "--drop 'airtable:recSyn001Abc002Xy=data.airtable.contact_email'. "
            "For a value you have read and judged wrong. The losing row keeps it "
            "(soft delete + audit snapshot), so nothing is destroyed. Repeatable"
        ),
    )
    p.add_argument(
        "--review",
        action="store_true",
        help="markdown describe output: per-row provenance, side-by-side "
        "identity fields with conflicts marked, the keeper suggestion, and the "
        "evidence a human weighs. Nothing in it is a command to run",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


def parse_drops(values: list[str] | None) -> dict[str, list[str]]:
    """``["group=data.a.b", ...]`` -> ``{"group": ["data.a.b"]}``.

    Split on the FIRST ``=`` only: a group key is ``airtable:recXXXX`` or
    ``name:<normalised name>`` and a normalised name can contain almost anything,
    so splitting on every ``=`` would silently truncate a key rather than fail.
    """
    drops: dict[str, list[str]] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ParticipantsError(
                f"--drop needs GROUP=data.path, got {raw!r}. The group key is the "
                "one printed by the dry run."
            )
        group, path = raw.split("=", 1)
        if not group or not path:
            raise ParticipantsError(f"--drop needs a group AND a path, got {raw!r}")
        drops.setdefault(group, []).append(path)
    return drops


def _plan(body: object) -> dict:
    """The dedupe plan from a response body, or a refusal naming what came back."""
    envelope = require_object(body, what="the participants dedupe endpoint", error=ParticipantsError)
    if "groups" not in envelope:
        raise ParticipantsError(
            "the participants dedupe endpoint returned an object with no `groups` "
            f"key: {str(envelope)[:200]}"
        )
    return envelope


def fetch_plan(target, space: str, *, token: str | None, groups: list[str] | None = None, get=None) -> dict:
    """The dry run. Writes nothing.

    ``groups`` is repeated as ``?group=`` (a plain dict can only carry one
    value per key, which is why this builds the query string itself rather
    than passing ``params=``): the server treats it as a pure display
    filter and scopes the returned ``plan_token`` to exactly what came back.
    """
    from urllib.parse import urlencode

    from formist_cli import api

    get = get or api.get
    path = DEDUPE_PATH.format(space=space)
    if groups:
        path = f"{path}?{urlencode([('group', g) for g in groups])}"
    return _plan(get(target, path, token=token))


def submit(
    target,
    space: str,
    plan_token: str,
    *,
    token: str | None,
    groups: list[str],
    reason: str,
    drop_data_keys: dict[str, list[str]] | None = None,
    post=None,
) -> dict:
    """Merge exactly ``groups`` of the plan whose fingerprint is ``plan_token``.

    Sends ``groups``/``reason`` as-is and lets the server refuse (422) an
    empty list, a wildcard, or a blank reason: the server is the one place
    that rule is checked, and duplicating it here would be a second copy
    that can drift from it.
    """
    from formist_cli import api

    post = post or api.post
    body: dict[str, Any] = {"plan_token": plan_token, "groups": groups, "reason": reason}
    if drop_data_keys:
        body["drop_data_keys"] = drop_data_keys
    return _plan(post(target, DEDUPE_PATH.format(space=space), body, token=token))


def render_plan(plan: dict, *, applying: bool = False, out=None) -> None:
    """DESCRIBE only. Neither tier nor ``applying`` decides what merges: naming a
    group with ``--group --reason --apply`` does. See :func:`render_review` for the
    fuller per-row/per-field describe output.
    """
    out = out or sys.stdout
    groups = plan.get("groups") or []
    print(
        f"{plan.get('live_participants')} live participant(s) | "
        f"{plan.get('auto_mergeable', 0)} record-id group(s) | "
        f"{plan.get('advisory', 0)} same-name group(s), advisory",
        file=out,
    )
    for group in groups:
        tier = group.get("tier")
        label_tier = "record-id" if tier == "record_id" else "same-name"
        print(
            f"  [{label_tier}] {group.get('key')}  {group.get('label')!r}: "
            f"suggests keeping {group.get('keep_id')}, dropping "
            f"{len(group.get('drop_ids') or [])}",
            file=out,
        )
        updates = group.get("updates") or {}
        if updates:
            print(f"          would merge in: {', '.join(sorted(updates))}", file=out)
        for conflict in group.get("conflicts") or []:
            # Never folded into the summary count. A conflict is the reason a human
            # is reading this, and a plan that reported "3 groups" while silently
            # picking a side would be a check that cannot fail.
            print(
                f"          CONFLICT {conflict.get('field')}: "
                f"keeping {conflict.get('keeper')!r}, discarding "
                f"{conflict.get('loser')!r} (from {conflict.get('loser_id')})",
                file=out,
            )
    print(
        f"\n{plan.get('rows_to_soft_delete', 0)} row(s) in a record-id group | "
        f"plan_token {plan.get('plan_token')} (scoped to the groups printed above)\n"
        "NOTHING HERE IS APPLIED. Pass --group <key> (repeatable) --reason "
        "\"<why>\" --apply <plan_token> to merge exactly the groups you name.",
        file=out,
    )


def render_review(plan: dict, *, out=None) -> None:
    """The full describe output, as markdown. Nothing here is a command to run:
    it is what a human reads to decide which groups (if any) to name."""
    out = out or sys.stdout
    groups = plan.get("groups") or []
    print(f"# participants dedupe, review ({len(groups)} duplicate group(s))\n", file=out)
    if not groups:
        print("no duplicate groups in this space (or in the --group scope given).", file=out)
        return
    for group in groups:
        print(f"## [{group.get('tier')}] `{group.get('key')}`: {group.get('label')!r}\n", file=out)
        suggestion = group.get("keeper_suggestion") or {}
        print(
            f"keeper SUGGESTION (not a decision): `{suggestion.get('id')}`: "
            f"{suggestion.get('reason')}\n",
            file=out,
        )

        rows = group.get("rows") or []
        row_ids = [r.get("id") for r in rows]
        print("| row id | created_at | created by | sessions | audit rows |", file=out)
        print("|---|---|---|---|---|", file=out)
        for row in rows:
            by = row.get("created_by") or {}
            who = by.get("source", "unknown")
            actor = by.get("actor")
            proposal = by.get("proposal_id")
            if actor:
                who = f"{who} ({actor}" + (f", proposal {proposal}" if proposal else "") + ")"
            print(
                f"| `{row.get('id')}` | {row.get('created_at')} | {who} | "
                f"{row.get('session_participations', 0)} | {row.get('audit_history', 0)} |",
                file=out,
            )
        print("", file=out)

        fields = group.get("fields") or []
        if fields and row_ids:
            print("| field | " + " | ".join(f"`{rid}`" for rid in row_ids) + " | |", file=out)
            print("|" + "---|" * (len(row_ids) + 2), file=out)
            for f in fields:
                values = f.get("values") or {}
                cells = [
                    "-" if values.get(rid) in (None, "", []) else str(values.get(rid))
                    for rid in row_ids
                ]
                mark = "CONFLICT" if f.get("conflict") else "agree"
                print(f"| {f.get('field')} | " + " | ".join(cells) + f" | {mark} |", file=out)
        print("\n", file=out)


def render_outcomes(report: dict, *, out=None) -> None:
    out = out or sys.stdout
    print("\napplied:", file=out)
    for outcome in report.get("outcomes") or []:
        if outcome.get("error"):
            print(f"  ERROR {outcome.get('key')}: {outcome['error']}", file=out)
        elif outcome.get("skipped"):
            print(f"  SKIP {outcome.get('key')}: {outcome['skipped']}", file=out)
        else:
            for gone in outcome.get("dropped_values") or []:
                print(
                    f"      dropped {gone.get('path')} = {gone.get('value')!r} "
                    "(kept on the soft-deleted row and in its audit snapshot)",
                    file=out,
                )
            refs = outcome.get("references_repointed") or {}
            detail = ", ".join(f"{k}={v}" for k, v in sorted(refs.items())) or "no live references"
            print(
                f"  OK {outcome.get('key')}: merged {outcome.get('merged')} into "
                f"{outcome.get('keep_id')} ({detail})",
                file=out,
            )
    print(
        f"\n{report.get('live_participants')} live participant(s) remain | "
        f"{report.get('auto_mergeable', 0)} record-id group(s) still duplicated",
        file=out,
    )


def cmd_participants(args) -> int:
    from formist_cli.cli import _effective, _token_for

    eff = _effective(args, for_write=bool(args.apply))
    target, space = eff.target, eff.space
    token = _token_for(eff)

    plan = fetch_plan(target, space, token=token, groups=args.group)
    if args.review and not args.json:
        render_review(plan)
    elif not args.json:
        render_plan(plan, applying=bool(args.apply))
    if not args.apply:
        if args.json:
            print(json.dumps(plan, indent=2))
        elif plan.get("groups"):
            print(
                f"\n-> re-run with --group <key> (repeat) --reason \"<why>\" "
                f"--apply {plan.get('plan_token')} --host <host> to merge exactly "
                "the groups you name."
            )
        return 0

    if args.apply != plan.get("plan_token"):
        raise ParticipantsError(
            "REFUSING TO APPLY: this is not the plan you were shown.\n"
            f"  you passed : {args.apply}\n"
            f"  plan now   : {plan.get('plan_token')}\n"
            "The duplicates changed between your dry run and this call (the "
            "connector syncs every 30 minutes), or the --group scope differs "
            "between the two calls. Read the plan above and apply that token "
            "instead, with the same --group keys."
        )
    if not plan.get("groups"):
        print("\nnothing to merge, no request was sent.")
        return 0

    report = submit(
        target,
        space,
        args.apply,
        token=token,
        groups=args.group,
        reason=args.reason,
        drop_data_keys=parse_drops(args.drop),
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    render_outcomes(report)
    failed = [o for o in report.get("outcomes") or [] if o.get("error")]
    if failed:
        print(f"\n{len(failed)} group(s) were NOT merged, see above.", file=sys.stderr)
        return 1
    return 0
