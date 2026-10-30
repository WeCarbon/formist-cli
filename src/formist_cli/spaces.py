"""``fm spaces`` — every space across every region, and the platform-admin
operations on one space's members/conversations/skills/invitations/plan usage.

``list`` (the default, no subject needed) is the original command: every space,
every region, with member counts. Everything else here is new surface added for
D224 (2026-09-06) — owner's ruling 「所有 admin 功能都要用 fm cli 可用」, all admin
functionality must be reachable from ``ev`` — filling the gaps ``/admin/spaces/*``
had over ``routes/admin.py``.

WHAT THIS COMMAND CAN GET WRONG, AND WHAT STOPS IT
----------------------------------------------------
The same defect ``fm users`` names: a report that is true about what it fetched and
false about what exists. ``list``, ``conversations``, ``skills`` and
``invitations`` are all server-paginated, so every listing here reports SHOWN and
TOTAL together and names the flag that widens the view — the identical contract
``fm users list`` and ``fm audit-log`` already carry.

WRITES (``rename`` / ``delete`` / ``transfer-ownership`` / ``delete-conversation``
/ ``delete-skill`` / ``delete-invitation`` / ``create`` / ``add-member``)
------------------------------------------------------------------------------------
None of these has a server-side dry run — each takes effect immediately, same as
the equivalent button in the admin panel. ``--env`` therefore NEVER inherits from
`fm use` on a write. ``delete`` is IRREVERSIBLE (permanently deletes the space and
every row/blob under it) and requires ``--confirm``, mirroring the server's own
``?confirm=true`` requirement — the flag exists on both sides for the same reason
``fm space-plan``'s revision check does: the client-side refusal is a readable
early exit, the server-side one is what actually holds for a caller that skips
this CLI.

``create`` / ``add-member`` — owner, 2026-09-19: 「建工作区可以走 ev cli」,
workspace creation goes through the ev CLI. Added for standing up a `formist`-deployment
customer workspace end to end (``fm spaces create`` -> ``fm spaces add-member``
per teammate -> ``fm space-plan set``) without ever touching a database by hand.

``create`` is DIFFERENT FROM EVERY OTHER SUBJECT HERE: it calls ``POST /projects``
(the ordinary product route, not ``/admin``) with the CALLER's own token, and the
caller becomes the new space's OWNER — same as clicking "Create workspace" in the
app. It is here rather than in a "projects" module because an operator thinks of
it as one more thing `fm spaces` does to a space, and creating one is the natural
first step before the admin-only subjects below act on it.

``add-member`` wraps the SAME route ``fm users add-space`` does
(``POST /admin/users/{user_id}/spaces``) — it exists as a second door onto that
route because an operator adding someone to a space thinks in emails, not user
ids, and this resolves one to the other first (walking ``GET /admin/users``,
which has no server-side email filter — see ``evtool/users.py``'s module doc).
Sends NO email and creates no invitation row; it is an immediate membership
grant, verified by reading ``routes/admin.py::assign_user_to_space``.
"""

from __future__ import annotations

import argparse
import json

from formist_cli import api, context
from formist_cli.cli_support import PLATFORM_ADMIN_GATE_NOTE, pagination_footer
from formist_cli.cli_support import as_dict as _shared_space_payload
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser
from formist_cli.targets import require_space_id
from formist_cli.users import render_membership_change

SUBJECTS = (
    "list", "members", "conversations", "delete-conversation",
    "skills", "delete-skill", "invitations", "delete-invitation",
    "usage", "rename", "delete", "transfer-ownership",
    "create", "add-member",
)
DEFAULT_SUBJECT = "list"

#: Every project-role string the underlying routes accept
#: (``services.rbac.parse_project_role`` -> ``models.ProjectUserRel.ProjectRole``).
#: Checked here too so a typo'd ``--role`` is a readable local refusal instead of
#: a 400 from the server — same reasoning as ``fm space-plan``'s client-side
#: revision check (module doc: "the client's copy gives a readable refusal
#: without a write ever being attempted").
_ROLES = ("OWNER", "ADMIN", "USER", "VIEWER")

#: The MUTATING subjects — resolved with ``for_write=True``, same rule as ``ev
#: users``. Deliberately NOT named ``_WRITE_SUBJECTS`` like ``evtool/users.py``'s
#: identical-shaped set: two same-named module constants with different content
#: is exactly the ``diverged:const`` shape ``tests/test_no_cross_module_drift.py``
#: flags, on the first drift scan that saw both.
_MUTATING_SUBJECTS = frozenset({
    "delete-conversation", "delete-skill", "delete-invitation",
    "rename", "delete", "transfer-ownership", "create", "add-member",
})

#: (subject, server field name) for the three delete-a-child-object subjects,
#: shared so the argparse help, the dispatch and the error messages cannot list
#: three different names for the same id.
_CHILD_LABEL = {
    "delete-conversation": "chat_id",
    "delete-skill": "skill_id",
    "delete-invitation": "invitation_id",
}


class SpacesError(Exception):
    """A refusal about the space surface, written for an operator."""


def canonical_subject(subject: str | None) -> str:
    if subject is None:
        return DEFAULT_SUBJECT
    value = subject.strip().lower()
    if value not in SUBJECTS:
        raise SpacesError(
            f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}.\n"
            "  list                every space, every region (paginated) — the default\n"
            "  members             a space's members and roles\n"
            "  conversations       a space's chat sessions (paginated)\n"
            "  delete-conversation delete one conversation + its messages\n"
            "  skills              a space's skills (paginated)\n"
            "  delete-skill        delete (soft) one skill\n"
            "  invitations         a space's pending/decided invitations (paginated)\n"
            "  delete-invitation   revoke a still-PENDING invitation\n"
            "  usage               subscription + token usage for this space\n"
            "  rename              set the space's name\n"
            "  delete              PERMANENTLY delete the space and everything under it\n"
            "  transfer-ownership  move OWNER to a different existing member\n"
            "  create              create a new workspace; you become its OWNER\n"
            "  add-member          assign an existing platform user (by email) to a space"
        )
    return value


# ===========================================================================
# Rendering — reads
# ===========================================================================
def render_space_list(payload: dict) -> str:
    """Every space, never overstating how much of the platform it saw."""
    items = payload.get("items") or []
    total = payload.get("total")
    offset = int(payload.get("offset") or 0)

    lines = [
        f"{'region':<7} {'members':<8} {'name':<32} id",
        f"{'-' * 7} {'-' * 8} {'-' * 32} {'-' * 8}",
    ]
    for row in items:
        lines.append(
            f"{str(row.get('region') or '?'):<7} "
            f"{row.get('member_count', '?'):<8} "
            f"{str(row.get('name') or '-'):<32.32} "
            f"{row.get('id')}"
        )

    lines.append("\n" + pagination_footer(shown=len(items), total=total, offset=offset))
    return "\n".join(lines)


def render_members(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return "(this space has no members)"
    lines = [
        f"{'email':<34} {'name':<20} {'role':<8} user_id",
        f"{'-' * 34} {'-' * 20} {'-' * 8} {'-' * 8}",
    ]
    for row in items:
        lines.append(
            f"{str(row.get('email') or '-'):<34.34} "
            f"{str(row.get('name') or '-'):<20.20} "
            f"{str(row.get('role') or '-'):<8} "
            f"{row.get('user_id')}"
        )
    return "\n".join(lines)


def _paginated(payload: dict, rows: list[str], *, header: list[str], noun: str) -> str:
    """Shared body for the three paginated per-space listings — one place that
    can misreport SHOWN vs TOTAL, not three."""
    items = payload.get("items") or []
    total = payload.get("total")
    offset = int(payload.get("offset") or 0)
    if not items:
        return f"(this space has no {noun})"
    lines = list(header) + rows
    lines.append("\n" + pagination_footer(shown=len(items), total=total, offset=offset))
    return "\n".join(lines)


def render_conversations(payload: dict) -> str:
    items = payload.get("items") or []
    rows = [
        f"{str(c.get('title') or '(untitled)'):<40.40} "
        f"{str(c.get('model') or '-'):<20} "
        f"{('archived' if c.get('archived') else ''):<8} "
        f"{c.get('id')}"
        for c in items
    ]
    header = [
        f"{'title':<40} {'model':<20} {'':<8} id",
        f"{'-' * 40} {'-' * 20} {'-' * 8} {'-' * 8}",
    ]
    return _paginated(payload, rows, header=header, noun="conversations")


def render_skills(payload: dict) -> str:
    items = payload.get("items") or []
    rows = [
        f"{str(s.get('name') or '-'):<30.30} "
        f"{('enabled' if s.get('enabled') else 'disabled'):<8} "
        f"{s.get('id')}"
        for s in items
    ]
    header = [f"{'name':<30} {'':<8} id", f"{'-' * 30} {'-' * 8} {'-' * 8}"]
    return _paginated(payload, rows, header=header, noun="skills")


def render_invitations(payload: dict) -> str:
    items = payload.get("items") or []
    rows = [
        f"{str(i.get('email') or '-'):<34.34} "
        f"{str(i.get('role') or '-'):<8} "
        f"{str(i.get('status') or '-'):<10} "
        f"{i.get('id')}"
        for i in items
    ]
    header = [
        f"{'email':<34} {'role':<8} {'status':<10} id",
        f"{'-' * 34} {'-' * 8} {'-' * 10} {'-' * 8}",
    ]
    return _paginated(payload, rows, header=header, noun="invitations")


def render_usage(payload: dict) -> str:
    """Subscription + agent-token usage, and — alongside it, not instead of it —
    the space's OWN billing-cycle spend over the per-call meter. The two
    disagree on purpose: `tokens_used_this_period` is what gets invoiced,
    `current_cycle` is every LLM call including work an agent run never saw."""
    lines = [
        f"plan      {payload.get('plan') or '(none)'}   status={payload.get('status')}",
        f"seats     {payload.get('seats')}",
        f"period    ends {payload.get('current_period_end') or '?'}",
        f"invoiced  {payload.get('tokens_used_this_period')} / "
        f"{payload.get('included_tokens')} included tokens "
        f"(overage {payload.get('overage_tokens')})",
    ]
    cycle = payload.get("current_cycle") or {}
    if cycle:
        lines.append("current_cycle (this space's own billing_reset_day, ALL LLM calls):")
        for key in sorted(cycle):
            lines.append(f"  {key:<28} {cycle[key]}")
    return "\n".join(lines)


# ===========================================================================
# Rendering — writes
# ===========================================================================
def render_rename(payload: dict) -> str:
    return f"renamed  {payload.get('id')} -> {payload.get('name')!r}"


def render_delete(payload: dict) -> str:
    return (
        f"DELETED  {payload.get('deleted')}\n"
        f"  rows   {payload.get('rows')}\n"
        f"  blobs  {payload.get('blobs')}"
    )


def render_transfer(payload: dict) -> str:
    return (
        f"transferred space={payload.get('space_id')}\n"
        f"  {payload.get('old_owner_user_id')} -> {payload.get('new_owner_user_id')} (OWNER)\n"
        f"  outgoing owner demoted to {payload.get('old_owner_demoted_to') or '(unchanged — no prior owner rel)'}"
    )


def render_child_delete(payload: dict, *, noun: str) -> str:
    return f"deleted {noun} {payload.get('deleted')}"


def render_created(payload: dict) -> str:
    """``POST /projects``'s response — see :func:`_project_dict` on the server
    for the full shape; only what an operator needs to act next is printed."""
    return (
        f"created  {payload.get('id')}\n"
        f"  name    {payload.get('name')!r}\n"
        f"  region  {payload.get('region')}\n"
        f"  role    {payload.get('role')}   (you)"
    )


# ===========================================================================
# CLI
# ===========================================================================
_HELP = (
    "every space (admin); members/conversations/skills/invitations/usage; "
    "rename/delete/transfer/create/add-member"
)

_DESCRIPTION = """Platform-admin view of spaces, across every region.

  fm spaces                                        every space (paginated) — default
  fm spaces members <space-id>
  fm spaces conversations <space-id> [--limit --offset]
  fm spaces delete-conversation <space-id> <chat-id>
  fm spaces skills <space-id> [--limit --offset]
  fm spaces delete-skill <space-id> <skill-id>
  fm spaces invitations <space-id> [--limit --offset]
  fm spaces delete-invitation <space-id> <invitation-id>
  fm spaces usage <space-id>
  fm spaces rename <space-id> --name 'New Name'
  fm spaces delete <space-id> --confirm                 # IRREVERSIBLE
  fm spaces transfer-ownership <space-id> --new-owner <user-id>
  fm spaces create --name 'New Workspace' --country ae
  fm spaces add-member --space <space-id> --user <email> [--role USER]

⚠️ THERE IS NO SERVER-SIDE SEARCH on `list` — it takes limit/offset only, same as
`fm users`. An empty page means "not on this page", not "no such space".

⚠️ `delete` IS PERMANENT — it deletes every row and blob under the space (the same
cascade the owner's own project-delete uses) and requires --confirm on top of the
server's own ?confirm=true. There is no dry run and no undo.

⚠️ `create` calls `POST /projects` — the ORDINARY product route, with YOUR OWN
token, not the admin API. You become the new space's OWNER. `--country` is
REQUIRED (owner, 2026-10-11: forgetting it must not pass the server — this used
to be possible with `--region` alone, which left a space stuck with no country,
since country is immutable and can never be set after the fact). The server
INFERS the datacenter from the country per product; `--region` is optional and
only checked for agreement — it cannot pick a datacenter by itself.

⚠️ `add-member` resolves --user's EMAIL to a user id via `GET /admin/users`
(admin-only, no server-side email filter — a full walk of the platform's users)
and then assigns them with `POST /admin/users/{id}/spaces`, same as `fm users
add-space` — this is a second door onto that one route, addressed by email
because that is what an operator has on hand. Sends NO invitation email; the
membership is immediate. --role is OWNER/ADMIN/USER/VIEWER (default USER),
case-insensitive.

THE `formist` ENV: `--host formist` is its OWN deployment (`evtool/targets.py::
REGION_FORMIST`), not a region of the default product — use it for every one of these commands
against a workspace on that deployment, never `--host evane`.
"""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "subject", nargs="?",
        help=f"one of: {', '.join(SUBJECTS)} (default: {DEFAULT_SUBJECT})",
    )
    parser.add_argument("space_id", nargs="?", help="space UUID (never a name)")
    parser.add_argument(
        "child_id", nargs="?",
        help="chat/skill/invitation UUID (for the three delete-* subjects)",
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
    parser.add_argument("--name", default=None, help="`rename`/`create`: the (new) name")
    parser.add_argument(
        "--country", default=None,
        help="`create`: REQUIRED — the ISO 3166-1 alpha-2 country/territory the "
             "space is for (e.g. 'ae'). Decides the datacenter per product "
             "(services/space_policy.py::infer_datacenter). Immutable once created",
    )
    parser.add_argument(
        "--region", default=None,
        help="`create`: optional — must AGREE with the datacenter --country "
             "infers for this product, or the server refuses with "
             "region-conflict. Cannot select a datacenter on its own",
    )
    parser.add_argument(
        "--new-owner", dest="new_owner", default=None,
        help="`transfer-ownership`: the new owner's user UUID (must already be a member)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="`delete`: required to actually delete — there is no dry run",
    )
    parser.add_argument(
        "--space", default=None,
        help="`add-member`: the space UUID (an alternative to the positional "
             "space-id argument; never a name)",
    )
    parser.add_argument(
        "--user", default=None,
        help="`add-member`: the platform user's EMAIL (resolved to an id via "
             "`GET /admin/users` — never pass a user id here)",
    )
    parser.add_argument(
        "--role", default="USER",
        help=f"`add-member`: one of {', '.join(_ROLES)} (default: USER), "
             "case-insensitive",
    )


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "spaces", help=_HELP, description=_DESCRIPTION, epilog=PLATFORM_ADMIN_GATE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def _require_space(args) -> str:
    space_id = getattr(args, "space_id", None)
    if not space_id:
        raise SpacesError(
            "this subcommand needs a space id.\n"
            "→ `fm spaces` prints them (id is the last column)."
        )
    return require_space_id(space_id)


def run(args) -> int:
    subject = canonical_subject(getattr(args, "subject", None))
    if subject in _MUTATING_SUBJECTS:
        return _run_write(subject, args)

    ctx = context.resolve(args, need_token=True)

    if subject == "list":
        params: dict[str, str] = {}
        if getattr(args, "limit", None) is not None:
            params["limit"] = str(args.limit)
        if getattr(args, "offset", None) is not None:
            params["offset"] = str(args.offset)
        payload = api.admin_get(ctx.target, "/admin/spaces", token=ctx.token, params=params)
        print(json.dumps(payload, indent=2) if args.json else render_space_list(_space_payload(payload)))
        return 0

    space = _require_space(args)

    if subject == "members":
        payload = api.admin_get(
            ctx.target, f"/admin/spaces/{space}/members", token=ctx.token
        )
        print(json.dumps(payload, indent=2) if args.json else render_members(_space_payload(payload)))
        return 0

    if subject == "usage":
        payload = api.admin_get(ctx.target, f"/admin/spaces/{space}/usage", token=ctx.token)
        print(json.dumps(payload, indent=2) if args.json else render_usage(_space_payload(payload)))
        return 0

    params = {}
    if getattr(args, "limit", None) is not None:
        params["limit"] = str(args.limit)
    if getattr(args, "offset", None) is not None:
        params["offset"] = str(args.offset)

    if subject == "conversations":
        payload = api.admin_get(
            ctx.target, f"/admin/spaces/{space}/conversations", token=ctx.token, params=params
        )
        print(json.dumps(payload, indent=2) if args.json else
              render_conversations(_space_payload(payload)))
        return 0

    if subject == "skills":
        payload = api.admin_get(
            ctx.target, f"/admin/spaces/{space}/skills", token=ctx.token, params=params
        )
        print(json.dumps(payload, indent=2) if args.json else render_skills(_space_payload(payload)))
        return 0

    # invitations
    payload = api.admin_get(
        ctx.target, f"/admin/spaces/{space}/invitations", token=ctx.token, params=params
    )
    print(json.dumps(payload, indent=2) if args.json else render_invitations(_space_payload(payload)))
    return 0


def _run_write(subject: str, args) -> int:
    ctx = context.resolve(args, for_write=True, need_token=True)

    # `create` has no EXISTING space to require — it makes one — and
    # `add-member` addresses its space via `--space` (an alternative to the
    # positional `space_id` the other subjects use, so it reads naturally next
    # to `--user`/`--role`). Both are dispatched before the generic
    # `_require_space` below, which every other mutating subject still uses.
    if subject == "create":
        return _create_space(ctx, args)
    if subject == "add-member":
        return _add_member(ctx, args)

    space = _require_space(args)

    if subject == "rename":
        name = (getattr(args, "name", None) or "").strip()
        if not name:
            raise SpacesError("`fm spaces rename` needs --name <text>")
        payload = api.admin_patch(
            ctx.target, f"/admin/spaces/{space}", {"name": name}, token=ctx.token
        )
        payload = _space_payload(payload)
        print(json.dumps(payload, indent=2) if args.json else render_rename(payload))
        return 0

    if subject == "delete":
        if not getattr(args, "confirm", False):
            raise SpacesError(
                "REFUSING — this PERMANENTLY deletes the space and every row/blob "
                "under it, with no dry run and no undo.\n"
                "→ Re-run with --confirm once you are certain."
            )
        payload = api.admin_delete(
            ctx.target, f"/admin/spaces/{space}", token=ctx.token,
            params={"confirm": "true"},
        )
        payload = _space_payload(payload)
        print(json.dumps(payload, indent=2) if args.json else render_delete(payload))
        return 0

    if subject == "transfer-ownership":
        new_owner = getattr(args, "new_owner", None)
        if not new_owner:
            raise SpacesError(
                "`fm spaces transfer-ownership` needs --new-owner <user-uuid>.\n"
                "→ `fm spaces members <space-id>` lists candidates — the new owner "
                "must already be a member."
            )
        payload = api.admin_post(
            ctx.target, f"/admin/spaces/{space}/transfer-ownership",
            {"new_owner_user_id": new_owner}, token=ctx.token,
        )
        payload = _space_payload(payload)
        print(json.dumps(payload, indent=2) if args.json else render_transfer(payload))
        return 0

    # the three delete-<child> subjects
    child_id = getattr(args, "child_id", None)
    child_label = _CHILD_LABEL[subject]
    if not child_id:
        raise SpacesError(f"`fm spaces {subject}` needs a {child_label} as the third argument")
    child_path = {
        "delete-conversation": "conversations",
        "delete-skill": "skills",
        "delete-invitation": "invitations",
    }[subject]
    noun = {
        "delete-conversation": "conversation",
        "delete-skill": "skill",
        "delete-invitation": "invitation",
    }[subject]
    payload = api.admin_delete(
        ctx.target, f"/admin/spaces/{space}/{child_path}/{child_id}", token=ctx.token
    )
    payload = _space_payload(payload)
    print(json.dumps(payload, indent=2) if args.json else
          render_child_delete(payload, noun=noun))
    return 0


def _create_space(ctx, args) -> int:
    """``POST /projects`` — the ordinary product route, with the caller's OWN
    token, not ``/admin``. See the module docstring for why `create` lives here
    rather than being `/admin`-gated like everything else in this file.

    `--country` is REQUIRED and refused client-side before any network call —
    the server would 422 `country-required` anyway, but a caller script should
    not need a round trip to learn that (see the module docstring for why)."""
    name = (getattr(args, "name", None) or "").strip()
    if not name:
        raise SpacesError("`fm spaces create` needs --name <text>")
    country = (getattr(args, "country", None) or "").strip()
    if not country:
        raise SpacesError("`fm spaces create` needs --country <ISO alpha-2> (e.g. ae, us)")
    body: dict = {"name": name, "country": country}
    region = (getattr(args, "region", None) or "").strip()
    if region:
        body["region"] = region
    payload = _space_payload(api.post(ctx.target, "/projects", body, token=ctx.token))
    print(json.dumps(payload, indent=2) if args.json else render_created(payload))
    return 0


def _add_member(ctx, args) -> int:
    """Resolve ``--user``'s email to a user id, then
    ``POST /admin/users/{user_id}/spaces`` — the SAME route ``fm users
    add-space`` calls (see the module docstring for why this is a second door
    onto it rather than a new one)."""
    space = getattr(args, "space", None) or getattr(args, "space_id", None)
    if not space:
        raise SpacesError(
            "`fm spaces add-member` needs a space id: --space <uuid>, or as the "
            "second argument.\n"
            "→ `fm spaces` prints them (id is the last column)."
        )
    space = require_space_id(space)

    email = (getattr(args, "user", None) or "").strip()
    if not email:
        raise SpacesError("`fm spaces add-member` needs --user <email>")

    role = (getattr(args, "role", None) or "USER").strip().upper()
    if role not in _ROLES:
        raise SpacesError(
            f"unknown --role {getattr(args, 'role', None)!r}. Valid: {', '.join(_ROLES)}."
        )

    user_id = _find_user_id_by_email(ctx, email)
    body = {"space_id": space, "role": role}
    payload = _space_payload(
        api.admin_post(ctx.target, f"/admin/users/{user_id}/spaces", body, token=ctx.token)
    )
    print(json.dumps(payload, indent=2) if args.json else
          render_membership_change(payload, removed=False))
    return 0


#: Rows per page while walking `GET /admin/users` looking for one email. Large
#: enough that a normal platform (tens to a few hundred users) resolves in one
#: or two requests; the server caps `limit` regardless, so this is a ceiling
#: this client asks for, not one it can force.
_USER_SEARCH_PAGE_SIZE = 200


def _find_user_id_by_email(ctx, email: str) -> str:
    """Walk ``GET /admin/users`` for an exact, case-insensitive email match.

    There is no server-side email filter (``evtool/users.py``'s own module doc:
    "/admin/users accepts limit and offset and nothing else — no q, no email
    filter"), so resolving one email means paging through all of them. Exact
    match only, never a substring — a substring match risks assigning the WRONG
    account to a space, which is a worse failure than a slower, correct one.
    """
    target = email.strip().lower()
    offset = 0
    while True:
        payload = _space_payload(
            api.admin_get(
                ctx.target, "/admin/users", token=ctx.token,
                params={"limit": str(_USER_SEARCH_PAGE_SIZE), "offset": str(offset)},
            )
        )
        items = payload.get("items") or []
        for row in items:
            if str(row.get("email") or "").strip().lower() == target:
                return str(row.get("id"))
        offset += len(items)
        total = payload.get("total")
        if not items or (total is not None and offset >= int(total)):
            break
    raise SpacesError(
        f"no platform user with email {email!r} on {ctx.target.name}.\n"
        f"→ `fm users create --email {email} --host {ctx.target.name}` first, then "
        "re-run this command."
    )


def _space_payload(payload: object) -> dict:
    """:func:`formist_cli.cli_support.as_dict`, bound to this command's error type.

    Deliberately NOT named the same as ``users._as_dict`` would look wrong to
    share — two same-named one-line bindings of the shared body are the
    ``diverged:impl`` shape ``tests/test_no_cross_module_drift.py`` flags. Both are
    one-line bindings of the SAME shared function with a module-specific error
    type, which is what the shared helper exists for.
    """
    return _shared_space_payload(payload, what="/admin/spaces", error=SpacesError)
