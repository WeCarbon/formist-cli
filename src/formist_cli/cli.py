"""``fm``: the Formist/Evane operator CLI.

Design rules, all of which are safety properties rather than preferences:

- **Local by default, prod named explicitly.** ``--env`` defaults to
  ``local``. There is no state file and no "current environment",
  because a remembered target is one somebody forgets they set.
- **Hosts are allow-listed** (``formist_cli.targets``): an unknown
  host is refused rather than tried.
- **Spaces are addressed by UUID, never by name**, since names are
  not guaranteed unique.
- **Every ``/admin`` mutation is exposed, and every one is audited, by
  two different mechanisms, and that is fine.** ``fm update`` posts to
  ``/projects/{id}/admin/data-changes``, where every op lands in the
  project-scoped ``AuditLog``. The rest of the platform-admin surface
  (``fm users create/set/add-space/remove-space``, ``fm spaces
  rename/delete/transfer-ownership/delete-conversation/delete-skill/
  delete-invitation``) writes to a separate ``PlatformAuditLog``
  instead, because ``AuditLog.project_id`` is a non-nullable FK and a
  platform action (create a user, delete a space) often has no project
  at all.
- **A write never inherits its environment.** ``fm use`` remembers a
  space and, for reads, an environment. A write resolves ``--env``
  from the command line only. A space is 36 characters nobody
  verifies by eye; an environment is one word and decides whether
  this is production.

Stdlib only: the production image builds without dev dependencies, so
this tool must not add a runtime dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from formist_cli import (
    api,
    cli_support,
    context,
    logs,
    prefs,
    privileges,
    skill_library,
    skills,
    space_plan,
    tokens,
    users,
)
from formist_cli import chat as chat_mod
from formist_cli import chats as chats_mod
from formist_cli import connectors as connectors_mod
from formist_cli import diff as diff_mod
from formist_cli import doctor as doctor_mod
from formist_cli import export as export_mod
from formist_cli import files as files_mod
from formist_cli import importer as importer_mod
from formist_cli import kb as kb_mod
from formist_cli import login as login_mod
from formist_cli import meetings as meetings_mod
from formist_cli import messages as messages_mod
from formist_cli import participants as participants_mod
from formist_cli import projects as projects_mod
from formist_cli import resolve as resolve_mod
from formist_cli import spaces as spaces_mod
from formist_cli import update as update_mod
from formist_cli import uploads as uploads_mod
from formist_cli.airtable import AirtableError
from formist_cli.alerts import AlertsError
from formist_cli.chats import ChatsError
from formist_cli.cli_support import as_dict
from formist_cli.diff import DiffError
from formist_cli.export import ExportError
from formist_cli.files import FilesError
from formist_cli.importer import ImportRefused
from formist_cli.kb import KbError
from formist_cli.meetings import MeetingsError
from formist_cli.participants import ParticipantsError
from formist_cli.prefs import PrefsError
from formist_cli.projects import ProjectsError
from formist_cli.resolve import ResolutionError
from formist_cli.settings import SettingsError
from formist_cli.spaces import SpacesError
from formist_cli.targets import (
    DEFAULT_HOST,
    HOSTS,
    TargetError,
    is_admin_env,
    require_space_id,
    resolve,
)
from formist_cli.update import UpdateError
from formist_cli.uploads import UploadError
from formist_cli.usage_tokens import UsageTokensError
from formist_cli.whatsapp import WhatsAppError

_HOST_HELP = (
    f"deployment: {', '.join(HOSTS)}, or an API origin such as "
    f"https://api.example.com or 127.0.0.1:8160 (default: {DEFAULT_HOST})"
)


def _add_env(parser: argparse.ArgumentParser, *, for_write: bool = False) -> None:
    """Add ``--host`` to a subcommand.

    The root parser carries ``--host`` too, so both ``fm --host X cmd`` and
    ``fm cmd --host X`` work. A write never inherits a host remembered by
    ``fm use``; see :mod:`formist_cli.prefs`.
    """
    parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help=_HOST_HELP + ("; a write never inherits it from `fm use`" if for_write else ""),
    )


def _add_space(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--space", help="space UUID (never a name; default from `fm use`)"
    )


def _effective(
    args, *, for_write: bool = False, need_space: bool = True, need_token: bool = False
):
    """Resolve the effective target and print its provenance before anything runs.

    The banner goes to stderr on every command, reads included: the
    target alone tells the operator little, while where each half
    came from, and how stale a remembered one is, is what catches a
    wrong assumption.
    """
    return context.resolve(
        args, for_write=for_write, need_space=need_space, need_token=need_token
    )


def _token_for(eff):
    """The stored token for this target, announcing a cross-region borrow.

    A credential silently sourced from another file is exactly the invisible state
    this tool otherwise refuses, so the borrow is printed.
    """
    # Already resolved and already announced by context.resolve; kept as a named
    # accessor so ten call sites read the same as before.
    return eff.token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm",
        description="Command-line client for Formist (and Evane) deployments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Named hosts: {', '.join(f'{n} ({h.api_base})' for n, h in HOSTS.items())}.\n"
            "Writes run as a dry run first; pass --apply to commit.\n"
        ),
    )
    parser.add_argument("--host", default=None, metavar="HOST", help=_HOST_HELP)
    # ``default=False`` here, not SUPPRESS, so every command always has an
    # `admin` attribute to read with a plain `getattr`/truth test. A subcommand's
    # own `--admin` (added with `cli_support.add_admin_flag`, SUPPRESS'd) never
    # overwrites this back to False when omitted, the same duality `--host` uses.
    parser.add_argument(
        "--admin", action="store_true", default=False, help=cli_support.ADMIN_FLAG_HELP,
    )
    parser.add_argument("--version", action="version", version=f"fm {login_mod.cli_version()}")
    sub = parser.add_subparsers(
        dest="command", required=True, parser_class=cli_support.SubcommandParser
    )

    p = sub.add_parser(
        "login",
        help="sign in through the browser and store a CLI credential (0600)",
        description=(
            "Signs in through the same OAuth 2.1 flow the MCP connector uses, "
            "one authorization server for both. `fm` registers itself as a "
            "public client, opens the authorization page in a browser where "
            "you sign in with your normal login and approve, and exchanges a "
            "one-time code (PKCE S256) for a credential that is scoped to "
            "you, expires, and is revocable on its own from Settings > "
            "Security > Connected apps, beside your other connected apps. "
            "It replaces pasting a browser session JWT, which was the whole "
            "account and could only be killed by signing out everywhere. "
            "There is no --token flag. For CI / an unattended machine, export "
            "FORMIST_TOKEN with the 'token' field of the file this command "
            "writes (0600, printed below); `fm` uses it for that invocation "
            "only, never writes it to disk, and never refreshes it; run "
            "`fm whoami` to confirm which source is in use. "
            "A --host evane or --host evane-ae login runs against the Global "
            "region and the credential works at both of those hosts; "
            "--host formist signs in against its own deployment."
        ),
    )
    _add_env(p)
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser or listen on 127.0.0.1; print the URL and "
             "prompt for the code the page displays (headless boxes, SSH)",
    )
    p.add_argument(
        "--admin",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also request the platform-administration scope. Only granted if your "
             "account already has it; needed for any other command's own --admin",
    )

    p = sub.add_parser(
        "logout",
        help="revoke the stored credential server-side and delete the file",
    )
    _add_env(p)
    p.add_argument("--all", action="store_true", help="remove every environment's token")
    p.add_argument(
        "--local-only",
        action="store_true",
        help="delete the file without calling the server (offline)",
    )

    p = sub.add_parser("whoami", help="describe the stored token (never prints it)")
    _add_env(p)
    p.add_argument(
        "--remote",
        action="store_true",
        help="also list this account's connected apps as the SERVER sees them "
             "(MCP connectors and CLI devices together)",
    )

    p = sub.add_parser("health", help="health checks for a region (public, no auth)")
    _add_env(p)
    p.add_argument(
        "--admin", action="store_true", default=argparse.SUPPRESS,
        help="GET /admin/health instead: this region's checks plus the other "
             "region's, in one call (" + cli_support.ADMIN_FLAG_HELP + ")",
    )

    spaces_mod.add_parser(sub)

    p = sub.add_parser(
        "proposals",
        help=(
            "list a space's AI proposals (member), or unstick the "
            "pre-atomic-approve ones (--admin)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    cli_support.add_admin_flag(p)
    # Positional, defaulted, and NOT a second subparser level: `fm proposals` with
    # no subject has meant "list them" for months and every runbook says so.
    p.add_argument(
        "subject",
        nargs="?",
        default="list",
        choices=PROPOSAL_SUBJECTS,
        help="list (default, member) | unstick (always needs --admin)",
    )
    p.add_argument("--status", help="list: e.g. PENDING")
    p.add_argument("--target-kind", dest="target_kind")
    p.add_argument("--limit", default="50")
    p.add_argument(
        "--id",
        dest="proposal_ids",
        action="append",
        help="unstick: repair only this proposal id (repeatable). "
        "Default is every stranded proposal in the space.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="unstick: commit the write. Without it nothing is written.",
    )

    p = sub.add_parser("audit-log", help="a space's audit log (member; --admin for a space you are not a member of)")
    _add_env(p)
    _add_space(p)
    cli_support.add_admin_flag(p)
    p.add_argument("--limit", default="50")

    p = sub.add_parser(
        "connectors",
        help="a space's connectors (member); `grants` always needs --admin",
        description=connectors_mod.COMMAND_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    cli_support.add_admin_flag(p)
    # Optional and default-preserving: `fm connectors` alone must keep
    # printing the plain list it always printed, since a positional
    # that changed the default behaviour would silently redefine an
    # existing operator command.
    p.add_argument(
        "subject", nargs="?", default=None,
        help=f"one of: {', '.join(connectors_mod.SUBJECTS)} (default: list)",
    )
    p.add_argument(
        "space_id", nargs="?", default=None,
        help="space UUID (an alternative to --space)",
    )
    p.add_argument("--json", action="store_true", help="raw JSON instead of a table")

    p = sub.add_parser(
        "entity-fields",
        help="a space's custom-field schemas (the read whose absence forced a write)",
    )
    _add_env(p)
    _add_space(p)

    skill_library.add_install_parser(sub)
    skills.add_parser(sub)

    p = sub.add_parser(
        "skill",
        help="skill operations: add, update, attach, install-package (all WRITE)",
        description=(
            "Groups the four skill-writing commands under one noun; each is "
            "the exact behaviour of its old top-level name, kept as a "
            "deprecated alias through the next release:\n\n"
            "  fm skill add <source> --space <id> [--apply]                "
            "(was `fm skills-add`)\n"
            "  fm skill update <source> --skill <id|name> --space <id> [--apply]  "
            "(was `fm skills-update`)\n"
            "  fm skill attach <skill> <file> --space <id> [--apply]       "
            "(was `fm skill-attach`)\n"
            "  fm skill install-package <package> --space <id> [--apply]  "
            "(was `fm skill-library-install`)\n\n"
            "Each is DRY RUN by default; run the old command's own --help "
            "(e.g. `fm skills-add --help`) for the full contract, which has "
            "not changed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("subject", help=f"one of: {', '.join(_SKILL_SUBJECTS)}")
    p.add_argument(
        "arg1", nargs="?", default=None,
        help="add/update: source directory or .zip. attach: skill id or exact "
        "name. install-package: the package id",
    )
    p.add_argument(
        "arg2", nargs="?", default=None,
        help="attach only: path to the local file",
    )
    p.add_argument("--skill", default=None, help="update: skill id (UUID) or exact name")
    p.add_argument(
        "--folder-path", default=uploads_mod.DEFAULT_FOLDER_PATH,
        help="attach: folder path recorded alongside the uploaded file",
    )
    p.add_argument("--owner", default=None, help="attach: free-text team label")
    p.add_argument(
        "--file-id", dest="file_id", default=None,
        help="attach: attach this already-uploaded file id instead of "
        "uploading the file again",
    )
    p.add_argument("--apply", action="store_true", help="perform the write (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser(
        "doctor",
        help="readable health rollup across environments",
        description=(
            "Health, migration identity and credential status for every "
            "environment, in one place. Distinguishes 'could not reach it' from "
            "'reached it and it is unhealthy', different facts, different next "
            "actions, and reports every environment even when one is down."
        ),
    )
    p.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        metavar="HOST",
        help="check only this deployment (default: every named, live one)",
    )

    p = sub.add_parser(
        "use",
        help="remember a default space (and, for reads, a host)",
        description=(
            "Remembers defaults so a space UUID need not be retyped. The space is "
            "remembered for everything; the host is remembered for READS "
            "ONLY. A write never inherits it: with one remembered the write is "
            "REFUSED until you pass --host, and with nothing remembered a write "
            "targets the default host (formist). A remembered host is one "
            "somebody forgets they set and the cost of forgetting is a write to "
            "the wrong database. Every command prints "
            "the effective target and where each half came from."
        ),
    )
    _add_env(p)
    _add_space(p)
    p.add_argument("--show", action="store_true", help="print the current defaults")
    p.add_argument("--clear", action="store_true", help="forget everything")

    p = sub.add_parser(
        "get",
        help="find rows by name, and the ids they resolve to",
        description=(
            "The resolver that means a UUID is never transcribed. Prints the id for "
            "a name so it can be checked, and is the same lookup `fm update` does "
            "for $name. Listing shows every candidate; resolving requires exactly "
            "one match and refuses otherwise."
        ),
    )
    _add_env(p)
    _add_space(p)
    p.add_argument(
        "kind",
        help=f"one of: {resolve_mod.kind_names()}  (case-insensitive; - and _ alike)",
    )
    p.add_argument("--name", help="exact name; omit to list the kind")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    chats_mod.add_parser(sub)

    # Replaces `chat_mod.add_parser(sub)`: `chats_mod.add_parser(sub)` above
    # (the plural, kept as a deprecated alias of `list`) still runs
    # separately and is untouched.
    p = sub.add_parser(
        "chat",
        help="a space's chats: `list` them, or `send` into one (WRITE)",
        description=(
            "  fm chat list [chat_id] [--kind ..] [--archived] [--latest] "
            "[--messages] [--all-messages] [--limit N] [--json]   (was `fm chats`)\n"
            "  fm chat send [chat_id] [--model ..] [--effort ..] [--message TEXT] [--json]\n\n"
            "`list` reads (member route, see `fm chats --help`); `send` opens "
            "the interactive REPL, or with --message sends one turn "
            "non-interactively and exits (see the old `fm chat --help`). "
            "Neither behaviour changed, only the names they are reached "
            "under.\n\n"
            "A bare `fm chat [chat_id]` (no subject) still works exactly as "
            "it always did -- equivalent to `fm chat send [chat_id]` -- since "
            "`chat` is not an alias here, it is the same name carrying one "
            "more way to spell the same thing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument(
        "subject_or_id", nargs="?", default=None,
        help="`list` or `send`, or (legacy, no subject) a chat UUID to "
        "continue/show directly",
    )
    p.add_argument(
        "second_arg", nargs="?", default=None,
        help="the chat UUID, when `subject_or_id` is `list` or `send`",
    )
    p.add_argument("--kind", help="list: background-agent | user-chat (default: both)")
    p.add_argument("--archived", action="store_true", help="list: list archived chats")
    p.add_argument(
        "--latest", action="store_true",
        help="list: address the newest matching chat instead of naming an id",
    )
    p.add_argument(
        "--messages", action="store_true", help="list: also print the transcript (one page)"
    )
    p.add_argument(
        "--all-messages", dest="all_messages", action="store_true",
        help="list: page the WHOLE transcript, however long",
    )
    p.add_argument("--limit", type=int, default=50, help="list: page size")
    p.add_argument("--model", default=None, help="send: override the chat's model for this run")
    p.add_argument(
        "--effort", default=None, help="send: low|medium|high|xhigh (ignored by models with no knob)"
    )
    p.add_argument(
        "--message", default=None, metavar="TEXT",
        help="send: send this one message non-interactively and exit when the run finishes",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


    export_mod.add_parser(sub)
    importer_mod.add_parser(sub)

    diff_mod.add_parser(sub)


    participants_mod.add_parser(sub)

    messages_mod.add_parser(sub)

    uploads_mod.add_parser(sub)

    kb_mod.add_parser(sub)
    meetings_mod.add_parser(sub)
    files_mod.add_parser(sub)
    projects_mod.add_parser(sub)
    connectors_mod.add_connector_parser(sub)

    # Moved to curist-server's `fmdev` (evdev renamed), C1105/C1114: these ten
    # had no non-admin meaning in `fm` at all (no caller's-own-data reading,
    # unlike `fm spaces`/`fm health`/etc, which keep their --admin branch
    # right here), so there is nothing here for an external customer to run.
    # `fmdev` builds its own, identical parsers for these by calling
    # `add_instance_parser`/`add_update_parser` and the modules' own
    # `add_parser` directly -- formist_cli stays the one place this shape is
    # defined. `fm` keeps a one-release stub under each name so a script
    # still running the old command gets a clear redirect instead of
    # argparse's bare "invalid choice".
    for name in _MOVED_TO_FMDEV:
        _add_moved_to_fmdev_stub(sub, name)

    return parser


# --- commands -----------------------------------------------------------------


def cmd_login(args) -> int:
    """Browser-confirmed OAuth authorization.

    Deliberately `resolve`, not `_effective`: the credential commands
    bind to the environment named on the command line, never to a
    remembered one. `fm login` picks which file a token lands in, and
    inheriting that from `fm use` is how a prod token ends up in the
    local slot (or the reverse). Same for logout/whoami.
    """
    target = resolve(args.host)
    payload = login_mod.perform_login(
        target,
        no_browser=bool(getattr(args, "no_browser", False)),
        admin=bool(getattr(args, "admin", False)),
    )
    login_mod.store_payload(
        target.name, payload,
        client_id=str(payload["client_id"]),
        client_name=str(payload.get("client_name") or ""),
    )
    meta = tokens.load_meta(target.name)

    who = _describe_account(target, payload)
    print(f"Connected as {who} ({target.name})")
    print(f"  credential stored at {tokens.token_path(target.name)} (0600)")
    print(f"  scopes: {', '.join(meta.get('scopes') or []) or '(none granted)'}")
    if privileges.SCOPE_ADMIN not in (meta.get("scopes") or []):
        # Said out loud rather than discovered at the first 403. The whole point of
        # the default is that it is narrow; a narrow default nobody was told about
        # reads as a bug.
        print(
            "  platform-admin actions are NOT granted to this credential "
            "(`fm login --admin` requests them; only a platform admin is granted them)"
        )
    print("  revoke it from Settings > Security > Connected apps, or `fm logout`")
    return 0


def _describe_account(target, payload: dict) -> str:
    """Who the new credential belongs to, asked of the server.

    The OAuth token response says nothing about the user, it is opaque
    by design, so "Connected as <who>" has to be a question, not a
    claim read off the token. Falls back to the client name when
    /auth/me cannot be reached: printing a wrong identity would be
    worse than printing none.
    """
    try:
        me = api.get(login_mod.auth_target(target), "/auth/me",
                     token=str(payload["access_token"]))
    except (api.ApiError, TargetError, OSError):
        return str(payload.get("client_name") or "this account")
    if isinstance(me, dict):
        return str(me.get("email") or me.get("id") or "this account")
    return "this account"


def cmd_logout(args) -> int:
    """Revoke the grant server-side, then delete the file.

    In that order, and both halves matter. Deleting only the file
    leaves a live credential in the world that nobody can see any
    more; revoking only on the server leaves a dead file that `fm`
    will keep presenting. The local delete happens even when the
    revoke fails (an offline laptop must still be able to forget its
    credential), and the failure is printed, since "logged out" while
    the credential is still live would be a quiet wrong answer.
    """
    if args.all:
        removed = tokens.clear_all()
        print(f"removed: {', '.join(removed)}" if removed else "no stored tokens")
        if removed:
            print(
                "  note: --all deletes the local files only. Revoke the grants "
                "themselves from Settings > Security > Connected apps."
            )
        return 0

    target = resolve(args.host)
    token = tokens.load(target.name)
    if not token:
        print(f"no stored token for {target.name}")
        return 0

    revoked = False
    if not getattr(args, "local_only", False):
        revoked = login_mod.revoke(target, token, _own_grant_id(target, token))
    tokens.clear(target.name)
    if revoked:
        print(f"revoked the {target.name} grant server-side and removed the file")
        return 0
    if getattr(args, "local_only", False):
        print(f"removed the {target.name} token file (--local-only: NOT revoked)")
        return 0
    print(
        f"removed the {target.name} token file, but the server did NOT confirm the "
        "revocation; the grant may still be live. Revoke it from "
        "Settings > Security > Connected apps.",
        file=sys.stderr,
    )
    return 1


def _own_grant_id(target, token: str) -> str | None:
    """The grant id of the credential we are holding, asked of the server.

    An OAuth access token is opaque: there is nothing in it to read a
    grant id out of, which is the property that makes it revocable in
    the first place. So `fm logout` asks: `GET /auth/apps` returns
    this user's grants and the CLI matches the one whose client_id it
    stored. A stored grant id would be the alternative and it would go
    stale on the first refresh.
    """
    client_id = tokens.load_meta(target.name).get("client_id")
    if not client_id:
        return None
    try:
        apps = login_mod.list_apps(target, token)
    except (api.ApiError, TargetError, OSError):
        return None
    for app in apps:
        if app.get("client_id") == client_id and not app.get("revoked"):
            return str(app.get("grant_id") or "") or None
    return None


def cmd_whoami(args) -> int:
    target = resolve(args.host)
    token, origin = tokens.load_for(target.name)
    if not token:
        print(f"no stored token for {target.name}. Run fm login --host {target.name}")
        return 1

    if origin == tokens.ENV_TOKEN_ORIGIN:
        print(f"{target.name}: token source = environment ({tokens.ENV_TOKEN_VAR})")
        print(
            "  not written to disk and not auto-refreshed by `fm`; scopes and "
            "expiry are whatever the issuer granted; the server decides on every "
            "call"
        )
    else:
        meta = tokens.load_meta(target.name)
        print(f"{target.name}: {meta.get('client_name') or 'fm'}")
        print(f"  token source: {tokens.token_path(target.name)}")
        if meta.get("scopes"):
            print(f"  scopes: {', '.join(meta['scopes'])}")
        if meta.get("expires_at"):
            print(f"  access token expires: {meta['expires_at']} (refreshed automatically)")
    print("  the token is OPAQUE: `fm` cannot read anything out of it; the server decides")

    if getattr(args, "remote", False):
        for app in login_mod.list_apps(target, token):
            state = (
                "revoked" if app.get("revoked")
                else "expired" if app.get("expired")
                else "active"
            )
            mark = "*" if app.get("client_id") == meta.get("client_id") else " "
            kind = "cli" if app.get("is_cli") else "app"
            admin = "  [ADMIN]" if app.get("grants_admin") else ""
            print(
                f"  {mark} [{kind}] {app.get('client_name')}  {state}  "
                f"last used {app.get('last_used_at') or 'never'}{admin}"
            )
    return 0


def _print_health_block(label: str, payload: dict) -> bool:
    """One region's health block. Returns True iff a FATAL check failed."""
    print(f"{label}: {payload.get('status')}  ({payload.get('checked_at', '')})")
    for check in payload.get("checks", []):
        flag = "!" if check.get("status") != "ok" else " "
        fatal = " [fatal]" if check.get("fatal") else ""
        print(f"  {flag} {check.get('name'):<22} {check.get('status'):<6}"
              f" {check.get('detail', '')}{fatal}")
    bad = [c for c in payload.get("checks", []) if c.get("status") != "ok"]
    return any(c.get("fatal") for c in bad)


def cmd_health(args) -> int:
    if getattr(args, "admin", False):
        # /admin/health is a DIFFERENT endpoint from the public /health above: it
        # is platform-admin-gated and reports THIS region PLUS a best-effort read
        # of the OTHER region's public /health, in one call.
        eff = _effective(args, need_space=False, need_token=True)
        payload = api.admin_get(eff.target, "/admin/health", token=_token_for(eff))
        if not isinstance(payload, dict):
            print(payload)
            return 0
        regions = payload.get("regions") or {}
        worst = False
        for name, data in regions.items():
            worst = _print_health_block(name, data if isinstance(data, dict) else {}) or worst
        return 1 if worst else 0

    eff = _effective(args, need_space=False)
    target = eff.target
    payload = api.get(target, "/health")
    if not isinstance(payload, dict):
        print(payload)
        return 0
    return 1 if _print_health_block(target.name, payload) else 0


def add_instance_parser(sub) -> None:
    """Public (no leading underscore) because `fm` no longer registers this
    itself -- `instance` is one of the ten commands that moved to
    curist-server's `fmdev` (C1105/C1114), which calls this directly to
    build the exact same parser `fm instance` used to have, and then
    :func:`cmd_instance`, unchanged, to run it. Kept here rather than moved
    out entirely so evdev/fmdev, like every other `fm`-reused command,
    stays a thin caller of formist_cli rather than a second copy of it.
    """
    p = sub.add_parser("instance", help="build + migration identity (Tier 1 endpoint)")
    _add_env(p)
    cli_support.add_admin_flag(p)


def cmd_instance(args) -> int:
    eff = _effective(args, need_space=False)
    target = eff.target
    payload = api.admin_get(target, "/admin/instance", token=_token_for(eff))
    if not isinstance(payload, dict):
        print(payload)
        return 0
    worst = 0
    for name, data in (payload.get("regions") or {}).items():
        alembic = data.get("alembic") or {}
        print(f"[{name}] scope={data.get('scope')}")
        if data.get("scope") == "process":
            print(f"  db_role={data.get('db_role')}  scheduler={data.get('scheduler')}"
                  f"  build_marker={data.get('build_marker')}")
        print(f"  alembic db_head={alembic.get('db_head')}")
        if "code_heads" in alembic:
            print(f"  alembic code_heads={alembic.get('code_heads')}")
            if alembic.get("in_sync") is False:
                print("  db_head != code_heads: a migration is pending or failed.")
                worst = 1
    return worst


#: `fm proposals <subject>`.
PROPOSAL_SUBJECTS = ("list", "unstick")


def cmd_proposals(args) -> int:
    subject = getattr(args, "subject", None) or "list"
    admin = bool(getattr(args, "admin", False))
    if subject == "unstick":
        privileges.require_admin_flag("fm proposals unstick", admin)
        return _proposals_unstick(args)
    eff = _effective(args)
    target, space = eff.target, eff.space
    params = {"limit": args.limit, "status": args.status, "target_kind": args.target_kind}
    # Same shared list contract on both sides (PROPOSAL_SPEC): the admin route
    # reads any space cross-region, platform-admin only; the member route reads
    # the caller's own space only, and needs no flag.
    path = (
        f"/admin/spaces/{space}/proposals" if admin else f"/projects/{space}/proposals"
    )
    fetch = api.admin_get if admin else api.get
    payload = fetch(target, path, token=_token_for(eff), params=params)
    for item in _items(payload):
        print(f"{item.get('id')}  {item.get('status'):<10} {item.get('target_kind'):<14}"
              f" {item.get('created_at', '')}")
    return 0


def _proposals_unstick(args) -> int:
    """Survey (and with --apply, repair) proposals an earlier approve stranded.

    The dry run always runs, including under --apply, and it is
    printed before anything is written. The whole value of this
    command is that the operator reads which proposals are
    half-applied before deciding; a repair whose scope is only visible
    afterwards is a repair nobody reviewed.

    Half-applied proposals are called out separately from merely-failed ones
    because only the first needs a decision: its applied items cannot be undone,
    so approving finishes the job and rejecting leaves those rows changed. The
    command does not choose between those; a person does.
    """
    eff = _effective(args)
    target, space = eff.target, eff.space
    path = f"/admin/spaces/{space}/proposals/unstick"
    body: dict = {"dry_run": True}
    if getattr(args, "proposal_ids", None):
        body["proposal_ids"] = list(args.proposal_ids)

    dry = as_dict(
        api.admin_post(target, path, body, token=_token_for(eff)),
        what="unstick survey",
        error=TargetError,
    )
    _render_unstick(dry)
    if not dry.get("total"):
        return 0
    if not args.apply:
        ids = "".join(f" --id {i}" for i in (body.get("proposal_ids") or []))
        print()
        print("NOTHING WAS WRITTEN. To apply exactly this:")
        print(f"  fm proposals unstick --space {space}{ids} --apply")
        return 0

    applied = as_dict(
        api.admin_post(target, path, {**body, "dry_run": False}, token=_token_for(eff)),
        what="unstick result",
        error=TargetError,
    )
    _render_unstick(applied)
    print()
    print(
        f"APPLIED: reopened {applied.get('reopened_targets')} failed item(s) "
        f"across {applied.get('watermarks_written')} proposal(s). "
        "Approve or reject them from the space as usual."
    )
    return 0


def _render_unstick(payload: dict) -> None:
    rows = payload.get("proposals") or []
    mode = "DRY RUN" if payload.get("dry_run") else "APPLIED"
    print(
        f"{mode}: {payload.get('total', 0)} stranded proposal(s), "
        f"{payload.get('half_applied', 0)} of them HALF-APPLIED"
    )
    for row in rows:
        flag = "HALF-APPLIED" if row.get("half_applied") else "failed-items"
        print(
            f"  {row.get('id')}  {row.get('status'):<9} {flag:<12} "
            f"applied={row.get('applied')}/{row.get('target_count')} "
            f"failed={row.get('failed')} reopen={row.get('reopened_targets')}"
        )
        for actor in row.get("blocked_actors") or []:
            print(f"      blocked: {actor} (can neither approve nor reject)")
        for reason in row.get("failure_reasons") or []:
            print(f"      why: {reason}")


#: The route's default sort is `created_at` ascending, so a space with
#: more rows than `--limit` would otherwise show its oldest page and
#: nothing else, which is a page nobody asked for: "what changed
#: recently" is the only question this command is ever asked.
_AUDIT_LOG_SORT = "-created_at"


def cmd_audit_log(args) -> int:
    """A space's audit log, newest first, saying so when it did not show all of it.

    Both halves are load-bearing and neither is decoration:

    * The sort, because ascending order answers a question nobody has.
    * The count line, because the server caps `--limit`, so asking for
      more than the cap and receiving fewer rows is not an error and
      prints no warning of its own. A listing that shows a page while
      looking like it showed the whole set would be true about what it
      fetched and false about what exists.
    """
    admin = bool(getattr(args, "admin", False))
    eff = _effective(args)
    target, space = eff.target, eff.space
    # Same shared list contract (AUDIT_LOG_SPEC) either way: the member route
    # reads only the caller's own space and needs no flag; --admin reads a
    # space this credential is not a member of, platform-admin only.
    path = (
        f"/admin/spaces/{space}/audit-log" if admin else f"/projects/{space}/audit-log"
    )
    fetch = api.admin_get if admin else api.get
    payload = fetch(
        target, path,
        token=_token_for(eff),
        params={"limit": args.limit, "sort": _AUDIT_LOG_SORT},
    )
    items = _items(payload)
    for item in items:
        print(f"{item.get('created_at', '')}  {item.get('actor_type', ''):<6}"
              f" {item.get('action', ''):<8} {item.get('target_kind', '')}")
    total = payload.get("total") if isinstance(payload, dict) else None
    shown = len(items)
    if total is None:
        # The route always sends `total`; if it ever stops, say so rather than
        # printing a count that looks authoritative.
        print(f"\n{shown} shown (newest first), total UNKNOWN "
              f"(the server sent no `total`)")
    elif shown < int(total):
        print(f"\n{shown} of {total} shown, NEWEST FIRST, {int(total) - shown} "
              f"older rows not listed. Raise --limit to reach further back.")
    else:
        print(f"\n{shown} of {total} (newest first)")
    return 0


def cmd_connectors(args, *, label: str = "fm connectors grants") -> int:
    """A space's connectors, what each one's last sync run actually did, or
    `grants` for what they may do.

    The default subject is the original listing, unchanged: adding a
    subject that displaced it would break every existing invocation,
    and the breakage would look like a connector problem rather than a
    CLI change.

    The second line per connector is the whole point of the command
    for an operator diagnosing cadence: `trigger` separates the two
    clocks a connector runs on (`scheduler` means the periodic poll
    produced this run, `push` means a provider change notification
    did), and `dur` is how long the run held the tick. The tick is
    strictly serial, so a large number here on one connector explains
    every other connector running late.

    ``label`` names this call in the --admin refusal message: this is also
    reached from `fm connector list`/`fm connector grants` (:func:`cmd_connector`),
    which should name itself, not the deprecated `fm connectors` alias.
    """
    subject = (getattr(args, "subject", None) or connectors_mod.DEFAULT_SUBJECT).strip().lower()
    if subject not in connectors_mod.SUBJECTS:
        raise connectors_mod.subject_error(getattr(args, "subject", None))
    if getattr(args, "space_id", None) and not getattr(args, "space", None):
        args.space = args.space_id
    admin = bool(getattr(args, "admin", False))
    if subject == "grants":
        # No member-level equivalent: the grant view (consented scopes, auth
        # status, expiry) is platform-admin only, always.
        privileges.require_admin_flag(label, admin)
        return connectors_mod.run_grants(args, _effective(args))
    eff = _effective(args)
    target, space = eff.target, eff.space
    # Same card shape either way (`_connection_dict`): the member route reads
    # only the caller's own space's connections; --admin reads any space.
    path = (
        f"/admin/spaces/{space}/connectors" if admin else f"/projects/{space}/connections"
    )
    fetch = api.admin_get if admin else api.get
    payload = fetch(target, path, token=_token_for(eff))
    for item in _items(payload):
        # `type`, not `kind`: the payload has never had a `kind` key, so an
        # operator could not otherwise tell one connector type from another in
        # the output of the command whose whole job is telling them apart.
        # `label` is the registry's human name for the same thing.
        kind = item.get("type") or item.get("kind") or "?"
        print(f"{item.get('id')}  {str(kind).lower():<16} {item.get('status', '?')}")
        print(
            f"    last_synced={item.get('last_synced_at') or '-'}"
            f"  next_sync={item.get('next_sync_at') or '-'}"
            f"  every={item.get('sync_interval_seconds', '?')}s"
        )
        run = item.get("sync_run")
        if not isinstance(run, dict):
            # Distinguished from a run that failed: this connector has never run at
            # all, which for a linked connector is itself the finding.
            print("    last run: (never)")
            continue
        dur = run.get("duration_seconds")
        # A live run has no finished_at, so its duration is elapsed-so-far; saying
        # which it is costs one character and stops "202s" being read as final.
        elapsed = "-" if dur is None else (
            f"{dur}s" if run.get("finished_at") else f"{dur}s so far"
        )
        print(
            f"    last run: {run.get('state', '?'):<10} trigger={run.get('trigger', '?'):<9}"
            f" dur={elapsed}"
            f"  items={run.get('items_processed', '?')}/{run.get('items_seen', '?')}"
            f" failed={run.get('items_failed', '?')}"
        )
        if run.get("error"):
            print(f"      error: {run['error']}")
    return 0


#: Every subject `fm connector` answers to, now that `fm connectors`' two
#: (list, grants) are folded in alongside its own (reauth, sync). Checked
#: here, once, rather than in two different modules that would each only see
#: half the vocabulary.
_CONNECTOR_ALL_SUBJECTS = connectors_mod.SUBJECTS + connectors_mod.CONNECTOR_SUBJECTS


def cmd_connector(args) -> int:
    """`fm connector list|grants|reauth|sync` -- the noun-group command
    `fm connectors` (list/grants) and `fm connector` (reauth/sync) are now
    folded into. Routes to the two ORIGINAL, unchanged handlers rather than
    re-implementing either: `list`/`grants` reuse :func:`cmd_connectors`
    (the `space_id`-vs-connection-id positional is the same reuse-the-generic-
    slot trick `fm spaces`/`fm whatsapp` already use, documented on
    ``arg``'s own help text), `reauth`/`sync` reuse
    `connectors_mod.run_connector` exactly as `fm connector` always has.
    """
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in _CONNECTOR_ALL_SUBJECTS:
        raise connectors_mod.connector_subject_error(getattr(args, "subject", None))
    if subject in connectors_mod.SUBJECTS:
        if getattr(args, "arg", None) and not getattr(args, "space_id", None):
            args.space_id = args.arg
        return cmd_connectors(args, label="fm connector grants")
    return connectors_mod.run_connector(args)


def cmd_entity_fields(args) -> int:
    """A space's custom-field schemas.

    This is a project route, not an admin one (``GET
    /projects/{id}/entity-fields``): being able to inspect the field
    shape directly means an operator never has to write to
    ``project.settings`` by hand to work around not seeing it.

    The gate is membership, not platform-admin, so this needs a
    session belonging to a member of the space. That is the normal
    case for the operator and is why no admin-scoped endpoint was
    added for it.
    """
    eff = _effective(args)
    target, space = eff.target, eff.space
    payload = api.get(
        target,
        f"/projects/{space}/entity-fields",
        token=_token_for(eff),
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict) or not items:
        print("(no custom entity fields configured for this space)")
        return 0
    for kind, fields in sorted(items.items()):
        print(f"{kind}  ({len(fields)} field{'s' if len(fields) != 1 else ''})")
        for field in fields:
            if isinstance(field, dict):
                print(f"    {field.get('name', '?'):<24} {field.get('type', '?')}")
    return 0


def cmd_doctor(args) -> int:
    """Health across environments, reported honestly.

    Never aborts on the first failure: one region being unreachable is exactly when
    the state of the others matters most. The exit code is the WORST status seen,
    not the first.
    """
    host = getattr(args, "host", None)
    envs = [prefs.canonical_env(host)] if host else list(HOSTS)

    reports = []
    for env in envs:
        try:
            target = resolve(env)
        except TargetError as exc:
            reports.append(doctor_mod.EnvReport.unreachable(str(env), str(exc)))
            continue

        try:
            report = doctor_mod.EnvReport.from_health(
                env, api.get(target, "/health", timeout=10)
            )
        except api.ApiError as exc:
            # One line, because a rollup with a five-line paragraph in it is not a
            # rollup. The per-env command still gives the full explanation.
            reports.append(
                doctor_mod.EnvReport.unreachable(
                    str(env), str(exc).splitlines()[0]
                )
            )
            continue

        report = _with_credential(report, target.name)
        reports.append(_with_migrations(report, target, target.env))

    print(f"fm doctor: {len(reports)} deployment(s)")
    print(doctor_mod.rollup(reports))
    worst = doctor_mod.Status.worst([r.status for r in reports])
    if worst is not doctor_mod.Status.OK:
        print(f"\nworst: {worst.name}", file=sys.stderr)
    return 0 if worst is doctor_mod.Status.OK else 1


def _with_credential(report, env: str):
    """Annotate with the stored credential's state, described, never revealed.

    Never decodes the token: an ``fm login`` access token is opaque,
    so the only place left to read expiry from is the metadata
    ``fm login``/refresh wrote beside it (:func:`tokens.load_meta`,
    :func:`tokens.describe_meta`), the same source ``fm whoami``
    already uses.
    """
    try:
        token, origin = tokens.load_for(env)
    except tokens.TokenError as exc:
        return replace(report, credential=f"unusable: {exc}")
    if not token:
        return replace(report, credential=f"none stored (fm login --host {env})")
    if origin == tokens.ENV_TOKEN_ORIGIN:
        return replace(report, credential=f"environment ({tokens.ENV_TOKEN_VAR})")
    info = tokens.describe_meta(tokens.load_meta(origin))
    borrowed = "" if origin == env else f" (using the {origin} token)"
    return replace(report, credential=f"{info.describe_expiry()}{borrowed}")


def _with_migrations(report, target, env: str):
    """Annotate with alembic identity when /admin/instance is available.

    Absence is not a failure: the endpoint is admin-gated (Global, or
    Formist as its own single-region deployment), so a local or
    unauthenticated run against any other region legitimately has
    nothing to say here. Reporting "unknown" would be inventing a
    problem.
    """
    if target.is_prod and not is_admin_env(env):
        return report
    try:
        token, _ = tokens.load_for(target.name)
        payload = api.admin_get(target, "/admin/instance", token=token)
    except (api.ApiError, tokens.TokenError):
        return report
    if not isinstance(payload, dict):
        return report
    for name, data in (payload.get("regions") or {}).items():
        alembic = (data or {}).get("alembic") or {}
        if alembic.get("in_sync") is False:
            return replace(
                report,
                migrations=(
                    f"{name}: db_head={alembic.get('db_head')} != "
                    f"code_heads={alembic.get('code_heads')}; a migration is "
                    "pending or failed"
                ),
            )
    return replace(report, migrations="in sync")


def cmd_use(args) -> int:
    """Remember a default space, and, for reads only, a default environment.

    Records the space's region as reported by the server, so a later
    write whose --env contradicts it is refused before a socket is
    opened. That is a cheap catch for a costly accident, and it costs
    one comparison.
    """
    if args.clear:
        print("forgot the remembered defaults" if prefs.clear() else "nothing remembered")
        return 0

    if not args.show and args.space is None and args.host is None:
        print("nothing to set. Usage: fm use --space <uuid> [--host <host>] | --show | --clear",
              file=sys.stderr)
        return 2

    if args.space is not None or args.host is not None:
        region = prefs._UNSET
        if args.space is not None:
            space = require_space_id(args.space)
            # Read the region back from the SERVER rather than assuming the env
            # being set is the one that owns the space. The recorded value has to
            # come from the thing it describes, or it vouches for nothing.
            region = _lookup_region(space, args.host)
        prefs.store(
            space=args.space if args.space is not None else prefs._UNSET,
            env=args.host if args.host is not None else prefs._UNSET,
            space_region=region,
        )

    current = prefs.load()
    print(f"space  {current.space or '(none)'}")
    print(f"host   {current.env or '(none)'}   (reads only; a write always needs --host)")
    if current.space_region:
        print(f"region {current.space_region}   (recorded from the server)")
    if current.set_at:
        print(f"set    {current.age}")
    return 0


def _lookup_region(space: str, env: str | None) -> object:
    """The region /admin/spaces reports for ``space``, or ``_UNSET`` if unknown.

    Never guessed from the env being set: an unknown region must stay
    unknown, so that `prefs.effective` treats it as absence of
    knowledge rather than as evidence. A lookup that fails is not a
    reason to refuse `fm use`, since the operator may legitimately be
    offline or not logged in.
    """
    for candidate in (env or DEFAULT_HOST,):
        if candidate is None:
            continue
        try:
            target = resolve(prefs.canonical_env(candidate))
        except (TargetError, PrefsError):
            continue
        # Only a prod instance can make a residency claim. A local dev server
        # reports every space it holds as its own home region (`global`), which
        # is a fact about the server, not about the row.
        if not target.is_prod:
            continue
        try:
            token, _ = tokens.load_for(target.name)
            payload = api.admin_get(target, "/admin/spaces", token=token)
        except (api.ApiError, tokens.TokenError):
            continue
        for item in _items(payload):
            if str(item.get("id")) == space and item.get("region"):
                region = str(item["region"])
                print(f"  server reports this space is resident in {region}",
                      file=sys.stderr)
                return region
    print(
        "  (no residency region recorded; only a prod instance can state one)",
        file=sys.stderr,
    )
    return prefs._UNSET


def cmd_get(args) -> int:
    """Find rows by name and print the ids they resolve to.

    Listing and resolving are different acts: listing shows every candidate,
    resolving demands exactly one. `fm get --name X` therefore refuses on ambiguity
    where a bare `fm get` happily prints both rows.
    """
    eff = _effective(args, need_token=True)
    token = eff.token
    kind = resolve_mod.require_kind(args.kind)

    if args.name:
        match = resolve_mod.resolve_one(
            args.name, kind=kind, target=eff.target, space=eff.space, token=token
        )
        if args.json:
            print(json.dumps(
                {"id": match.id, "field": match.field, "value": match.value},
                indent=2, ensure_ascii=False, default=str,
            ))
        else:
            print(f"{match.id}  {match.label}")
            print(f"  matched on {match.field} = "
                  f"{json.dumps(match.value, ensure_ascii=False, default=str)}")
        return 0

    matches = resolve_mod.find(
        None, kind=kind, target=eff.target, space=eff.space, token=token
    )
    if args.json:
        print(json.dumps(
            [{"id": m.id, "field": m.field, "value": m.value} for m in matches],
            indent=2, ensure_ascii=False, default=str,
        ))
        return 0
    for match in matches:
        print(f"{match.id}  {match.label}")
    if not matches:
        print(f"(no {kind} rows in this space)")
    return 0


def add_update_parser(sub) -> None:
    """Public, same reason as :func:`add_instance_parser`: `update` is one
    of the ten commands that moved to curist-server's `fmdev`; this builds
    the exact parser `fm update` used to have, for `fmdev` to call.
    """
    p = sub.add_parser(
        "update",
        help="bulk data change: dry-run always, apply with the plan token",
        description=(
            "Applies a batch of data changes transactionally, or reports what would "
            "happen. A dry run ALWAYS runs first and prints a plan token; --apply "
            "takes that token and re-runs the dry run to check it still matches "
            "before committing.\n\n"
            "Payload ops use the endpoint's own vocabulary ($action, $kind, $id, "
            "$set, $expect, $ref, $replace_data, $hard) plus one fm-side addition: "
            "$name, resolved to an id here, seconds before the write, against the "
            "environment being written. The name is copied into $expect so the "
            "server re-checks the resolution against the row itself.\n\n"
            "A $set naming `data` (STAKEHOLDER / CUSTOM's free-form blob) MERGES "
            "into the row's existing value by default -- {'country': 'AE'} changes "
            "only `country`. Add \"$replace_data\": true to an op to replace the "
            "whole blob instead; that is the only way to remove a key.\n\n"
            "$action \"delete\" is SOFT -- restorable from the space's Trash, "
            "like the UI. $hard is refused for a kind with a Trash entry (purge "
            "from there instead) and required for the two that have none "
            "(FILE_LINK, SESSION_PARTICIPANT).\n\n"
            + update_mod.NOT_DEPLOYED_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    cli_support.add_admin_flag(p)
    p.add_argument("payload", help="path to a JSON file of ops")
    p.add_argument(
        "--apply",
        metavar="PLAN_TOKEN",
        help="commit, if this token still matches a fresh dry run of this plan",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")


def cmd_update(args) -> int:
    """Dry-run a bulk change, then apply it only against the token that dry run gave.

    The dry run is not optional and not skippable. The server executes
    every op for real and rolls back, so what is printed is what would
    happen, including the ids the inserts actually produced.
    """
    eff = _effective(args, for_write=True, need_token=True)
    token = eff.token

    payload = update_mod.load_payload(args.payload)
    plan = update_mod.build_plan(
        payload, target=eff.target, space=eff.space, token=token
    )

    if plan.resolutions:
        print("\nresolved by name:", file=sys.stderr)
        for resolution in plan.resolutions:
            print(f"  {resolution.describe()}", file=sys.stderr)

    if args.apply:
        report = update_mod.apply(
            plan, eff.target, token=token, supplied_token=args.apply
        )
        _print_report(report, applied=True, json_out=args.json)
        return 0

    report = update_mod.dry_run(plan, eff.target, token=token)
    _print_report(report, applied=False, json_out=args.json)
    token_value = update_mod.plan_token(plan, report)
    print(
        f"\nDRY RUN: nothing was changed.\n"
        f"To apply this exact plan:\n"
        f"  fm update --host {eff.target.name} --space {eff.space} "
        f"{args.payload} --apply {token_value}",
        file=sys.stderr,
    )
    return 0


def _print_report(report: object, *, applied: bool, json_out: bool) -> None:
    """Print the server's report.

    Every value here is read out of the response body. Nothing is
    echoed from the payload that was sent: a confirmation line that
    prints what it hoped to write would confirm whatever landed,
    whether or not it matched what was sent.
    """
    if json_out:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return
    if not isinstance(report, dict):
        print(report)
        return

    counts = report.get("counts") or {}
    print(f"mode={report.get('mode')}  committed={report.get('committed')}")
    print(f"  ops={counts.get('ops')}  ok={counts.get('ok')}  "
          f"refused={counts.get('refused')}  applied={counts.get('applied')}")
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        cascaded = result.get("cascaded")
        extra = f"  cascade={cascaded}" if cascaded else ""
        print(f"  [{result.get('index')}] {result.get('action')} "
              f"{result.get('kind')} {result.get('id')} "
              f"-> {result.get('outcome')}{extra}")
    for refusal in report.get("refusals") or []:
        if isinstance(refusal, dict):
            print(f"  REFUSED [{refusal.get('index')}] {refusal.get('reason')}: "
                  f"{refusal.get('message', '')}")


def _items(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    print(json.dumps(payload, indent=2, default=str))
    return []


# ===========================================================================
# Moved to curist-server's `fmdev` (C1105/C1114): one-release stubs
# ===========================================================================

#: No non-admin meaning in `fm` at all -- unlike `spaces`/`health`/`proposals`/
#: `audit-log`/`connector`/`files`, which keep a member-level default here and
#: only gate their --admin branch, these ten are refused locally before the
#: --admin check even runs, so a customer install of `fm` never reaches the
#: server for them either way. `messages`/`participants` are NOT here: they
#: stay in `fm` unchanged, retiring with C1073/C1110 instead.
_MOVED_TO_FMDEV = (
    "instance", "users", "logs", "space-plan", "usage-tokens",
    "alerts", "settings", "update", "airtable", "whatsapp",
)


def _add_moved_to_fmdev_stub(sub, name: str) -> None:
    p = sub.add_parser(
        name,
        help="MOVED to curist-server's `fmdev`; kept one release as a redirect",
        description=(
            f"`fm {name}` moved to curist-server's `fmdev` and no longer runs "
            f"here: run `fmdev {name}` instead (needs the curist-server repo "
            "checked out, or ask whoever operates it). This redirect is kept "
            "through the next release only, after which `fm` stops "
            f"recognising `{name}` at all."
        ),
    )
    # Must absorb whatever the caller passed -- flags the real command used
    # to accept included -- without erroring on an unrecognised option:
    # the point of a redirect is to print it and exit, not to re-validate
    # arguments for a command that is not going to run. `nargs=REMAINDER`
    # on a positional does NOT do this: argparse classifies a leading
    # option-looking token (`fm whatsapp --env ae`, which was this exact
    # command's own documented example) as an unmatched option before a
    # positional ever gets to claim it, so `parse_args` still raises
    # "unrecognized arguments". Overriding `parse_known_args` on this one
    # parser INSTANCE (not the shared `SubcommandParser` class every other
    # subcommand uses) is what `_SubParsersAction.__call__` actually
    # consults, and it both sets `args.command` before calling this and
    # only raises "unrecognized arguments" on whatever this returns as
    # leftover -- so returning none, unconditionally, is what makes every
    # shape of the old invocation land on the redirect instead of an error.
    p.parse_known_args = lambda args=None, namespace=None: (argparse.Namespace(), [])


def cmd_moved_to_fmdev(args) -> int:
    name = args.command
    print(
        f"`fm {name}` moved to curist-server's `fmdev`. Run `fmdev {name}` "
        "instead. This redirect is kept through the next release only.",
        file=sys.stderr,
    )
    return 1


# ===========================================================================
# Noun-group commands: `fm skill` and `fm chat`
#
# Each groups commands that used to be unrelated-looking top-level names
# under one noun with several subjects, the same "generic positional slot
# reused per subject, flags unioned, meaning resolved in the handler"
# pattern `fm spaces`/`fm whatsapp`/`fm connector` already use -- not
# argparse's nested subparsers, which cannot express "this token is either
# one of a few keywords or an arbitrary id" without the same kind of
# pre-dispatch logic anyway. The four old modules' own handlers are called
# UNCHANGED; this is routing, not a second implementation.
# ===========================================================================

_SKILL_SUBJECTS = ("add", "update", "attach", "install-package")


def cmd_skill(args) -> int:
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject == "add":
        args.source = args.arg1
        return skills.cmd_skills_add(args)
    if subject == "update":
        args.source = args.arg1
        return skills.cmd_skills_update(args)
    if subject == "attach":
        args.skill = args.arg1
        args.file = args.arg2
        return uploads_mod.cmd_skill_attach(args)
    if subject == "install-package":
        args.package = args.arg1
        return skill_library.run_install(args)
    raise skills.SkillGroupError(
        f"unknown subcommand {getattr(args, 'subject', None)!r}. "
        f"Valid: {', '.join(_SKILL_SUBJECTS)}."
    )


def cmd_chat_grouped(args) -> int:
    first = args.subject_or_id
    if first == "list":
        args.chat_id = args.second_arg
        return chats_mod.cmd_chats(args)
    if first == "send":
        args.chat_id = args.second_arg
        return chat_mod.cmd_chat(args)
    # Legacy shape: no subject given, so `first` IS the chat id (or None, to
    # start a new chat) -- `fm chat [chat_id]`'s behaviour before `list`/
    # `send` existed, kept working rather than aliased away (see
    # `_add_chat_parser`'s own description of why `chat` is not an alias).
    if first is not None:
        print(
            "note: `fm chat <id>` without a subject is the older spelling of "
            "`fm chat send <id>`; both work, this one through the next "
            "release only.",
            file=sys.stderr,
        )
    args.chat_id = first
    return chat_mod.cmd_chat(args)


def _deprecated_alias(old_command: str, new_group: str, handler):
    """Wraps a kept-for-one-release alias's ORIGINAL handler: prints the
    deprecation note (`cli_support.warn_deprecated_alias`) then runs it
    completely unchanged. One release after this one, the `_COMMANDS` entry
    this wraps is deleted, not just the wrapper.
    """

    def wrapped(args):
        cli_support.warn_deprecated_alias(old_command, new_group)
        return handler(args)

    return wrapped


_COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "whoami": cmd_whoami,
    "health": cmd_health,
    "spaces": spaces_mod.run,
    "proposals": cmd_proposals,
    "audit-log": cmd_audit_log,
    "connectors": _deprecated_alias("connectors", "connector list|grants", cmd_connectors),
    "entity-fields": cmd_entity_fields,
    "doctor": cmd_doctor,
    "use": cmd_use,
    "get": cmd_get,
    "chats": _deprecated_alias("chats", "chat list", chats_mod.cmd_chats),
    "chat": cmd_chat_grouped,
    "export": export_mod.cmd_export,
    "import": importer_mod.cmd_import,
    "skill-library-install": _deprecated_alias(
        "skill-library-install", "skill install-package", skill_library.run_install
    ),
    "skills-add": _deprecated_alias("skills-add", "skill add", skills.cmd_skills_add),
    "skills-update": _deprecated_alias("skills-update", "skill update", skills.cmd_skills_update),
    "skill": cmd_skill,
    "diff": diff_mod.cmd_diff,
    "participants": participants_mod.cmd_participants,
    "upload": uploads_mod.cmd_upload,
    "skill-attach": _deprecated_alias("skill-attach", "skill attach", uploads_mod.cmd_skill_attach),
    "kb": kb_mod.run,
    "meeting": meetings_mod.run,
    "files": files_mod.cmd_files,
    "project": _deprecated_alias("project", "spaces set-logo", projects_mod.run),
    "connector": cmd_connector,
    "messages": messages_mod.run,
    # Moved to curist-server's `fmdev` (C1105/C1114); one-release redirect
    # stubs, not the real commands -- see `_MOVED_TO_FMDEV`'s own comment.
    "instance": cmd_moved_to_fmdev,
    "users": cmd_moved_to_fmdev,
    "logs": cmd_moved_to_fmdev,
    "space-plan": cmd_moved_to_fmdev,
    "usage-tokens": cmd_moved_to_fmdev,
    "alerts": cmd_moved_to_fmdev,
    "settings": cmd_moved_to_fmdev,
    "update": cmd_moved_to_fmdev,
    "airtable": cmd_moved_to_fmdev,
    "whatsapp": cmd_moved_to_fmdev,
}


def main(
    argv: list[str] | None = None,
    *,
    parser: argparse.ArgumentParser | None = None,
    commands: dict | None = None,
    extra_errors: tuple[type[BaseException], ...] = (),
    check_privileges=None,
    prepare=None,
) -> int:
    """Run one command. ``parser``/``commands``/``extra_errors`` let another
    front end (such as a repository's own developer tool) reuse this dispatch."""
    args = (parser or build_parser()).parse_args(argv)
    table = _COMMANDS if commands is None else commands
    try:
        if prepare is not None:
            prepare(args)
        # The --admin gate, for every command whose WHOLE surface is
        # platform-admin (LEVEL_ADMIN). It fires on the missing FLAG, before
        # any file is read or any request is built, regardless of what the
        # stored credential's scopes say; a MIXED command (e.g. `fm spaces`,
        # `fm files merge`) gates its own admin-only subject/subcommand
        # itself, since this table entry cannot see which one is about to run.
        if privileges.requires_admin(args.command) and not getattr(args, "admin", False):
            print(
                f"error: `fm {args.command}` needs --admin (platform administrators "
                "only).",
                file=sys.stderr,
            )
            return 2
        # The privilege pre-check, at the one point every command passes
        # through. It only ever refuses what the server would also refuse.
        # `resolve` here (not `_effective`) for the same reason cmd_login
        # uses it: which credential file is read must come from the command
        # line, never from a remembered target.
        (check_privileges or privileges.check)(
            args.command,
            tokens.local_scopes(resolve(getattr(args, "host", None)).name),
        )
        return table[args.command](args)
    except api.BulkRefused as exc:
        # The refusal report is the product: it names the offending op by index and
        # carries a machine-readable reason. Flattening it to a message would throw
        # away the only part a caller can act on.
        print(f"error: {exc}", file=sys.stderr)
        _print_report(exc.payload, applied=False, json_out=False)
        return 2
    except USER_ERRORS + extra_errors as exc:
        # These carry operator-facing prose, so print the message rather than a
        # traceback: the message is the product.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


#: Exceptions whose message is written for the user; printed, not a traceback.
USER_ERRORS = (
    TargetError,
    PrefsError,
    DiffError,
    ResolutionError,
    ChatsError,
    AirtableError,
    ParticipantsError,
    FilesError,
    ExportError,
    ImportRefused,
    UpdateError,
    UploadError,
    api.ApiError,
    tokens.TokenError,
    logs.LogsError,
    users.UsersError,
    connectors_mod.ConnectorsError,
    space_plan.SpacePlanError,
    skill_library.SkillLibraryError,
    messages_mod.MessagesError,
    SpacesError,
    UsageTokensError,
    AlertsError,
    SettingsError,
    WhatsAppError,
    KbError,
    MeetingsError,
    ProjectsError,
    skills.SkillGroupError,
    privileges.PrivilegeError,
    login_mod.LoginError,
)


if __name__ == "__main__":
    raise SystemExit(main())
