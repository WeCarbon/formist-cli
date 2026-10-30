"""``fm usage-tokens``: system-wide LLM token spend, over ``GET /admin/usage/tokens``.

Read-only; there is nothing to apply here.

A zero can mean "measured, and it really is zero" or "unmeasured", and
those are different facts. Every bucket the server returns carries
``unmeasured_calls`` / ``unpriced_calls`` and the derived
``tokens_are_lower_bound`` / ``cost_is_lower_bound`` flags. This
renderer prints them on every row, not only when they are non-zero,
since a flag that only sometimes appears is easy to miss when scanning
a column.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE
from formist_cli.cli_support import as_dict as _shared_usage_payload
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

GROUP_BY_CHOICES = ("source", "operation", "space", "user", "model", "provider", "day")


class UsageTokensError(Exception):
    """A refusal about the usage-tokens surface, written for an operator."""


def render(payload: dict) -> str:
    lines = [
        f"window   {payload.get('since')} .. {payload.get('until')}"
        f"   group_by={payload.get('group_by')}",
        f"regions  {', '.join(payload.get('regions') or [])}",
    ]
    if payload.get("meter_enabled") is False:
        lines.append(
            "  TOKEN_METER_ENABLED is OFF on this deployment, every number below "
            "is stale by however long it has been off"
        )
    lines.append("")
    lines.append(_row("TOTAL", payload.get("totals") or {}))
    lines.append("")
    groups = payload.get("groups") or []
    if not groups:
        lines.append("(no rows in this window)")
    for g in groups:
        lines.append(_row(str(g.get("label") or g.get("key") or "(unattributed)"), g))
    if payload.get("truncated"):
        lines.append(
            f"\n{len(groups)} of {payload.get('group_count')} groups shown, MORE "
            "EXIST. Raise --limit for the rest."
        )
    return "\n".join(lines)


def _row(label: str, bucket: dict) -> str:
    """Render one bucket. The two lower-bound flags are printed
    unconditionally, since a flag that only shows up when it fires is
    easy to miss when scanning.
    """
    tokens_flag = "LOWER BOUND" if bucket.get("tokens_are_lower_bound") else "measured"
    cost_flag = "LOWER BOUND" if bucket.get("cost_is_lower_bound") else "priced"
    return (
        f"{label:<28.28} calls={bucket.get('calls', 0):<6} "
        f"tokens={bucket.get('total_tokens', 0):<10} ({tokens_flag}) "
        f"cost=${bucket.get('estimated_cost_usd', 0):<10} ({cost_flag}) "
        # Subsets of tokens above, not additive, same as cached_input_tokens,
        # which this line does not print either (see `--json` for the full
        # per-bucket breakdown). Surfaced here because cache_write is priced
        # at a premium.
        f"cache_write={bucket.get('cache_write_tokens', 0)} "
        f"reasoning={bucket.get('reasoning_tokens', 0)} "
        f"unmeasured={bucket.get('unmeasured_calls', 0)} "
        f"unpriced={bucket.get('unpriced_calls', 0)} "
        f"failed={bucket.get('failed_calls', 0)}"
    )


_HELP = "system-wide LLM token spend, aggregated across every region (admin, read-only)"

_DESCRIPTION = """Total LLM token spend across the whole deployment, sliceable by
source, operation, space, user, model, provider or day.

  fm usage-tokens --host evane
  fm usage-tokens --host evane --group-by space --since 2026-09-01
  fm usage-tokens --host evane --group-by model --space <space-uuid>

A zero in this table can mean "measured, and it really is zero" or "the
backend reported no usage"; those are different facts. Every row is labelled
`measured`/`LOWER BOUND` (tokens) and `priced`/`LOWER BOUND` (cost) so the two
are never read as the same thing. `unmeasured`/`unpriced`/`failed` are the
counts behind those labels.
"""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--group-by", dest="group_by", choices=GROUP_BY_CHOICES, default="source")
    parser.add_argument("--since", default=None, help="ISO-8601; default: 7 days before --until")
    parser.add_argument("--until", default=None, help="ISO-8601; default: now")
    parser.add_argument("--space", dest="space_id", default=None, help="filter to one space UUID")
    parser.add_argument("--user", dest="user_id", default=None, help="filter to one user UUID")
    parser.add_argument("--source", default=None)
    parser.add_argument("--operation", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--limit", type=int, default=None, help="max groups returned (server caps it)")
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "usage-tokens", help=_HELP, description=_DESCRIPTION, epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def run(args) -> int:
    ctx = context.resolve(args, need_token=True)
    params: dict[str, str] = {"group_by": getattr(args, "group_by", None) or "source"}
    for flag in (
        "since", "until", "space_id", "user_id", "source", "operation", "model", "provider",
    ):
        value = getattr(args, flag, None)
        if value is not None:
            params[flag] = str(value)
    if getattr(args, "limit", None) is not None:
        params["limit"] = str(args.limit)

    payload = api.admin_get(ctx.target, "/admin/usage/tokens", token=ctx.token, params=params)
    payload = _usage_payload(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render(payload))
    return 0


def _usage_payload(payload: object) -> dict:
    return _shared_usage_payload(payload, what="/admin/usage/tokens", error=UsageTokensError)
