"""``fm space-plan`` — read and set a space's USAGE PLAN (``space_plan``).

WHY THIS EXISTS
---------------
On 2026-09-02 a plan row for a production space was attempted through
``az containerapp exec`` with an ad-hoc INSERT, because ``ev`` had no subcommand
for it. The insert failed on ``space_plan_project_id_fkey`` — a foreign key was the
only thing between the operator and a row in a database that does not own the
space. The point of this command is not that one row: it is that a plan change
becomes repeatable, auditable and reviewable instead of a paste into a container.

THE TARGET IS NOT YOURS TO CHOOSE
---------------------------------
``space_plan`` lives in a REGION database, keyed by ``project.id``, and the only
database it may go in is the one that owns that space's ``project`` row. So there
is deliberately **no ``--region`` and no database flag here**: the server resolves
it from the Global space registry and reports what it resolved to, and
``routes/admin.py`` refuses (409 ``space-project-missing``) if that database turns
out not to hold the project. A region an operator types is a region an operator can
get wrong, and getting it wrong writes another database.

``--env`` therefore does NOT name the database. It names which deployment's admin
API to ask — and for the default product, ``/admin`` is served from **Global only** in prod, so
the real choice is `local` or `global` even for an AE space. ``evtool/api.py``
refuses the other case before opening a socket.

THE `formist` ENV IS A DIFFERENT DEPLOYMENT, NOT A THIRD REGION — for a `formist`
space ``--host formist`` is the only correct choice; it has no Global/AE split of
its own, so its one deployment serves ``/admin`` locally (``evtool/targets.py::
REGION_FORMIST``).

DRY RUN FIRST, ALWAYS
---------------------
Same contract as ``fm update``: the request body carries ``apply`` and it defaults
to **false** on the server model, so a body that merely forgets it cannot write.
``fm space-plan set`` runs the dry run, prints exactly what would change, and
prints the ``revision`` of the row it was looking at. ``--apply <revision>``
re-runs the dry run, refuses if the revision has moved, and only then sends
``apply: true`` — carrying the revision, so the SERVER re-checks it too.

⚠️ That double check is deliberate and neither half is dead. The client's copy
gives the operator a readable refusal without a write ever being attempted; the
server's copy is the one that actually holds, because a caller reaching the API
without this CLI gets no protection from a check that lives here. Removing either
leaves a working command, which is why this paragraph exists — see GATE.md on
layered guards.

⚠️ WHAT A PLAN ROW DOES, and why the output says it in capitals: a space with NO
plan row is UNGATED — ``services/usage_gate.py`` allows every model call and settles
nothing. Creating the first row flips that space from fail-open to fail-closed. The
server measures the consequences on the deployment that will enforce them (is the
gate even switched on; is there a catch-all multiplier) and this prints them.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE, as_dict
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.targets import require_space_id

SUBJECTS = ("show", "set")
DEFAULT_SUBJECT = "show"

#: The plan columns this command can set, and the flag each is spelled with.
#: One table, so ``--help``, the request body and the "nothing to set" refusal
#: cannot disagree about what is settable.
SETTABLE = (
    ("name", "--name", str, "plan label (NULL ⇒ the config default, '<product> Max')"),
    ("rolling_window_hours", "--rolling-window-hours", int, "length of the ROLLING window, in hours"),
    (
        "rolling_limit_standard_tokens",
        "--rolling-limit",
        int,
        "standard tokens allowed inside the rolling window",
    ),
    (
        "weekly_limit_standard_tokens",
        "--weekly-limit",
        int,
        "standard tokens allowed inside the ANCHORED weekly window",
    ),
    ("tier", "--tier", str, "plan tier of this product (config/products/<id>.json billing.tiers), e.g. plus, pro, max"),
    ("extra_seats", "--extra-seats", int, "members allowed beyond the tier's included members"),
)

#: The columns ``--use-tier-limits`` clears to NULL, so name and limits follow the
#: tier's config (services/usage_gate.py::_effective_limits) instead of the row.
TIER_FOLLOWING = (
    "name",
    "rolling_window_hours",
    "rolling_limit_standard_tokens",
    "weekly_limit_standard_tokens",
)


class SpacePlanError(Exception):
    """A refusal about the plan surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise SpacePlanError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  show  this space's plan row, and the database it lives in\n"
            "  set   create or change it — a DRY RUN unless --apply is given"
        )
    return value


# ===========================================================================
# Rendering
# ===========================================================================
def render_target(target: dict, *, container_app: str | None) -> str:
    """WHERE this resolved to. A write whose target is not in its own output is
    not auditable, so this is printed on every call, read or write."""
    database = target.get("database") or {}
    lines = [
        f"space     {target.get('space_name') or '?'}  ({target.get('space_id')})",
        f"region    {target.get('region')}   (resolved by the server from the "
        "Global space registry — not from --env)",
        f"database  {database.get('database') or '?'} on "
        f"{database.get('host') or '?'}:{database.get('port') or '?'}",
    ]
    if container_app:
        lines.append(f"container {container_app}")
    if database.get("shared_with_global"):
        lines.append(
            "  ⚠️ this region shares ONE database with global on this deployment, "
            "so 'region' here is a label and not evidence about where a row landed"
        )
    return "\n".join(lines)


def render_plan(plan: dict | None) -> str:
    if plan is None:
        return (
            "plan      (none) — this space is UNGATED: every model call is allowed "
            "and nothing is settled"
        )
    fields = (
        ("name", plan.get("name")),
        ("rolling_window_hours", plan.get("rolling_window_hours")),
        ("rolling_limit_standard_tokens", plan.get("rolling_limit_standard_tokens")),
        ("weekly_limit_standard_tokens", plan.get("weekly_limit_standard_tokens")),
        ("week_reset_at", plan.get("week_reset_at")),
        ("additional_usage_enabled", plan.get("additional_usage_enabled")),
        ("additional_usage_disabled_reason", plan.get("additional_usage_disabled_reason")),
        ("tier", plan.get("tier")),
        ("extra_seats", plan.get("extra_seats")),
    )
    lines = ["plan"]
    for key, value in fields:
        shown = "NULL (⇒ the configured default)" if value is None else value
        lines.append(f"  {key:<34} {shown}")
    return "\n".join(lines)


def render_effects(effects: dict) -> str:
    """What the write DOES. Never omitted, including on a read."""
    lines = ["what a plan row does here", f"  {effects.get('billing_gate')}"]
    if effects.get("usage_gate_enabled_on_this_deployment") is False:
        lines.append(
            "  ⚠️ EVANE_USAGE_GATE_ENABLED is OFF on this deployment, so the row "
            "changes nothing until it is switched on — a quiet graph afterwards "
            "will not mean the plan is working"
        )
    else:
        lines.append("  the usage gate is switched ON on this deployment")
    lines.append(f"  {effects.get('catch_all_note')}")
    limits = effects.get("effective_limits") or {}
    lines.append("  effective limits (NULL columns resolved against the config):")
    for key, value in limits.items():
        lines.append(f"    {key:<32} {value}")
    return "\n".join(lines)


def render_change(payload: dict) -> str:
    """The dry-run / apply report."""
    outcome = payload.get("outcome")
    applied = bool(payload.get("applied"))
    changes = payload.get("changes") or {}
    lines = [f"outcome   {outcome}   (applied: {'yes' if applied else 'NO — dry run'})"]
    if changes:
        lines.append("changes")
        for key, pair in changes.items():
            before, after = pair if isinstance(pair, list) and len(pair) == 2 else (None, None)
            lines.append(f"  {key:<34} {before!r} -> {after!r}")
    elif outcome == "unchanged":
        lines.append("  (every requested value already matches this row)")
    return "\n".join(lines)


def next_command(args, revision: str) -> str:
    """The exact command that applies what was just shown."""
    parts = ["fm space-plan set", f"--host {getattr(args, 'host', None) or 'formist'}", f"--space {args.space}"]
    for field, flag, _type, _help in SETTABLE:
        value = getattr(args, field, None)
        if value is not None:
            parts.append(f"{flag} {value!r}" if isinstance(value, str) else f"{flag} {value}")
    if getattr(args, "additional_usage", None) is not None:
        parts.append(f"--additional-usage {args.additional_usage}")
    if getattr(args, "use_tier_limits", False):
        parts.append("--use-tier-limits")
    parts.append(f"--apply {revision}")
    return " ".join(parts)


# ===========================================================================
# CLI
# ===========================================================================
_HELP = "read or set a space's usage plan (dry run unless --apply)"

_DESCRIPTION = """A space's USAGE PLAN (space_plan): the two window limits and whether
overage is allowed.

  fm space-plan show --host evane --space <uuid>
  fm space-plan set  --host evane --space <uuid> --name '<product> Max' \\
                     --rolling-window-hours 5 --rolling-limit 20000000 \\
                     --weekly-limit 120000000 --additional-usage off

⚠️ A SPACE WITH NO PLAN ROW IS UNGATED. services/usage_gate.py allows every model
call and settles nothing. Creating the first row flips that space from FAIL-OPEN to
FAIL-CLOSED: the limits start being enforced, and a model with no configured
multiplier becomes a 503 model-not-priced for that space. The command prints the
live state of both of those before you apply anything.

⚠️ `set` IS A DRY RUN unless you pass --apply <revision>. The dry run prints the
revision of the row it read; --apply re-runs it, refuses if the row moved, and the
server re-checks the revision before writing.

⚠️ --env DOES NOT CHOOSE THE DATABASE. `space_plan` lives in whichever region
database owns the space's `project` row, and the server derives that from the Global
registry — there is no --region flag and there must never be one. /admin is served
from the GLOBAL region only, so use `--host evane` for a default-product prod space in
EITHER region; ev refuses to send an /admin call anywhere else before opening a
socket.

⚠️ `--host formist` IS DIFFERENT, NOT AN EXCEPTION TO THE RULE ABOVE: `formist` is a
whole separate deployment (its own subscription, database, container apps — see
`evtool/targets.py::REGION_FORMIST`), not a second region of the default product. It has no
Global/AE split, so its own single deployment serves /admin locally — `--env
formist` is the ONLY correct target for a space on that deployment, never `--host evane`.
"""



def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject",
        nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    # No default, deliberately: a WRITE must never inherit its environment from
    # `fm use`. See evtool/prefs.py and cli.py::_add_env(for_write=True).
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--space", help="space UUID (never a name)")
    for field, flag, type_, help_text in SETTABLE:
        parser.add_argument(flag, dest=field, type=type_, default=None, help=help_text)
    parser.add_argument(
        "--additional-usage",
        dest="additional_usage",
        choices=("on", "off"),
        default=None,
        help="allow usage to continue past a limit and be settled hourly",
    )
    parser.add_argument(
        "--use-tier-limits",
        dest="use_tier_limits",
        action="store_true",
        help="clear name and the three limits to NULL so they follow --tier's config",
    )
    parser.add_argument(
        "--apply",
        metavar="REVISION",
        default=None,
        help="commit, if the row still matches the revision this dry run printed",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "space-plan",
        help=_HELP,
        description=_DESCRIPTION,
        epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    """The same parser, detached, so ``--help`` can be asserted in a test."""
    return _build_standalone_parser(add_parser)


def requested_fields(args) -> dict:
    """The plan fields the operator actually named. Absent flag = leave alone.

    Only keys PRESENT here are sent, and the server applies only keys present in
    the body (``model_fields_set``) — so omitting a flag never silently NULLs a
    column that somebody else set.
    """
    body = {
        field: getattr(args, field)
        for field, _flag, _type, _help in SETTABLE
        if getattr(args, field, None) is not None
    }
    if getattr(args, "additional_usage", None) is not None:
        body["additional_usage_enabled"] = args.additional_usage == "on"
    if getattr(args, "use_tier_limits", False):
        clashing = [field for field in TIER_FOLLOWING if field in body]
        if clashing:
            raise SpacePlanError(
                "--use-tier-limits clears name and the limits; do not also set " + ", ".join(clashing)
            )
        body.update({field: None for field in TIER_FOLLOWING})
    return body


def _payload(response: object) -> dict:
    """This command's binding of :func:`formist_cli.cli_support.as_dict`.

    ⚠️ Deliberately NOT named ``_as_dict``. ``evtool/users.py`` has a function of
    that name binding the same shared body, and two same-named one-line bindings
    read to ``tests/test_no_cross_module_drift.py`` as one implementation copied
    into two modules (``diverged:impl:_as_dict sim=0.90``) — which is what it is.
    One shared body, one name per module for the binding.
    """
    return as_dict(response, what="/admin/spaces/…/plan", error=SpacePlanError)


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    # for_write on BOTH subjects: `show` is a read, but this command's whole
    # subject matter is a billing write, and a remembered environment is the one
    # an operator forgets they set. Resolving them differently would mean the
    # banner an operator reads on `show` describes a different resolution from the
    # one `set` will use — which is worse than requiring the flag twice.
    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)
    space = require_space_id(str(ctx.space))
    path = f"/admin/spaces/{space}/plan"

    if subject == "show":
        payload = _payload(api.admin_get(ctx.target, path, token=ctx.token))
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        target = payload.get("target") or {}
        print(render_target(target, container_app=None))
        print()
        print(render_plan(payload.get("plan")))
        print()
        print(render_effects(payload.get("effects") or {}))
        print(f"\nrevision  {payload.get('revision')}")
        return 0

    fields = requested_fields(args)
    if not fields:
        raise SpacePlanError(
            "nothing to set. Name at least one of:\n  "
            + "\n  ".join(f"{flag}  {help_}" for _f, flag, _t, help_ in SETTABLE)
            + "\n  --additional-usage on|off"
        )

    # THE DRY RUN ALWAYS RUNS, including on the apply path — a revision proves the
    # operator saw a plan, not that the plan is still true. Re-running is what makes
    # it a check instead of a receipt (evtool/update.py::apply, same reasoning).
    dry = _payload(api.admin_put(ctx.target, path, fields, token=ctx.token))
    target = dry.get("target") or {}
    revision = str(dry.get("revision"))

    if not args.apply:
        if args.json:
            print(json.dumps(dry, indent=2, ensure_ascii=False))
            return 0
        print(render_target(target, container_app=None))
        print()
        print(render_change(dry))
        print()
        print(render_effects(dry.get("effects") or {}))
        print(f"\nrevision  {revision}")
        print("\nNOTHING WAS WRITTEN. To apply exactly this:\n  " + next_command(args, revision))
        return 0

    if args.apply != revision:
        raise SpacePlanError(
            "REFUSING TO APPLY — this plan row is not the one you were shown.\n"
            f"  you supplied: {args.apply}\n"
            f"  current row:  {revision}\n"
            "\n"
            "Nothing was written. The revision covers the row's identity and every "
            "column this command writes, so the row changed underneath your dry "
            "run — somebody else set a plan, or this revision came from another "
            "space.\n"
            "\n"
            "→ Re-run without --apply, read it, and apply with the revision it prints."
        )

    applied = _payload(
        api.admin_put(
            ctx.target,
            path,
            fields | {"apply": True, "expect_revision": revision},
            token=ctx.token,
        )
    )
    if args.json:
        print(json.dumps(applied, indent=2, ensure_ascii=False))
        return 0
    print(render_target(target, container_app=None))
    print()
    print(render_change(applied))
    print(f"\nrevision  {applied.get('revision')}")
    return 0
