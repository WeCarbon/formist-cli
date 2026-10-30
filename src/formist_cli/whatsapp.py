"""``fm whatsapp`` - platform-admin WhatsApp device management, over
``/admin/whatsapp/devices*``.

Region-local, like `fm alerts` / `fm messages`. A ``whatsapp_device`` row is
regional: each region's instance manages the devices that live in its own
database, exactly like ``/admin/alerts``. So ``--env ae`` reads the AE
region's own devices; ``--host evane`` reads Global's, which is a different,
usually-empty set. There is no single "the" list across regions, only
per-region ones.

Writes. Every mutating subject here (``add``, ``pair``, ``start``,
``stop``, ``logout``, ``mark-account-deleted``, ``delete``) takes effect
immediately, none has a server-side dry run, so ``--env`` never inherits from
``fm use`` on a write, the same rule as every other write in this CLI.

``delete`` is irreversible and the server itself refuses it while
``account_deleted_at`` is unset, but this command adds its own gate on top:
it fetches the device, prints its phone number, and asks the operator to
type it back before sending the delete, unless ``--yes`` is given. A device
id is 36 characters nobody reads by eye; the phone number is the thing an
operator actually recognises, so that is what has to be typed, the same
reasoning `fm spaces delete`'s ``--confirm`` exists for, one step further
because there is no dry run to show first.

``pair`` POSTs ``{"method": "qr"|"code"}`` to ``.../pair``, then polls
``GET .../pairing`` every few seconds until the device reports ``connected`` or
``--timeout`` (default 300s) elapses. A QR is rendered as a scannable terminal
code via the optional third-party ``qrcode`` package, imported lazily and
only here, since `fm` is stdlib-only at module import time, so a missing
``qrcode`` prints the raw QR string instead of failing the command;
``--code`` sidesteps the whole question by asking the server for a phone
code instead.

``fm whatsapp runner start|stop|status`` is the armed switch, over
``/admin/whatsapp/runner*``. This is process-wide, independent of any one
device's own ``start``/``stop`` above: a dev box can set autostart off so a
reload never reconnects on its own, and this is the manual step that
(re)connects it. ``status`` (the default with no action given) is a read;
``start``/``stop`` are writes, so ``--env`` never inherits from ``fm use``
for any of the three, the same as every other write in this file.
"""

from __future__ import annotations

import argparse
import json
import re
import time

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE
from formist_cli.cli_support import as_dict as _shared_as_dict
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser

SUBJECTS = (
    "devices", "add", "show", "pair",
    "start", "stop", "logout", "mark-account-deleted", "delete", "groups",
    "runner",
)
DEFAULT_SUBJECT = "devices"

#: Write subjects, resolved with ``for_write=True`` (never inherits ``--env``
#: from ``fm use``), same convention as every other mutating command in this
#: CLI. ``runner`` covers all three of its own actions
#: (``start``/``stop``/``status``) even though ``status`` is a read, see
#: the module docstring.
_WHATSAPP_WRITE_SUBJECTS = frozenset({
    "add", "pair", "start", "stop", "logout",
    "mark-account-deleted", "delete", "runner",
})

DEVICES_PATH = "/admin/whatsapp/devices"
RUNNER_PATH = "/admin/whatsapp/runner"
RUNNER_ACTIONS = ("status", "start", "stop")
DEFAULT_RUNNER_ACTION = "status"

#: Poll cadence for `pair`: every 2-3 seconds, since the QR rotates roughly
#: every 20 seconds.
PAIR_POLL_INTERVAL_SECONDS = 3
DEFAULT_PAIR_TIMEOUT_SECONDS = 300

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

#: The device response's own field order, used to render `show`/`add`/every
#: write that returns a device: one table, so a field added to the server
#: response and forgotten here is a visible gap, not a silently-dropped
#: column (``.get`` returns ``None`` and the row still prints, labelled).
_DEVICE_FIELDS = (
    "id", "phone_e164", "display_name", "region", "status", "enabled",
    "jid", "push_name", "platform",
    "account_deleted_at",
    "last_connected_at", "last_seen_at", "last_error",
    "groups_cached_at", "history_synced_at",
    "registration_recordings",
    "created_at", "updated_at",
)


class WhatsAppError(Exception):
    """A refusal about the WhatsApp device surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise WhatsAppError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  devices              every device in this region (default)\n"
            "  add <+E164>          register a new device (status: registering)\n"
            "  show <id>            one device\n"
            "  pair <id>            pair by QR (or --code); polls until connected\n"
            "  start|stop <id>      enable/disable the runner for this device\n"
            "  logout <id>          unlink from the primary phone (WhatsApp-side)\n"
            "  mark-account-deleted <id>  attest the WhatsApp account was deleted\n"
            "  delete <id>          soft-delete the row (typed phone confirmation)\n"
            "  groups <id>          this device's joined groups\n"
            "  runner [start|stop|status]  arm/disarm the runner PROCESS (default: status)"
        )
    return value


def device_path(device_id: str) -> str:
    return f"{DEVICES_PATH}/{device_id}"


# ===========================================================================
# Rendering
# ===========================================================================
def render_devices(items: list[dict]) -> str:
    if not items:
        return "(no WhatsApp devices in this region)"
    lines = [
        f"{'phone':<16} {'status':<13} {'en':<3} {'region':<7} {'display_name':<24} id",
        f"{'-' * 16} {'-' * 13} {'-' * 3} {'-' * 7} {'-' * 24} {'-' * 8}",
    ]
    for row in items:
        lines.append(
            f"{str(row.get('phone_e164') or '-'):<16} "
            f"{str(row.get('status') or '-'):<13} "
            f"{'y' if row.get('enabled') else 'n':<3} "
            f"{str(row.get('region') or '-'):<7} "
            f"{str(row.get('display_name') or '-'):<24.24} "
            f"{row.get('id')}"
        )
    return "\n".join(lines)


def render_device(device: dict) -> str:
    lines = []
    for field in _DEVICE_FIELDS:
        value = device.get(field)
        if field == "registration_recordings":
            value = f"{len(value)} recording(s)" if value else "(none)"
        lines.append(f"  {field:<28} {value}")
    return "\n".join(lines)


def render_runner(payload: dict) -> str:
    lines = [f"armed={payload.get('armed')}"]
    if "autostart" in payload:
        lines.append(f"autostart={payload.get('autostart')}")
    if "started" in payload:
        lines.append(f"started={payload.get('started')}")
    sessions = payload.get("sessions")
    if sessions is not None:
        if not sessions:
            lines.append("(no live sessions)")
        else:
            lines.append("")
            lines.append(f"{'device_id':<38} {'status':<13} connected")
            lines.append(f"{'-' * 38} {'-' * 13} {'-' * 9}")
            for row in sessions:
                lines.append(
                    f"{str(row.get('device_id')):<38} "
                    f"{str(row.get('status') or '-'):<13} "
                    f"{'y' if row.get('connected') else 'n'}"
                )
    return "\n".join(lines)


def render_groups(payload: dict) -> str:
    items = payload.get("items") or []
    lines = [f"cached_at={payload.get('cached_at') or '?'}", ""]
    if not items:
        lines.append("(no groups cached for this device, connect it, or GET again later)")
        return "\n".join(lines)
    lines.append(f"{'participants':<12} {'eph':<6} {'announce':<9} {'name':<28} jid")
    lines.append(f"{'-' * 12} {'-' * 6} {'-' * 9} {'-' * 28} {'-' * 8}")
    for row in items:
        lines.append(
            f"{row.get('participants', '?'):<12} "
            f"{row.get('ephemeral_seconds', 0):<6} "
            f"{'y' if row.get('announce_only') else 'n':<9} "
            f"{str(row.get('group_name') or '-'):<28.28} "
            f"{row.get('jid')}"
        )
    return "\n".join(lines)


def _render_qr(qr: str) -> None:
    """Render ``qr`` as a scannable terminal code, or fall back to the raw string.

    ``qrcode`` is a third-party package and `fm` must add no runtime
    dependency, so it is imported lazily, here, only when a QR actually
    needs rendering, and only ever inside this try/except. A missing
    package is not a command failure: the raw string still pairs a device,
    and ``--code`` is the documented way around needing this at all.
    """
    try:
        import qrcode
    except ImportError:
        print(f"QR (install the `qrcode` package to render this in the terminal): {qr}")
        print("  -> or re-run with --code to pair by phone code instead.")
        return
    code = qrcode.QRCode()
    code.add_data(qr)
    code.make(fit=True)
    code.print_ascii(invert=True)


def _show_pairing(state: dict, last_qr: str | None) -> str | None:
    """Print whatever ``state`` currently offers; return the QR string just
    shown so the caller can skip re-rendering an unchanged one. The QR
    rotates roughly every 20 seconds, so an unchanged value between polls
    is the common case, not the exception."""
    qr = state.get("qr")
    pair_code = state.get("pair_code")
    if qr and qr != last_qr:
        _render_qr(qr)
        last_qr = qr
    if pair_code:
        print(f"pair code: {pair_code}")
    if not qr and not pair_code:
        print(f"status={state.get('status')}, waiting for the device to issue a code/QR...")
    return last_qr


# ===========================================================================
# CLI
# ===========================================================================
_HELP = "platform-admin WhatsApp devices: register, pair, start/stop, retire (admin)"

_DESCRIPTION = """WhatsApp linked-device management, region-local, like `fm alerts`.

  fm whatsapp --env ae                                   every device in AE (default subject)
  fm whatsapp add +97150... --name 'Event bot' --env ae
  fm whatsapp pair <id> --env ae                          QR in the terminal; --code for a phone code
  fm whatsapp start|stop|logout|mark-account-deleted <id> --env ae
  fm whatsapp delete <id> --env ae                        types the phone number back, or --yes
  fm whatsapp groups <id> --env ae
  fm whatsapp runner start|stop|status --env ae           arm/disarm the runner process (default: status)

`delete` is irreversible server-side once `account_deleted_at` is set, and this
command asks you to type the device's phone number back first (skip with --yes).
"""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject", nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument(
        "record", nargs="?",
        help="device UUID for most subjects (`add`: the +E164 phone number; "
             "`runner`: one of start|stop|status, default status)",
    )
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--name", dest="display_name", default=None, help="`add`: a human label")
    parser.add_argument(
        "--code", action="store_true", help="`pair`: pair by phone code instead of QR",
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help=f"`pair`: seconds to poll before giving up (default: {DEFAULT_PAIR_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="`delete`: skip the typed phone-number confirmation",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "whatsapp", help=_HELP, description=_DESCRIPTION, epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def _require_record(args, subject: str) -> str:
    value = getattr(args, "record", None)
    if not value or not value.strip():
        if subject == "add":
            raise WhatsAppError(
                "`fm whatsapp add` needs a phone number (E.164, e.g. +97150...) as "
                "the second argument."
            )
        raise WhatsAppError(
            f"`fm whatsapp {subject}` needs a device id as the second argument.\n"
            "-> `fm whatsapp devices` lists them (id is the last column)."
        )
    return value.strip()


def _validate_phone(value: str) -> str:
    if not _E164_RE.match(value):
        raise WhatsAppError(
            f"`fm whatsapp add` needs an E.164 phone number like +97150XXXXXXX, got {value!r}."
        )
    return value


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    ctx = context.resolve(args, for_write=subject in _WHATSAPP_WRITE_SUBJECTS, need_token=True)

    if subject == "devices":
        payload = _payload(api.admin_get(ctx.target, DEVICES_PATH, token=ctx.token))
        items = payload.get("items") or []
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
              else render_devices(items))
        return 0

    if subject == "runner":
        return _run_runner(ctx, args)

    if subject == "add":
        phone = _validate_phone(_require_record(args, subject))
        body: dict = {"phone_e164": phone}
        if getattr(args, "display_name", None):
            body["display_name"] = args.display_name
        payload = _payload(api.admin_post(ctx.target, DEVICES_PATH, body, token=ctx.token))
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
              else render_device(payload))
        return 0

    device_id = _require_record(args, subject)

    if subject == "show":
        payload = _payload(api.admin_get(ctx.target, device_path(device_id), token=ctx.token))
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
              else render_device(payload))
        return 0

    if subject == "pair":
        return _run_pair(ctx, device_id, args)

    if subject in ("start", "stop", "logout", "mark-account-deleted"):
        payload = _payload(
            api.admin_post(ctx.target, f"{device_path(device_id)}/{subject}", {}, token=ctx.token)
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
              else render_device(payload))
        return 0

    if subject == "delete":
        return _run_delete(ctx, device_id, args)

    # groups
    payload = _payload(api.admin_get(ctx.target, f"{device_path(device_id)}/groups", token=ctx.token))
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render_groups(payload))
    return 0


def _run_runner(ctx, args) -> int:
    """`fm whatsapp runner [start|stop|status]`, the armed switch (module
    docstring). `record` carries the action here, the same slot `add` uses for
    a phone number and `show`/`pair`/etc. use for a device id: this file's
    existing per-subject overload of that one positional, not a new shape."""
    action = (getattr(args, "record", None) or DEFAULT_RUNNER_ACTION).strip().lower()
    if action not in RUNNER_ACTIONS:
        raise WhatsAppError(
            f"`fm whatsapp runner` action must be one of {'|'.join(RUNNER_ACTIONS)}, got {action!r}."
        )
    if action == "status":
        payload = _payload(api.admin_get(ctx.target, RUNNER_PATH, token=ctx.token))
    else:
        payload = _payload(api.admin_post(ctx.target, f"{RUNNER_PATH}/{action}", {}, token=ctx.token))
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render_runner(payload))
    return 0


def _run_pair(ctx, device_id: str, args) -> int:
    method = "code" if getattr(args, "code", False) else "qr"
    timeout = getattr(args, "timeout", None) or DEFAULT_PAIR_TIMEOUT_SECONDS

    state = _payload(
        api.admin_post(ctx.target, f"{device_path(device_id)}/pair", {"method": method}, token=ctx.token)
    )
    last_qr = _show_pairing(state, None)

    deadline = time.monotonic() + timeout
    while state.get("status") != "connected":
        if time.monotonic() >= deadline:
            raise WhatsAppError(
                f"timed out after {timeout}s waiting for device {device_id} to report "
                f"connected (last status: {state.get('status')!r}).\n"
                f"-> `fm whatsapp pair {device_id}` to keep waiting, or "
                f"`fm whatsapp show {device_id}` to see what the device reports now."
            )
        time.sleep(PAIR_POLL_INTERVAL_SECONDS)
        state = _payload(
            api.admin_get(ctx.target, f"{device_path(device_id)}/pairing", token=ctx.token)
        )
        last_qr = _show_pairing(state, last_qr)

    print(f"connected. device={device_id}")
    if args.json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


def _run_delete(ctx, device_id: str, args) -> int:
    device = _payload(api.admin_get(ctx.target, device_path(device_id), token=ctx.token))
    phone = device.get("phone_e164") or device_id

    if not getattr(args, "yes", False):
        typed = input(
            f"This PERMANENTLY deletes WhatsApp device {device_id} ({phone}). "
            f"Type the phone number to confirm: "
        ).strip()
        if typed != phone:
            raise WhatsAppError(
                f"typed {typed!r} does not match {phone!r}, refusing.\n"
                "-> Re-run and type the exact phone number, or pass --yes to skip this prompt."
            )

    payload = _payload(api.admin_delete(ctx.target, device_path(device_id), token=ctx.token))
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
          else f"deleted device {device_id} ({phone}): ok={payload.get('ok')}")
    return 0


def _payload(payload: object) -> dict:
    return _shared_as_dict(payload, what="/admin/whatsapp", error=WhatsAppError)
