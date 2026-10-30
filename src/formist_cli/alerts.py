"""``fm alerts`` - platform risk alerts, over ``GET /admin/alerts*``.

Read-only; the email is the push channel, this is the pull one: filterable,
reviewable a week later, and shows an alert the hourly ceiling suppressed
from becoming an email at all.

These are platform alerts, not project notifications. ``project_id`` on a
row is attribution, never scope: most alerts have no project at all.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE
from formist_cli.cli_support import as_dict as _shared_alerts_payload
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

SUBJECTS = ("list", "conditions", "ack-parked")
DEFAULT_SUBJECT = "list"


class AlertsError(Exception):
    """A refusal about the alerts surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise AlertsError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  list        open (default) / resolved / all alerts\n"
            "  conditions  every condition this deployment CAN alert on\n"
            "  ack-parked  stop listing a connector's parked sync rows"
        )
    return value


def render_alerts(payload: dict) -> str:
    counts = payload.get("counts") or {}
    header = (
        f"open={counts.get('open', 0)}  critical={counts.get('critical', 0)}  "
        f"warning={counts.get('warning', 0)}  info={counts.get('info', 0)}"
    )
    alerts = payload.get("alerts") or []
    if not alerts:
        return f"{header}\n\n(no alerts match this filter)"
    lines = [
        header, "",
        f"{'severity':<9} {'condition':<38} {'occ':<4} {'last_seen':<20} id",
        f"{'-' * 9} {'-' * 38} {'-' * 4} {'-' * 20} {'-' * 8}",
    ]
    for row in alerts:
        resolved = "  (resolved)" if row.get("resolved") else ""
        lines.append(
            f"{str(row.get('severity') or '-'):<9} "
            f"{str(row.get('condition') or '-'):<38.38} "
            f"{row.get('occurrences', '?'):<4} "
            f"{str(row.get('last_seen_at') or '-'):<20} "
            f"{row.get('id')}{resolved}"
        )
        if row.get("title"):
            lines.append(f"    {row['title']}")
    total = payload.get("total")
    shown = len(alerts)
    if total is not None and shown < int(total):
        lines.append(
            f"\n{shown} shown of {total}, MORE EXIST. Raise --limit or page with --offset."
        )
    return "\n".join(lines)


def render_conditions(payload: dict) -> str:
    origin = payload.get("deployment") or {}
    lines = [
        f"deployment  {origin.get('region', '?')}/{origin.get('env_name', '?')}",
        f"alerts_enabled={payload.get('alerts_enabled')}  "
        f"email_enabled={payload.get('email_enabled')}  "
        f"admin_email_configured={payload.get('admin_email_configured')}",
        f"evaluation_interval_seconds={payload.get('evaluation_interval_seconds')}  "
        f"max_emails_per_hour={payload.get('max_emails_per_hour')}",
        "",
        f"{'key':<40} {'category':<10} {'severity':<9} available",
        f"{'-' * 40} {'-' * 10} {'-' * 9} {'-' * 9}",
    ]
    for c in payload.get("conditions") or []:
        available = "yes" if c.get("available") else f"NO (needs {c.get('requires')})"
        lines.append(
            f"{str(c.get('key') or '-'):<40.40} "
            f"{str(c.get('category') or '-'):<10} "
            f"{str(c.get('severity') or '-'):<9} "
            f"{available}"
        )
    return "\n".join(lines)


_HELP = "platform risk alerts and the conditions that can fire (admin)"

_DESCRIPTION = """Platform-wide risk alerts, the PULL channel alongside the admin
email (the PUSH channel). Filterable and reviewable after the fact, unlike email.

  fm alerts --host evane                              open alerts (default)
  fm alerts --host evane --status all --severity critical
  fm alerts conditions --host evane                   what CAN alert, and why not
  fm alerts ack-parked <connection-id> --env ae       stop listing its parked sync rows
  fm alerts ack-parked <connection-id> --record recX  just one row (repeatable)

project_id on a row is ATTRIBUTION, not scope: most alerts have no project.
"""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject", nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument(
        "connection", nargs="?", help="ack-parked: the connection id",
    )
    parser.add_argument(
        "--record", action="append", default=None, metavar="RECORD_ID",
        help="ack-parked: one source record id (repeatable; default: every parked row)",
    )
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--status", choices=("open", "resolved", "all"), default=None)
    parser.add_argument("--severity", choices=("critical", "warning", "info"), default=None)
    parser.add_argument("--condition", default=None, help="filter to one condition key")
    parser.add_argument("--region", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "alerts", help=_HELP, description=_DESCRIPTION, epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    ctx = context.resolve(args, need_token=True)

    if subject == "conditions":
        payload = api.admin_get(ctx.target, "/admin/alerts/conditions", token=ctx.token)
        payload = _alerts_payload(payload)
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
              else render_conditions(payload))
        return 0

    if subject == "ack-parked":
        connection = getattr(args, "connection", None)
        if not connection:
            raise AlertsError("ack-parked needs the connection id: fm alerts ack-parked <id>")
        body = {"connection_id": connection, "record_ids": getattr(args, "record", None)}
        payload = _alerts_payload(
            api.admin_post(
                ctx.target, "/admin/alerts/parked-rows/acknowledge", body, token=ctx.token
            )
        )
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"acknowledged {len(payload.get('acknowledged') or [])} row(s); "
                f"{payload.get('still_listed', '?')} still listed"
                + (f"; not parked: {', '.join(payload['unknown'])}" if payload.get("unknown") else "")
            )
        return 0

    params: dict[str, str] = {}
    for flag in ("status", "severity", "condition", "region"):
        value = getattr(args, flag, None)
        if value is not None:
            params[flag] = str(value)
    if getattr(args, "limit", None) is not None:
        params["limit"] = str(args.limit)
    if getattr(args, "offset", None) is not None:
        params["offset"] = str(args.offset)

    payload = api.admin_get(ctx.target, "/admin/alerts", token=ctx.token, params=params)
    payload = _alerts_payload(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
          else render_alerts(payload))
    return 0


def _alerts_payload(payload: object) -> dict:
    return _shared_alerts_payload(payload, what="/admin/alerts", error=AlertsError)
