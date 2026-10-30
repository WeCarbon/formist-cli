"""``fm connectors`` — what a space's connectors are authorized to do.

The space-side half of the ACCESS panel, over ``GET /admin/spaces/{id}/connectors/
grants``. ``fm users grants <id>`` is the person-side half; both read the same view
(``services/grants.py``) and share one renderer in :mod:`formist_cli.cli_support`, so the
two commands cannot come to disagree about what a column means.

WHAT THIS COMMAND CAN GET WRONG, AND WHAT STOPS IT
----------------------------------------------------
The same defect ``fm users`` names: a report that is true about what it fetched and
false about what exists. Two shapes of it live here specifically.

**An empty scope column.** Several connector types record no scopes at all — an
API-key connector has no scope concept. Printing an empty cell for both "records
none" and "granted none" would say a Fireflies key may do nothing, when in fact it
may do everything that API allows. The shared renderer prints ``-`` for the first
and a count for the second.

**An expiry that is always about to lapse.** ``expires_at`` on a Google or Airtable
connector is the ~1h ACCESS token, which the server refreshes silently. Rendered as
a deadline it would show every healthy connector as expiring within the hour,
permanently. It is labelled, never bare.

⚠️ THIS IS THE SPACE'S OWN CONNECTORS ONLY. A member's personal (USER-scoped)
connector belongs to the person and has no ``project_id``; it is reported by
``fm users grants``. An empty table here therefore does not mean nobody in the space
has connected anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from formist_cli import api, context
from formist_cli.cli_support import as_dict as _shared_as_dict
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.cli_support import render_grant_rows

SUBJECTS = ("list", "grants")
DEFAULT_SUBJECT = "list"


class ConnectorsError(Exception):
    """A refusal about the connector surface, written for an operator."""


#: The refusal an unknown subject gets. Prose, not a bare ValueError: the reader is
#: an operator at a terminal, and "unknown subcommand" without the valid list is a
#: dead end.
def subject_error(subject: object) -> ConnectorsError:
    return ConnectorsError(
        f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
        "  list    the space's connectors (id, kind, status) — the default\n"
        "  grants  every connector of a space: identity, scopes, status, expiry"
    )


# ===========================================================================
# Rendering
# ===========================================================================
def render_grants(payload: dict) -> str:
    """One space's connectors, named so the reader knows which space they read.

    The heading is not decoration: this command takes a UUID, and a table with no
    space name on it is impossible to check against the space you meant.
    """
    space = payload.get("space") or {}
    items = payload.get("items") or []
    heading = (
        f"{space.get('name') or '(unnamed space)'} "
        f"[{space.get('region') or '?'}] {space.get('id') or ''}"
    ).strip()
    return f"{heading}\n\n{render_grant_rows(items)}"


# ===========================================================================
# CLI — the PARSER lives in evtool/cli.py
# ===========================================================================
# ⚠️ Deliberately NOT an `add_parser` module like evtool/users.py. `fm connectors`
# already existed as an inline command, and registering a second parser under the
# same name raises `ValueError: conflicting subparser` at import — measured, not
# feared. So this module supplies the SUBJECT vocabulary and the grants behaviour,
# and cli.py keeps owning the one `connectors` parser. One command, two subjects.

COMMAND_DESCRIPTION = """A space's connectors, and what they are authorized to do.

  ev connectors [--space X]            the connector list (id, kind, status)
  ev connectors grants [<space-id>]    identity, scopes, auth status, expiry

⚠️ SPACE-OWNED CONNECTORS ONLY. A member's PERSONAL connector is user-scoped, has no
project, and is not listed here — use `fm users grants <user-id>` for those. An empty
table means the space owns no connectors, NOT that nobody has connected anything.

⚠️ THE EXPIRY COLUMN IS LABELLED, AND THE LABEL IS THE POINT. `access token,
auto-renewed` is the ~1h token the server refreshes on its own — it is always minutes
from expiring and that is normal, not a problem to chase. `(not recorded)` means no
expiry is stored for that credential, which is NOT the same as "it never expires".

⚠️ A `-` IN THE SCOPES COLUMN MEANS THE PROVIDER RECORDS NO SCOPES (an API-key
connector), not that the credential was granted nothing.

This command never writes. There is no flag that makes it write.
"""


def run_grants(args, eff) -> int:
    """`fm connectors grants` — the space's connector grants.

    Takes the already-resolved context from cli.py rather than resolving again: the
    resolver PRINTS a provenance banner, and resolving twice would print it twice
    and invite the reader to believe two different targets were consulted.
    """
    space = getattr(eff, "space", None)
    if not space:
        raise ConnectorsError(
            "`fm connectors grants` needs a space id.\n"
            "→ pass it positionally, with --space, or set one with `fm use`."
        )
    payload = api.admin_get(
        eff.target, f"/admin/spaces/{space}/connectors/grants", token=eff.token
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        # ⚠️ The shared helper is called INLINE rather than wrapped in a local
        # `_as_dict`. evtool/users.py has such a wrapper, and adding a second one
        # here was flagged by tests/test_no_cross_module_drift.py — correctly: the
        # baseline file records that this exact helper was already SHARED into
        # cli_support once, and re-wrapping it per module is how it would drift
        # back apart.
        print(
            render_grants(
                _shared_as_dict(
                    payload,
                    what="/admin/spaces/{id}/connectors/grants",
                    error=ConnectorsError,
                )
            )
        )
    return 0


# ===========================================================================
# ``fm connector`` (SINGULAR) — per-connector operator ACTIONS
# ===========================================================================
# ⚠️ A DELIBERATELY SEPARATE TOP-LEVEL COMMAND FROM ``fm connectors`` (plural)
# above. That one is the ADMIN surface (``/admin/spaces/{id}/connectors...`` —
# Global-only, platform-admin-gated). These two hit ordinary PROJECT routes
# (``/projects/{id}/connections/{cid}/reauth``,
# ``/projects/{id}/connectors/{cid}/sync`` — OWNER/ADMIN member-gated, never
# ``/admin``), which is the SAME reason ``fm entity-fields`` is its own
# top-level command rather than a subject of ``fm connectors``
# (``evtool/cli.py::cmd_entity_fields``'s own docstring). Folding a
# project-scoped write into the admin listing command would blur a
# distinction this codebase treats as load-bearing (the project rules: "the
# server is the trust boundary").
#
# Absorbs (EV-ABSORB.md §3.1, both "no new endpoint"):
#   the retired device_code_auth.py     -> `fm connector reauth <cid>`             (fully absorbed, deleted)
#   the retired sync_calendar.py        -> `fm connector sync <cid> --kind calendar` (fully absorbed, deleted)
#   the retired sync_messages.py        -> `fm connector sync <cid> --kind messages` (fully absorbed, deleted)
#   scripts/sync.py                    -> `fm connector sync <cid>`  for the plain one-shot trigger ONLY.
#
# ⚠️ scripts/sync.py is only PARTIALLY absorbed and is deliberately KEPT, not
# deleted. Its `--full` flag clears the connection's stored delta cursor
# (`delta_link`/`delta_cursor`) as "the ONLY recovery path for a truncated
# Drive first crawl" (its own docstring) — and there is no route that can do
# that: `POST /connectors/{cid}/sync` (routes/connectors.py::sync_connection)
# always runs the engine as-is, and `Connection` is not a kind the generic
# `POST /admin/.../data-changes` bulk endpoint can reach (no `TargetKind.
# CONNECTION` — models/Proposal.py). Absorbing `--full` needs a NEW endpoint,
# which is out of this "no new endpoint" batch; see the lane report.
#
# Both subjects are WRITES with real external side effects (a live Microsoft
# OAuth flow; a real Graph/Attendee crawl) and follow ``ev``'s write
# convention: DRY RUN by default, ``--apply`` to actually do it.

CONNECTOR_SUBJECTS = ("reauth", "sync")

#: How often to poll `.../reauth/poll` while waiting for the operator to enter
#: the device code, and for how long before giving up. The server's own flow
#: response carries an `interval`/`expires_in` (Microsoft's own device-code
#: timing) — these are only the CLI's fallback when the server sends neither.
DEFAULT_POLL_INTERVAL = 5
DEFAULT_POLL_TIMEOUT = 900


def _connector_subject_error(subject: object) -> ConnectorsError:
    return ConnectorsError(
        f"unknown subcommand {subject!r}. Valid: {', '.join(CONNECTOR_SUBJECTS)}.\n"
        "  reauth <cid>   re-authorize an existing OUTLOOK connection (device-code, WRITE)\n"
        "  sync   <cid>   trigger a real sync for one connector NOW (WRITE)"
    )


CONNECTOR_COMMAND_DESCRIPTION = """Per-connector operator actions (project-scoped WRITEs).

  fm connector reauth <cid> --space <id> [--apply]
      Re-authorize an existing OUTLOOK connection whose delegated token has
      died (the same flow the product's "Reconnect" banner drives). Prints the
      user_code + verification_uri, then polls until Microsoft answers.

  fm connector sync <cid> --space <id> [--kind ...] [--apply]
      Trigger a real sync for ONE connector now (the scheduler's per-connection
      unit of work, on demand). --kind is accepted for your own bookkeeping —
      the server dispatches by the connector's OWN type, so it is never sent.

Both are WRITEs with real external side effects. DRY RUN by default; --apply
performs the action.
"""


def add_connector_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "connector",
        help="per-connector operator actions: reauth, sync (both WRITE)",
        description=CONNECTOR_COMMAND_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_connector_arguments(parser)
    return parser


def _add_connector_arguments(parser: argparse.ArgumentParser) -> None:
    from formist_cli.cli import _add_env, _add_space

    _add_env(parser, for_write=True)
    _add_space(parser)
    parser.add_argument("subject", help=f"one of: {', '.join(CONNECTOR_SUBJECTS)}")
    parser.add_argument("arg", nargs="?", default=None, help="the connection/connector id (UUID)")
    parser.add_argument(
        "--provider", default="outlook",
        help="reauth: informational only — today only OUTLOOK supports device-code reauth",
    )
    parser.add_argument(
        "--kind", default=None, choices=("calendar", "messages", "sharepoint", "drive"),
        help="sync: informational only (matches the retired scripts' names) — never sent to the server",
    )
    parser.add_argument("--apply", action="store_true", help="perform the action (default: dry run)")
    parser.add_argument(
        "--poll-interval", dest="poll_interval", type=int, default=DEFAULT_POLL_INTERVAL,
        help="reauth: seconds between polls when the server sends none",
    )
    parser.add_argument(
        "--poll-timeout", dest="poll_timeout", type=int, default=DEFAULT_POLL_TIMEOUT,
        help="reauth: give up after this many seconds",
    )
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def build_connector_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_connector_parser)


def _require_connection_id(arg: str | None) -> str:
    if not arg:
        raise ConnectorsError(
            "needs a connection id.\n→ fm connector reauth|sync <cid> --space <id>"
        )
    return arg


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------
def _run_reauth(ctx, args) -> int:
    cid = _require_connection_id(args.arg)
    if not args.apply:
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would initiate a device-code re-authorization for connection {cid} "
            f"in space {ctx.space} ({ctx.target.name}).\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    initiated = api.post(
        ctx.target, f"/projects/{ctx.space}/connections/{cid}/reauth", {}, token=ctx.token
    )
    if not isinstance(initiated, dict):
        raise ConnectorsError(
            f"expected a JSON object from .../reauth, got {type(initiated).__name__}."
        )
    flow = initiated.get("device_code_flow") or {}
    print("=" * 60)
    print(f"DEVICE CODE — authorize the connection at {ctx.space}")
    print(f"  URL : {flow.get('verification_uri')}")
    print(f"  CODE: {flow.get('user_code')}")
    print(f"  (expires in {flow.get('expires_in', DEFAULT_POLL_TIMEOUT)}s)")
    print("=" * 60)

    interval = flow.get("interval") or args.poll_interval
    deadline = time.monotonic() + int(flow.get("expires_in") or args.poll_timeout)
    poll_path = f"/projects/{ctx.space}/connections/{cid}/reauth/poll"
    result: dict = {"status": "pending"}
    first = True
    while time.monotonic() < deadline:
        if not first:
            time.sleep(max(0, interval))
        first = False
        result = api.post(ctx.target, poll_path, {}, token=ctx.token)
        if not isinstance(result, dict):
            raise ConnectorsError(
                f"expected a JSON object from .../reauth/poll, got {type(result).__name__}."
            )
        if result.get("status") != "pending":
            break

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"status={result.get('status')}  auth_status={result.get('auth_status')}")
    if result.get("status") not in ("connected",):
        if result.get("error"):
            print(f"FAILED: {result['error']}")
        else:
            print("did not complete (still pending — the operator ran out of time to authorize)")
        return 2
    return 0


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def _run_sync(ctx, args) -> int:
    cid = _require_connection_id(args.arg)
    kind_note = f" (kind={args.kind})" if args.kind else ""
    if not args.apply:
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would trigger a sync now for connector {cid}{kind_note} "
            f"in space {ctx.space} ({ctx.target.name}).\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    # `--kind` is never sent — one endpoint dispatches by the connector's OWN
    # type (routes/connectors.py::sync_connection), so there is no server-side
    # parameter to send it to; it exists purely so an operator can invoke this
    # the way the retired per-purpose scripts were named.
    result = api.post(ctx.target, f"/projects/{ctx.space}/connectors/{cid}/sync", {}, token=ctx.token)
    if not isinstance(result, dict):
        raise ConnectorsError(
            f"expected a JSON object from .../connectors/{{cid}}/sync, got {type(result).__name__}."
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for key, value in result.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                print(f"{key:<20} {value}")
        for key, value in result.items():
            if isinstance(value, list):
                print(f"{key:<20} {len(value)} item(s)")
    return 0


def run_connector(args) -> int:
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in CONNECTOR_SUBJECTS:
        raise _connector_subject_error(getattr(args, "subject", None))
    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)
    if subject == "reauth":
        return _run_reauth(ctx, args)
    return _run_sync(ctx, args)
