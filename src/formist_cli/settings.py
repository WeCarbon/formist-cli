"""``fm settings`` — read and flip the admin-editable runtime switches.

The CLI half of ``GET/PATCH /admin/settings`` (``routes/admin_settings.py``),
per the standing rule that every admin function is reachable from ``ev``.

    ev settings show --host evane
    ev settings set --host evane scheduler.disabled=true
    ev settings clear --host evane

⚠️ THE CHANGE IS PER REGION. The values live in a config-format document in
that region's blob storage (``file:`` in the output names it), so ``--env ae``
and ``--host evane`` are two separate changes. Every replica of the region
reads it, and it survives restarts. Every response prints the instance that
answered.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

SUBJECTS = ("show", "set", "clear")
DEFAULT_SUBJECT = "show"

ENDPOINT = "/admin/settings"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
#: The third state of the push-sync switches: neither on nor off, but "decide
#: from the notification URL". Spelled out rather than inferred from an empty
#: value, so `field=` is a typo rather than a silent reset.
_AUTO = {"auto", "null", "none", "unset"}


class SettingsError(Exception):
    """A refusal about this surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise SettingsError(f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.")
    return value


def parse_assignments(items: list[str]) -> dict[str, bool | None]:
    """``["a.b=true", "c.d=0", "e.f=auto"]`` -> ``{"a.b": True, "c.d": False, "e.f": None}``.

    Refuses anything outside the three vocabularies rather than guessing:
    ``scheduler.disabled=maybe`` silently becoming ``False`` is how an operator
    ends up believing a switch is off.
    """
    out: dict[str, bool | None] = {}
    for item in items:
        if "=" not in item:
            raise SettingsError(f"expected field=value, got {item!r}")
        field, _, raw = item.partition("=")
        field = field.strip()
        value = raw.strip().lower()
        if value in _TRUE:
            out[field] = True
        elif value in _FALSE:
            out[field] = False
        elif value in _AUTO:
            out[field] = None
        else:
            raise SettingsError(
                f"{field}={raw!r} is not a boolean; use one of "
                f"{', '.join(sorted(_TRUE))} / {', '.join(sorted(_FALSE))} "
                f"/ {', '.join(sorted(_AUTO))}"
            )
    return out


def render_show(payload: dict) -> str:
    lines = [f"instance: {payload.get('instance')}", f"file:     {payload.get('file')}"]
    if not payload.get("file_exists"):
        lines.append("          (no overrides document yet — every value below is env/run-config)")
    lines.append("")
    width = max((len(f["field"]) for f in payload.get("fields", [])), default=0)
    for entry in payload.get("fields", []):
        mark = "*" if entry.get("overridden") else " "
        value = "auto" if entry["value"] is None else str(entry["value"]).lower()
        restart = "  (restart)" if entry.get("restart_required") else ""
        lines.append(f" {mark} {entry['field']:<{width}}  {value}{restart}")
    lines.append("")
    lines.append("* = set by this region's overrides document")
    lines.append("(restart) = read once at startup; the change lands on the next one")
    lines.append(f"note: {payload.get('scope_note')}")
    return "\n".join(lines)


def render_patch(payload: dict) -> str:
    lines = [f"instance: {payload.get('instance')}", f"file:     {payload.get('file')}"]
    for change in payload.get("applied", []):
        before = "auto" if change["before"] is None else str(change["before"]).lower()
        after = "auto" if change["after"] is None else str(change["after"]).lower()
        lines.append(f"  {change['field']}: {before} -> {after}")
    for field in payload.get("unchanged", []):
        lines.append(f"  {field}: unchanged")
    for field in payload.get("cleared", []):
        lines.append(f"  {field}: override removed")
    if not any(payload.get(k) for k in ("applied", "unchanged", "cleared")):
        lines.append("  (nothing changed)")
    restart = payload.get("restart_required") or []
    if restart:
        lines.append(
            f"restart this instance for: {', '.join(restart)}"
        )
    return "\n".join(lines)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject", nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument(
        "assignments", nargs="*", metavar="field=value",
        help="for `set`: one or more dotted field names with a boolean value",
    )
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


_HELP = "read or flip the admin-editable runtime switches (admin)"

_DESCRIPTION = """Read and change the switches an admin may flip without a deploy.

  ev settings show  --host evane
  ev settings set   --host evane scheduler.disabled=true
  ev settings set   --env ae     connectors.sync_enabled=false connectors.agentic_review=true
  ev settings clear --host evane

The values live in a config-format document in each region's blob storage, so
`--env ae` and `--host evane` are two separate changes; every replica of a
region reads its document, and it survives restarts. `show` marks with a `*`
every field the document decides; the rest come from the environment or the
deployment's run-config.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "settings", help=_HELP, description=_DESCRIPTION,
        epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    assignments = list(getattr(args, "assignments", []) or [])

    if subject == "set" and not assignments:
        raise SettingsError("`fm settings set` needs at least one field=value")
    if subject != "set" and assignments:
        raise SettingsError(f"`fm settings {subject}` takes no field=value arguments")

    ctx = context.resolve(args, for_write=subject != "show", need_token=True)

    if subject == "show":
        payload = api.admin_get(ctx.target, ENDPOINT, token=ctx.token)
        render = render_show
    else:
        body = (
            {"clear": True}
            if subject == "clear"
            else {"values": parse_assignments(assignments)}
        )
        payload = api.admin_patch(ctx.target, ENDPOINT, body, token=ctx.token)
        render = render_patch

    if not isinstance(payload, dict):
        raise SettingsError(f"{ENDPOINT} returned a non-object response: {payload!r}")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render(payload))
    return 0
