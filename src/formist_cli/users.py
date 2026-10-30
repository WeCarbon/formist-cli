"""``fm users`` — platform user rows, over routes that already exist.

Step 1 of retiring ``az containerapp exec``. The operator read ``public.user`` and
``project_user_rel`` through the exec channel, at roughly one call per two minutes
and with a 404-on-oversize failure mode. Both are already served:

  GET /admin/users              every user, paginated
  GET /admin/users/{id}         one user
  GET /admin/users/{id}/spaces  their memberships, with region and role

So this is a CLIENT and not an endpoint, which is why it is first: it retires the
row-read half of the throttle with no server change and therefore no server risk.

WHAT THIS COMMAND CAN GET WRONG, AND WHAT STOPS IT
----------------------------------------------------
Not the HTTP call. The defect available here is **a report that is true about what
it fetched and false about what exists**: ``/admin/users`` is paginated and capped
by ``MAX_LIMIT``, so printing 50 rows without saying there are 312 tells an operator
the platform has 50 users. Every number on screen is real and the conclusion is
wrong — the shape of ``applied_ids``, and of the bulk endpoint's ``changed`` field
before it was fixed.

:func:`render_list` therefore reports SHOWN and TOTAL together, and names the flag
that widens the view. It also stays quiet when nothing was truncated: an alarm that
fires when nothing is wrong is an alarm nobody reads, and then it is not there for
the case that matters.

⚠️ WHAT THE SURFACE CANNOT DO, said in ``--help`` because the operator meets that
and not this file. ``/admin/users`` takes ``limit``/``offset`` and NOTHING ELSE —
there is no ``q``. An empty page therefore does not mean "no such user", and a
``--help`` that stayed silent would invite exactly that reading.

Follows ``routes/admin_instance.py``'s shape deliberately: report what can be
known, and label how far each field can be trusted.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE, pagination_footer, render_grant_rows
from formist_cli.cli_support import as_dict as _shared_as_dict
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.targets import require_space_id

SUBJECTS = (
    "list", "show", "spaces", "grants",
    "create", "set", "add-space", "remove-space",
)
DEFAULT_SUBJECT = "list"

#: WRITE subjects — resolved with ``for_write=True`` (never inherits ``--env``
#: from ``fm use``), same reasoning as every other mutating command in this CLI.
_WRITE_SUBJECTS = frozenset({"create", "set", "add-space", "remove-space"})


class UsersError(Exception):
    """A refusal about the user surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise UsersError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  list         every user on the platform (paginated)\n"
            "  show         one user by id\n"
            "  spaces       a user's space memberships, with region and role\n"
            "  grants       every credential this deployment holds for them (admin)\n"
            "  create       provision a user by email (no password — magic-link/OAuth)\n"
            "  set          update name and/or --platform-admin on/off\n"
            "  add-space    assign (or re-role) a user in a space\n"
            "  remove-space unassign a user from a space"
        )
    return value


# ===========================================================================
# Rendering
# ===========================================================================
def render_list(payload: dict) -> str:
    """A scannable table that never overstates how much of the platform it saw."""
    items = payload.get("items") or []
    total = payload.get("total")
    offset = int(payload.get("offset") or 0)

    lines = [
        f"{'email':<38} {'name':<22} {'admin':<6} {'waiting':<8} id",
        f"{'-' * 38} {'-' * 22} {'-' * 6} {'-' * 8} {'-' * 8}",
    ]
    for row in items:
        lines.append(
            f"{str(row.get('email') or '-'):<38.38} "
            f"{str(row.get('name') or '-'):<22.22} "
            f"{('yes' if row.get('is_platform_admin') else ''):<6} "
            f"{('yes' if row.get('waiting') else ''):<8} "
            f"{_short(row.get('id'))}"
        )

    lines.append("\n" + pagination_footer(shown=len(items), total=total, offset=offset))
    return "\n".join(lines)


def render_user(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def render_spaces(payload: dict) -> str:
    """A user's memberships. Says so when there are none.

    Blank output is indistinguishable from a command that failed quietly, which is
    the single most common way a read tool misleads.
    """
    items = payload.get("items") or []
    if not items:
        return "(this user is a member of no spaces)"
    lines = [
        f"{'space':<38} {'region':<8} {'role':<10} id",
        f"{'-' * 38} {'-' * 8} {'-' * 10} {'-' * 8}",
    ]
    for row in items:
        lines.append(
            f"{str(row.get('name') or '-'):<38.38} "
            f"{str(row.get('region') or '-'):<8} "
            f"{str(row.get('role') or '-'):<10} "
            f"{_short(row.get('space_id'))}"
        )
    return "\n".join(lines)


def render_grants(payload: dict) -> str:
    """Every credential held for one user, plus WHERE we looked.

    ``regions_scanned`` is printed rather than dropped: personal connectors are
    region-local, so "this user has no Google connector" and "we only searched one
    database" would otherwise be the same empty table.
    """
    items = payload.get("items") or []
    regions = payload.get("regions_scanned") or []
    body = render_grant_rows(items)
    return f"{body}\n\nregions scanned: {', '.join(str(r) for r in regions) or '(none)'}"


def render_membership_change(payload: dict, *, removed: bool) -> str:
    """The result of ``add-space`` / ``remove-space`` — an operator-facing line,
    not a raw dict, because the interesting fact (which role, which space) is
    two keys the server already named."""
    if removed:
        return f"removed  user={payload.get('user_id')}  space={payload.get('space_id')}"
    return (
        f"assigned user={payload.get('user_id')}  space={payload.get('space_id')}  "
        f"role={payload.get('role')}"
    )


def _short(value) -> str:
    text = str(value or "-")
    return text[:8] if len(text) > 8 else text


# ===========================================================================
# CLI
# ===========================================================================
_HELP = "platform user rows: list, show one, or list a user's spaces (admin)"

_DESCRIPTION = """Read platform user rows over the admin API.

Replaces the `az containerapp exec` route to `public.user` and `project_user_rel`,
which cost a ~120s-throttled round trip per query and answered 404 when the command
exceeded ~1800 characters.

⚠️ THESE ROWS LIVE IN THE **GLOBAL** DATABASE regardless of which region a space is
in — auth is not per-region. Pointing this at a regional API and seeing nothing
would mean the admin surface is not served there, never that the user is absent.
/admin is Global-only and ev refuses to send it elsewhere before opening a socket.

⚠️ THERE IS NO SERVER-SIDE SEARCH. `/admin/users` accepts limit and offset and
nothing else — no `q`, no email filter. So an empty page means "not on this page",
NOT "no such user". Page with --offset, or pipe --json to a filter.

Every listing reports how many rows it showed AND how many exist, because a page
of 50 printed on its own reads as a platform with 50 users.

WRITES (``create`` / ``set`` / ``add-space`` / ``remove-space``)
------------------------------------------------------------------
These reach the SAME mutating ``/admin/users*`` routes the admin panel uses —
D224 (2026-09-06): 「所有 admin 功能都要用 ev cli 可用」, all admin functionality
must be reachable from ``ev``. Every mutating ``/admin`` route is wrapped in
``AuditedAdminRouter`` (``routes/admin.py``), so each of these writes a
``platform_audit_log`` row on the server regardless of which client called it —
that is a server-side property, not something this CLI has to arrange.

None of the four has a server-side dry run (unlike ``fm space-plan`` / ``ev
i18n-backfill``, which were built with one) — each takes effect immediately, the
same as clicking the equivalent button in the admin panel. ``--env`` therefore
NEVER inherits from ``fm use`` on a write, same rule as everywhere else in this
CLI: a write must be told, not remembered.

⚠️ ``set --platform-admin off`` on your OWN account is refused by the server
(``cannot-self-demote``); revoking the platform's LAST admin is refused too
(``last-platform-admin``) — both errors are printed verbatim, not swallowed.
"""



def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject", nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument(
        "user_id", nargs="?",
        help="user UUID (for `show` / `spaces` / `grants` / `set` / `add-space` / `remove-space`)",
    )
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="deployment name or API origin (default: formist, or `fm use` for reads); "
        "a write never inherits it",
    )
    parser.add_argument("--limit", type=int, default=None, help="rows per page (server caps it)")
    parser.add_argument("--offset", type=int, default=None, help="skip this many rows")
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a table")
    parser.add_argument("--email", default=None, help="`create`: the new user's email")
    parser.add_argument("--name", default=None, help="`create`/`set`: display name")
    parser.add_argument(
        "--platform-admin", dest="platform_admin", choices=("on", "off"), default=None,
        help="`set`: grant/revoke User.is_platform_admin — the highest-blast-radius "
             "flag on this whole surface",
    )
    parser.add_argument(
        "--space", default=None,
        help="`add-space`/`remove-space`: the space UUID (never a name)",
    )
    parser.add_argument(
        "--role", default="USER",
        help="`add-space`: OWNER, ADMIN, USER or VIEWER (default: USER)",
    )


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "users", help=_HELP, description=_DESCRIPTION, epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    """The same parser, detached, so ``--help`` can be asserted in a test.

    See :func:`formist_cli.cli_support.build_standalone_parser` for why the body lives
    there — this stays a one-line, no-argument wrapper so
    ``tests/test_evtool_users.py`` keeps calling ``users.build_standalone_parser()``.
    """
    return _build_standalone_parser(add_parser)


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    if subject in _WRITE_SUBJECTS:
        return _run_write(subject, args)
    # ⚠️ need_token is REDUNDANT for this command and kept on purpose. Every read
    # below goes through api.admin_get, which has its own "no stored session"
    # refusal — so a mutation removing this changes nothing observable, and it
    # SURVIVED the mutation batch for exactly that reason. That is the layered
    # guard GATE.md describes: covered, not dead.
    #
    # It stays because this is one command in a series, and the next ones (ev files,
    # ev cache) reach api.get, which does NOT guard. A resolver that refuses for
    # some commands and not others is the drift this module was extracted to end.
    ctx = context.resolve(args, need_token=True)

    params: dict[str, str] = {}
    if getattr(args, "limit", None) is not None:
        params["limit"] = str(args.limit)
    if getattr(args, "offset", None) is not None:
        params["offset"] = str(args.offset)

    if subject == "list":
        payload = api.admin_get(ctx.target, "/admin/users", token=ctx.token, params=params)
        print(json.dumps(payload, indent=2) if args.json else render_list(_as_dict(payload)))
        return 0

    user_id = getattr(args, "user_id", None)
    if not user_id:
        raise UsersError(
            f"`fm users {subject}` needs a user id.\n"
            "→ `fm users list` prints them (id is the last column)."
        )

    if subject == "show":
        payload = api.admin_get(ctx.target, f"/admin/users/{user_id}", token=ctx.token)
        print(render_user(_as_dict(payload)))
        return 0

    if subject == "grants":
        payload = api.admin_get(
            ctx.target, f"/admin/users/{user_id}/grants", token=ctx.token
        )
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_grants(_as_dict(payload)))
        return 0

    payload = api.admin_get(ctx.target, f"/admin/users/{user_id}/spaces", token=ctx.token)
    print(json.dumps(payload, indent=2) if args.json else render_spaces(_as_dict(payload)))
    return 0


def _run_write(subject: str, args) -> int:
    """The four mutating subjects. Resolved ``for_write=True`` — see the module
    docstring: none of these has a server-side dry run, so ``--env`` must be told
    on every call rather than inherited from ``fm use``."""
    ctx = context.resolve(args, for_write=True, need_token=True)

    if subject == "create":
        email = (getattr(args, "email", None) or "").strip()
        if not email:
            raise UsersError("`fm users create` needs --email <address>")
        body = {"email": email}
        if getattr(args, "name", None):
            body["name"] = args.name
        payload = api.admin_post(ctx.target, "/admin/users", body, token=ctx.token)
        print(json.dumps(payload, indent=2) if args.json else render_user(_as_dict(payload)))
        return 0

    user_id = getattr(args, "user_id", None)
    if not user_id:
        raise UsersError(
            f"`fm users {subject}` needs a user id.\n"
            "→ `fm users list` prints them (id is the last column)."
        )

    if subject == "set":
        fields: dict = {}
        if getattr(args, "name", None):
            fields["name"] = args.name
        if getattr(args, "platform_admin", None) is not None:
            fields["is_platform_admin"] = args.platform_admin == "on"
        if not fields:
            raise UsersError(
                "nothing to set. Name at least one of:\n"
                "  --name <text>\n"
                "  --platform-admin on|off"
            )
        payload = api.admin_patch(
            ctx.target, f"/admin/users/{user_id}", fields, token=ctx.token
        )
        print(json.dumps(payload, indent=2) if args.json else render_user(_as_dict(payload)))
        return 0

    space = getattr(args, "space", None)
    if not space:
        raise UsersError(
            f"`fm users {subject}` needs --space <uuid>.\n"
            "→ `fm spaces --host evane` prints them."
        )
    space = require_space_id(space)

    if subject == "add-space":
        body = {"space_id": space, "role": args.role}
        payload = api.admin_post(
            ctx.target, f"/admin/users/{user_id}/spaces", body, token=ctx.token
        )
        payload = _as_dict(payload)
        print(json.dumps(payload, indent=2) if args.json else
              render_membership_change(payload, removed=False))
        return 0

    # remove-space
    payload = api.admin_delete(
        ctx.target, f"/admin/users/{user_id}/spaces/{space}", token=ctx.token
    )
    payload = _as_dict(payload)
    print(json.dumps(payload, indent=2) if args.json else
          render_membership_change(payload, removed=True))
    return 0


def _as_dict(payload: object) -> dict:
    """:func:`formist_cli.cli_support.as_dict`, bound to this command's error type.

    The body moved to ``cli_support`` when ``fm space-plan`` needed the identical
    check; this stays as the one-line binding so the call sites here are unchanged
    and ``tests/test_evtool_users.py`` keeps asserting on ``users._as_dict``.
    """
    return _shared_as_dict(payload, what="/admin/users", error=UsersError)
