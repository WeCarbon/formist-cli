"""Which `fm` subcommand needs what: the privilege audit, as data.

`fm` must be fully usable with an ordinary user's login and must not do
anything that oversteps that user's privilege. "Usable" and "bounded" are
only compatible if it is written down which commands are which, so this
table exists, and a test fails if a subcommand is added without an entry.
A new command cannot be shipped unclassified.

This table is not a security control and must never become one. The
server is the trust boundary: it decides, on every single call, both
whether the account is a platform admin and whether this credential was
granted the admin scope. What the table buys is a better failure: without
it, running ``fm users`` on a normal login produces a 403 phrased as an
HTTP status after a network round-trip. With it, `fm` says which
privilege is missing and how to get it, before opening a socket, and it
says so in a way that does not suggest a workaround, because there isn't
one.

The check is advisory in one direction only, and that is deliberate. It
refuses early only when the stored credential positively records that it
lacks the admin scope (``fm login`` writes the granted scopes beside the
token). A credential with no recorded scopes (an older file, a
hand-placed one) is passed through to the server rather than guessed
about. A local check that can say "no" to something the server would
have allowed is the failure mode this codebase keeps finding: the thing
being checked stops being the thing being used.

Levels:

  ``LEVEL_LOCAL``   touches no API at all (``fm gate``, ``fm heads``, ``fm use``).
  ``LEVEL_PUBLIC``  an unauthenticated endpoint (``fm health`` without ``--admin``).
  ``LEVEL_USER``    the caller's own permissions: project membership and role decide
              it server-side. This is what an ordinary login is for, and it is the
              majority of the tool.
  ``LEVEL_ADMIN``   needs ``User.is_platform_admin`` and the ``admin`` scope on the
              credential. Two shapes reach it, and both are gated by the same
              server-side check: the ``/admin/*`` surface, and a handful of
              project-scoped paths under ``/projects/{space}/admin/...`` that end
              at the same platform-admin check, which is why a space owner role
              neither grants nor withholds it.
  ``LEVEL_MIXED``   subcommands differ, or one flag crosses the line (``fm health
              --admin``). Never refused early: the table cannot see which
              subcommand is about to run, so refusing would refuse the user-level
              half as well.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The OAuth scopes this client asks for, and the ``software_id`` it
#: registers itself under.
#:
#: Duplicated as plain strings rather than imported, and that is a rule,
#: not laziness: this CLI is stdlib-only by design and must run under a
#: bare system ``python3`` with no venv, including at deploy time, where
#: the server's dependency tree is not present. A test asserts each
#: string equals the server's own constant, which is what makes the
#: duplication safe rather than a promise.
SCOPE_KB = "evane:kb"
SCOPE_CLI = "evane:cli"
SCOPE_ADMIN = "evane:admin"
CLI_SOFTWARE_ID = "formist-cli"

#: Prefixed, and not for tidiness: a bare ``LEVEL_LOCAL`` / ``LEVEL_USER`` at a
#: module's top level is a name any module might bind to mean something
#: else entirely, and a name that means two things in one codebase is one
#: refactor away from someone importing the wrong one. The prefix costs
#: five characters and removes the ambiguity instead of declaring it.
LEVEL_LOCAL = "local"
LEVEL_PUBLIC = "public"
LEVEL_USER = "user"
LEVEL_ADMIN = "admin"
LEVEL_MIXED = "mixed"


@dataclass(frozen=True)
class Privilege:
    level: str
    #: The endpoint(s) this command calls, as they appear in the source. Prose is
    #: allowed for the non-HTTP ones: the point is that a reader can check the
    #: claim, not that a machine can parse it.
    endpoints: tuple[str, ...]
    note: str = ""


#: Keys must equal ``formist_cli.cli._COMMANDS``' keys exactly, asserted
#: in both directions: a removed command must lose its row too, or the
#: table starts describing a tool that no longer exists.
TABLE: dict[str, Privilege] = {
    # --- credentials ---------------------------------------------------------
    "login": Privilege(
        LEVEL_PUBLIC,
        (
            "GET /.well-known/oauth-authorization-server",
            "POST /register",
            "GET /authorize",
            "POST /token",
        ),
        "the OAuth 2.1 flow the MCP connector uses; unauthenticated by "
        "construction, it is how a credential is obtained",
    ),
    "logout": Privilege(
        LEVEL_USER, ("POST /auth/apps/revoke",), "revokes only the caller's own grant"
    ),
    "whoami": Privilege(
        LEVEL_USER, ("GET /auth/apps (only with --remote)",), "reads the local file"
    ),
    # --- read-only diagnostics ------------------------------------------------
    "health": Privilege(
        LEVEL_MIXED, ("GET /health", "GET /admin/health (only with --admin)"),
        "public without --admin",
    ),
    "instance": Privilege(LEVEL_ADMIN, ("GET /admin/instance",)),
    "doctor": Privilege(
        LEVEL_LOCAL, ("GET /health",), "local config checks plus the public health probe"
    ),
    "use": Privilege(LEVEL_LOCAL, (), "writes the remembered space to the config dir"),
    # --- the user's own spaces -------------------------------------------------
    "get": Privilege(LEVEL_USER, ("GET /projects/{space}/...",)),
    "chats": Privilege(
        LEVEL_USER,
        (
            "GET /projects/{space}/chats",
            "GET /projects/{space}/chats/{id}",
            "GET /projects/{space}/chats/{id}/messages",
        ),
    ),
    "chat": Privilege(LEVEL_USER, ("POST /projects/{space}/chats",)),
    "export": Privilege(
        LEVEL_USER,
        (
            "GET /projects/{space}/export",
            "POST /projects/{space}/export/bundle",
            "GET /projects/{space}/export/bundle/{file_id}",
        ),
    ),
    "import": Privilege(LEVEL_USER, ("POST /projects/{space}/import",)),
    "upload": Privilege(
        LEVEL_USER,
        (
            "POST /projects/{space}/uploads/sessions",
            "GET/DELETE /projects/{space}/uploads/sessions/{id}",
            "GET /projects/{space}/files/{id}",
        ),
    ),
    "skill-attach": Privilege(
        LEVEL_USER, ("POST /projects/{space}/uploads/sessions", "GET /projects/{space}/skills/{id}")
    ),
    "entity-fields": Privilege(LEVEL_USER, ("GET /projects/{space}/entity-fields",)),
    "diff": Privilege(LEVEL_USER, ("the export surface",), "compares two exports"),
    # --- platform administration ------------------------------------------------
    "spaces": Privilege(
        LEVEL_ADMIN,
        (
            "GET /admin/spaces",
            "GET/PATCH/DELETE /admin/spaces/{space}",
            "GET /admin/spaces/{space}/members|invitations|skills|conversations|usage",
            "POST /admin/spaces/{space}/transfer-ownership",
        ),
    ),
    "users": Privilege(
        LEVEL_ADMIN,
        (
            "GET/POST /admin/users",
            "GET/PATCH /admin/users/{id}",
            "GET /admin/users/{id}/spaces",
            "DELETE /admin/users/{id}/spaces/{space}",
            "GET /admin/users/{id}/grants",
        ),
    ),
    "proposals": Privilege(LEVEL_ADMIN, ("GET /admin/spaces/{space}/proposals",)),
    "audit-log": Privilege(LEVEL_ADMIN, ("GET /admin/spaces/{space}/audit-log",)),
    "connectors": Privilege(
        LEVEL_ADMIN,
        ("GET /admin/spaces/{space}/connectors", "GET /admin/spaces/{space}/connectors/grants"),
    ),
    "logs": Privilege(
        LEVEL_ADMIN, ("GET /admin/events", "GET /admin/events/{subject}"),
        "also shells out to `az` for Log Analytics, which has its own identity",
    ),
    "space-plan": Privilege(LEVEL_ADMIN, ("GET/PUT /admin/spaces/{space}/plan",)),
    "usage-tokens": Privilege(LEVEL_ADMIN, ("GET /admin/usage/tokens",)),
    "alerts": Privilege(LEVEL_ADMIN, ("GET /admin/alerts", "GET /admin/alerts/conditions",)),
    "messages": Privilege(
        LEVEL_ADMIN,
        ("GET /admin/messages/dedupe-scan", "POST /admin/messages/dedupe"),
    ),
    "settings": Privilege(LEVEL_ADMIN, ("GET/PUT /admin/settings",)),
    "skill-library-install": Privilege(
        LEVEL_USER,
        (
            "GET /projects/{space}/skill-library/packages",
            "GET /projects/{space}/skills",
            "POST /projects/{space}/skill-library/packages/{id}/install",
        ),
        "member-facing (require_member/require_writer): unlike skill-library-sync, "
        "which writes the GLOBAL admin-only catalog, this installs one already-"
        "synced package into the caller's own space, the same route the gallery's "
        "own 'Install' button uses",
    ),
    # These three are NOT under /admin, and that is exactly why they are easy to
    # misfile. They are project-scoped paths gated by the same platform-admin
    # check as everything above.
    "update": Privilege(
        LEVEL_ADMIN, ("POST /projects/{space}/admin/data-changes",),
        "bulk data changes: site-wide admin, not a space role",
    ),
    "airtable": Privilege(
        LEVEL_ADMIN, ("POST /projects/{space}/admin/airtable/direction",),
    ),
    "whatsapp": Privilege(
        LEVEL_ADMIN,
        (
            "GET/POST /admin/whatsapp/devices",
            "GET /admin/whatsapp/devices/{id}",
            "POST /admin/whatsapp/devices/{id}/{pair,start,stop,logout,mark-account-deleted}",
            "GET /admin/whatsapp/devices/{id}/pairing",
            "GET /admin/whatsapp/devices/{id}/groups",
            "DELETE /admin/whatsapp/devices/{id}",
            "GET /admin/whatsapp/runner",
            "POST /admin/whatsapp/runner/{start,stop}",
        ),
        "platform-owned WhatsApp devices; every write is dry-run unless --apply",
    ),
    "kb": Privilege(
        LEVEL_USER,
        ("GET /projects/{space}/search", "GET /projects/{space}/files/{id}/content"),
    ),
    "meeting": Privilege(
        LEVEL_USER,
        (
            "GET /projects/{space}/meetings",
            "GET /projects/{space}/meetings/{task}/transcript",
            "POST /projects/{space}/meetings/join-now (only with --apply)",
        ),
    ),
    "files": Privilege(
        LEVEL_MIXED,
        (
            "POST /projects/{space}/files/reevaluate (only with --apply)",
            "POST /projects/{space}/files/{id}/reevaluate (only with --apply)",
            "GET/POST /projects/{space}/admin/files/merge",
            "GET /projects/{space}/files, GET /projects/{space}/files/{id}/download (download)",
        ),
        "MIXED because `fm files` carries three subcommands at different "
        "levels: `reevaluate` and `download` are ordinary member reads/writes, "
        "`merge` is platform-admin because hard-deleting the dropped row is "
        "irreversible",
    ),
    "project": Privilege(
        LEVEL_USER, ("POST /projects/{space}/logo (only with --apply)",)
    ),
    "connector": Privilege(
        LEVEL_USER,
        (
            "POST /projects/{space}/connections/{id}/reauth (only with --apply)",
            "GET /projects/{space}/connections/{id}/reauth/poll",
            "POST /projects/{space}/connectors/{id}/sync (only with --apply)",
        ),
        "the caller's own space connections; the server's project ACL decides",
    ),
    "participants": Privilege(
        LEVEL_ADMIN,
        ("GET/POST /projects/{space}/admin/participants/dedupe",),
        "the duplicate-people merge. Project-scoped but platform-admin gated: "
        "participants are region-local data, so the surface cannot live "
        "under a bare /admin path",
    ),
    "skills-add": Privilege(
        LEVEL_USER,
        ("POST /projects/{space}/skills/import",),
        "creates a skill from a SKILL.md, through require_writer and apply_change "
        "(actor_type=HUMAN), identical to a member typing one into the app",
    ),
    "skills-update": Privilege(
        LEVEL_USER,
        ("PATCH /projects/{space}/skills/{id}/import",),
        "updates an existing skill IN PLACE through the same require_writer gate; "
        "never creates a second row",
    ),
}


class PrivilegeError(Exception):
    """A refusal raised before any request. The message is written for a human."""


def level_for(command: str) -> str | None:
    entry = TABLE.get(command)
    return entry.level if entry else None


def requires_admin(command: str) -> bool:
    return level_for(command) == LEVEL_ADMIN


def check(command: str, scopes: object) -> None:
    """Refuse an LEVEL_ADMIN command whose credential positively lacks the admin scope.

    ``scopes`` is what `fm login` recorded beside the token. ``None``/empty means
    "not recorded" and is passed through, see the module docstring on why this
    check only ever says no to something it KNOWS the server would also refuse.
    """
    if not requires_admin(command):
        return
    if not isinstance(scopes, (list, tuple)) or not scopes:
        return
    if SCOPE_ADMIN in scopes:
        return
    raise PrivilegeError(
        f"`fm {command}` is a platform-admin action and this credential was not "
        f"granted the '{SCOPE_ADMIN}' scope (it has: {', '.join(map(str, scopes))}).\n"
        "This is not something the CLI can work around; the server decides, on "
        "every call. If your account IS a platform admin, run `fm login --admin` "
        "and approve the platform-administration scope on the authorization page. "
        "If it is not, this action is not available to you."
    )
