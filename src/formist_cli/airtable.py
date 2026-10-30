"""``fm airtable direction`` - what an Airtable grant can actually do, and repair.

The decision this drives lives on the server, and this module is a
client that holds no copy of it. That split is the entire point: a
client that reached the database directly would be a second
implementation of "may this connection write back to Airtable", and two
answers to that question is how a connection ends up 403-ing every
outbound write on every tick while a script insists it is fine.

What stays on this side is only presentation and the safety choreography:

A dry run always runs, including under ``--apply``. ``--apply`` does not
skip the GET; it performs it, prints it, and then submits exactly the
lines it printed. The server re-probes and refuses anything that no
longer matches, so the operator authorizes a plan they have read, not a
verb they typed.

``--apply`` will not inherit an environment. A read may resolve ``--env``
from ``fm use``; this write may not. A remembered environment is one
somebody forgets they set, and the cost of forgetting is a write against
production.

Inconclusive is printed, never folded into "fine". Four things (no
credential, a failed probe, no bases, no ``permissionLevel``) mean
nothing was learned. They are the reason an operator is running this at
all, and a summary that reported them as "0 changes" would be a check
that cannot fail.
"""

from __future__ import annotations

import json
import sys
from typing import Any

ENDPOINT = "/projects/{space}/admin/airtable/direction"

#: How a plan line's reason code reads at a terminal. Unknown codes fall through
#: verbatim rather than being dropped: a reason this client has never heard of is
#: still the server's answer, and hiding it would be worse than printing it raw.
_REASONS = {
    "ok": "",
    "no-credential": "NO CREDENTIAL",
    "probe-failed": "PROBE FAILED",
    "no-bases": "NO BASES",
    "no-permission-level": "NO PERMISSION LEVEL",
}


class AirtableError(Exception):
    """A refusal or a malformed answer, phrased for whoever has to fix it."""


def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "airtable",
        help="inspect and repair an Airtable connection's sync direction",
        description=(
            "Asks AIRTABLE what each connection's token can actually do, and "
            "reports whether the stored sync direction agrees.\n\n"
            "The stored OAuth scope cannot answer this. A real connection carries "
            "data.records:write in payload.oauth.scope while the base's actual "
            "permissionLevel is 'read': the scope was granted, the permission was "
            "never there. So this makes a live call, per connection.\n\n"
            "DRY RUN BY DEFAULT. --apply re-runs the dry run, prints it, and "
            "submits exactly those lines; the server re-probes and applies only "
            "what still matches. --apply never inherits an environment from "
            "`fm use`: with one remembered the write is REFUSED until you pass "
            "--env, and with nothing remembered it targets local.\n\n"
            "This only ever DEMOTES a connection to 'inbound'. A writable base is "
            "not a request to write, so nothing is ever promoted to "
            "'outbound'/'both' here."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument(
        "subject",
        choices=("direction",),
        help="what to operate on (today: direction)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "persist the changes this run reports. Refuses a remembered `fm use` "
            "environment: pass --env to write anywhere but local"
        ),
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


def fetch_plan(target, space: str, *, token: str | None, get=None) -> dict:
    """The dry run: every Airtable connection in the space, probed live."""
    from formist_cli import api

    get = get or api.get
    body = get(target, ENDPOINT.format(space=space), token=token)
    return _envelope(body)


def submit(target, space: str, changes: list[dict], *, token: str | None, post=None) -> dict:
    """Authorize exactly the plan lines in ``changes``."""
    from formist_cli import api

    post = post or api.post
    body = post(target, ENDPOINT.format(space=space), {"changes": changes}, token=token)
    return _envelope(body)


def _envelope(body: Any) -> dict:
    if not isinstance(body, dict):
        raise AirtableError(
            f"expected a JSON object from the direction endpoint, got "
            f"{type(body).__name__}. This is not a plan and must not be acted on."
        )
    return body


def changes_from(plan: dict) -> list[dict]:
    """The lines to authorize: the ones the SERVER marked ``would_change``.

    Read off the server's own flag rather than recomputed here as
    ``current != wanted``. The two agree today and would diverge the moment an
    inconclusive probe grew a ``wanted`` that differs (a reasonable future change),
    at which point a client re-derivation would start authorizing writes on
    connections the server had explicitly declined to decide about.
    """
    return [
        {
            "connection_id": item.get("connection_id"),
            "current": item.get("current"),
            "wanted": item.get("wanted"),
        }
        for item in plan.get("items") or []
        if item.get("would_change")
    ]


def render_plan(plan: dict, *, applying: bool, out=None) -> None:
    out = out or sys.stdout
    items = plan.get("items") or []
    counts = plan.get("counts") or {}
    print(f"{len(items)} AIRTABLE connection(s)", file=out)
    for item in items:
        flag = _REASONS.get(item.get("reason"), str(item.get("reason")))
        head = f"  {item.get('connection_id')}"
        detail = item.get("detail") or ""
        if item.get("would_change"):
            suffix = "" if applying else "   (dry run)"
            print(
                f"{head}: {detail} -> direction {item.get('current')!r} => {item.get('wanted')!r}{suffix}",
                file=out,
            )
        elif item.get("conclusive"):
            print(
                f"{head}: {detail} -> direction {item.get('current')!r} is correct",
                file=out,
            )
        else:
            print(
                f"{head}: {flag}: {detail}; left at {item.get('current')!r}",
                file=out,
            )
    print(
        f"\n{counts.get('would_change', 0)} "
        + ("to change" if applying else "would change")
        + f" | {counts.get('inconclusive', 0)} inconclusive",
        file=out,
    )


def render_outcomes(report: dict, *, out=None) -> None:
    out = out or sys.stdout
    outcomes = report.get("outcomes") or []
    print("\napplied:", file=out)
    for o in outcomes:
        mark = "OK" if o.get("applied") else "SKIP"
        print(f"  {mark} {o.get('connection_id')}: {o.get('detail')}", file=out)
    counts = report.get("counts") or {}
    print(f"\n{counts.get('applied', 0)} changed", file=out)


def cmd_airtable(args) -> int:
    from formist_cli.cli import _effective, _token_for

    # for_write is the --apply flag itself: the dry run may inherit an environment
    # from `fm use`, the write may not. Same rule as `fm update`, applied to the
    # half of this command that actually writes.
    eff = _effective(args, for_write=bool(args.apply))
    target, space = eff.target, eff.space
    token = _token_for(eff)

    # The dry run happens either way. Under --apply it is printed FIRST and then
    # submitted verbatim, so the terminal shows what is about to be authorized.
    plan = fetch_plan(target, space, token=token)
    if not args.json:
        render_plan(plan, applying=bool(args.apply))
    if not args.apply:
        if args.json:
            print(json.dumps(plan, indent=2))
        elif plan.get("counts", {}).get("would_change"):
            print("\n-> re-run with --apply --host <host> to persist this.")
        return 0

    changes = changes_from(plan)
    if not changes:
        # Nothing to authorize. Deliberately NOT posted: the endpoint refuses an
        # empty change list, and turning "already correct" into a 400 would train
        # an operator to read a refusal as routine.
        if args.json:
            counts = {**(plan.get("counts") or {}), "applied": 0}
            print(json.dumps({**plan, "outcomes": [], "counts": counts}, indent=2))
        else:
            print("\nnothing to change, no request was sent.")
        return 0

    report = submit(target, space, changes, token=token)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    render_outcomes(report)
    stalled = [o for o in report.get("outcomes") or [] if o.get("reason") == "plan-moved"]
    if stalled:
        print(
            f"\n{len(stalled)} connection(s) were NOT applied because the live "
            "probe disagreed with the plan you were shown. Re-run the dry run.",
            file=sys.stderr,
        )
        return 1
    return 0
