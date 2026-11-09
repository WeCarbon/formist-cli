"""``fm task <verb>``: parity with every `nod` command (contract §6.2/§6.3).

Mode resolution (contract §6.3), printed on the first line of every
command's output (or as ``"mode"`` in ``--json``):

- ``--local`` forces local mode.
- ``--host <name>`` (or a stored credential for the default host) asks for
  remote mode, wired to the server's ``/projects/{id}/nodes`` router
  (C1092/C1093, ``feat/noddit-tasks``) — see ``task_cloud.py``'s module
  docstring for exactly which verbs that covers and which still raise
  "not available" (no server route yet: ``show``, ``search``, ``attach``,
  ``changes``, ``flag``, ``order``, ``check``, ``config``, ...).
- Otherwise, local mode when a ``decisions/`` folder is found upward from
  the cwd (the same lookup `nod` does, including the git-worktree
  main-checkout hop).
- Otherwise: refuse with a hint (`fm task init` or `fm login`). Modes
  never fall back into each other silently.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from formist_cli import task_identity, task_paths, task_store
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser


class TasksError(Exception):
    pass


# -- mode resolution --------------------------------------------------------


def _use_remembered_host_with_credential() -> str | None:
    """``fm use``'s remembered host, but only when BOTH a space is
    remembered AND a credential is stored for it — a stray token file
    left over from unrelated `fm` testing (e.g. `fm spaces`) must never
    silently flip `fm task` into cloud mode; only an actual `fm use`
    default does (contract §6.3: "`fm use` with a stored credential")."""
    from formist_cli import prefs, tokens

    try:
        current = prefs.load()
    except Exception:
        return None
    if not current.space:
        return None
    env = current.env or None
    try:
        token, _ = tokens.load_for(env or __import__("formist_cli.targets", fromlist=["DEFAULT_HOST"]).DEFAULT_HOST)
    except Exception:
        return None
    return env if token else None


def resolve_mode(args) -> tuple[str, object]:
    """Returns ``("local", LocalStore)`` or ``("cloud", CloudTarget)``."""
    local_flag = bool(getattr(args, "local", False))
    host = getattr(args, "host", None)
    if local_flag and host:
        raise TasksError("--local and --host don't mix: pick one mode.")
    if local_flag:
        return "local", task_store.LocalStore(_require_layout(args))
    remembered = None if host else _use_remembered_host_with_credential()
    if host or remembered:
        from formist_cli import task_cloud

        return "cloud", task_cloud.cloud_store(args)
    layout = task_paths.find_layout(getattr(args, "dir", None))
    if layout:
        return "local", task_store.LocalStore(layout)
    raise TasksError(
        "no decisions/ directory found here or in any parent, and no --host/login given.\n"
        "Run `fm task init` to start a local project, or `fm login` to use a Formist space."
    )


def _require_layout(args) -> task_paths.Layout:
    cwd = getattr(args, "dir", None)
    layout = task_paths.find_layout(cwd)
    if not layout:
        raise TasksError(
            "no decisions/ directory found here or in any parent.\n"
            "Run `fm task init` in the project root to create one."
        )
    return layout


def _mode_line(mode: str) -> None:
    if sys.stdout.isatty():
        print(f"[mode: {mode}]", file=sys.stderr)


# -- author / role resolution -------------------------------------------------


def _author(args) -> str:
    explicit = getattr(args, "author", None)
    if explicit:
        return explicit
    env_author = task_identity.current_author()
    if env_author:
        return env_author
    from formist_cli import task_sessions

    remembered = task_sessions.remembered_author()
    if remembered:
        return remembered
    raise TasksError('who is writing this? pass --author "<agent> · <doing what>", or set NODDIT_AUTHOR.')


def _role(args) -> str | None:
    if getattr(args, "human", False):
        return "human"
    if getattr(args, "agent", False):
        return "agent"
    return None


def _resolved_role(args) -> str:
    explicit = _role(args)
    if explicit:
        return explicit
    return "agent" if task_identity.is_agent_shell() else "human"


def _by(args) -> str:
    """Who signs a write, the same defaulting `nod`'s own CLI uses: an
    explicit ``--by``/``--author`` wins outright; otherwise a human write
    signs ``owner``, an agent write signs ``$NODDIT_AUTHOR`` (contract
    §6.2's ``push``/``note`` help text: "human defaults to owner, agent to
    --author / NODDIT_AUTHOR")."""
    explicit = getattr(args, "by", None) or getattr(args, "author", None)
    if explicit:
        return explicit
    if _resolved_role(args) == "human":
        return "owner"
    env_author = task_identity.current_author()
    if env_author:
        return env_author
    from formist_cli import task_sessions

    remembered = task_sessions.remembered_author()
    if remembered:
        return remembered
    raise TasksError('who is writing this? pass --author "<agent> · <doing what>", or set NODDIT_AUTHOR.')


_FULL_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")


def _resolve_date_flag(raw: str | None, flag: str, resolver) -> str | None:
    """A date CLI flag's value, through `nod date`'s fuzzy parser — unless
    it's already a full ISO-with-offset timestamp (what this tool's own
    ``--json`` output and `now_stamp` produce), which is passed through
    unchanged rather than re-parsed (re-parsing it through `parse_when`
    would re-stamp it with THIS machine's current offset, silently
    changing an explicit one from elsewhere)."""
    if raw is None or _FULL_ISO_RE.match(raw):
        return raw
    resolved = resolver(raw)
    if resolved is None:
        raise TasksError(f'{flag} "{raw}" is not a date I can read (try: tomorrow, fri, 3d, 15 nov, 2026-11-15, 下周一)')
    return resolved


def _resolve_due(raw: str | None) -> str | None:
    from formist_cli import task_fuzzy_date

    return _resolve_date_flag(raw, "--due", task_fuzzy_date.resolve_due)


def _resolve_until(raw: str | None) -> str | None:
    from formist_cli import task_fuzzy_date

    return _resolve_date_flag(raw, "--until", task_fuzzy_date.resolve_until)


def _resolve_remind(raw: str | None) -> str | None:
    from formist_cli import task_fuzzy_date

    return _resolve_date_flag(raw, "--remind", task_fuzzy_date.resolve_until)


def _print_node(node, as_json: bool, lang: str = "en") -> None:
    from formist_cli import task_json

    if as_json:
        print(json.dumps(task_json.to_json(node, lang), indent=2, ensure_ascii=False, default=str))
        return
    from formist_cli.task_ref import format_ref

    print(f"{format_ref(node.id, node.letter)}  {node.state:<14} {node.title}")
    if node.body.strip():
        print()
        print(node.body.strip())


# -- parser construction ------------------------------------------------------


def _add_common(p: argparse.ArgumentParser, *, write: bool = False) -> None:
    from formist_cli.cli import _add_env, _add_space  # lazy: avoids a cli<->tasks import cycle

    _add_env(p, for_write=write)
    _add_space(p)
    p.add_argument("--local", action="store_true", help="force local mode (a decisions/ directory)")
    p.add_argument("--dir", help="project root to look for decisions/ in (default: cwd, walking up)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    if write:
        p.add_argument("--author", help='"<agent> · <doing what>" (default: $NODDIT_AUTHOR)')
        p.add_argument("--human", action="store_true", help="this write is the owner's own")
        p.add_argument("--agent", action="store_true", help="this write is an agent's own")


def _add_by(p: argparse.ArgumentParser) -> None:
    """``--by``: who signs this write (default: owner for --human, --author
    for --agent). Added individually, never via ``_add_common``, because
    `nod supersede --by <new-ref>` already uses that spelling for something
    unrelated."""
    p.add_argument("--by", help="who signs this write (default: owner for --human, --author for --agent)")


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "task", help="nod parity: tasks and decisions as plain markdown files (contract C1052)",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verbs = parser.add_subparsers(dest="verb", required=True)

    p = verbs.add_parser("init", help="create decisions/ in this project")
    p.add_argument("dir", nargs="?", default=".")

    p = verbs.add_parser("config", help="project letter / display name (decisions/decs.json)")
    p.add_argument("key", choices=["letter", "name"])
    p.add_argument("value", nargs="?")
    _add_common(p)

    p = verbs.add_parser("whoami", help="this process's identity")
    _add_common(p)

    p = verbs.add_parser("add", help="new node (nod send/new is the same verb)")
    p.add_argument("title")
    p.add_argument("--option", "-o", action="append", default=[], metavar="KEY=LABEL")
    p.add_argument("--default")
    p.add_argument("--task", action="store_true")
    p.add_argument("--fyi", action="store_true")
    p.add_argument("--brief", action="store_true")
    p.add_argument("--for", dest="for_", choices=["me", "ai"], default=None)
    p.add_argument("--milestone", action="store_true")
    p.add_argument("--important", action="store_true")
    p.add_argument("--urgency", choices=["high", "normal", "low"], default="normal")
    p.add_argument("--due")
    p.add_argument("--remind")
    p.add_argument("--if-no-answer")
    p.add_argument("--reversible", choices=["yes", "no"])
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--parent")
    p.add_argument("--after", nargs="*", default=[])
    p.add_argument("--est")
    p.add_argument("--body")
    p.add_argument("--body-file")
    _add_common(p, write=True)

    p = verbs.add_parser("get", help="print one node")
    p.add_argument("ref")
    _add_common(p)

    p = verbs.add_parser("show", help="list nodes, one line each")
    p.add_argument("scope", nargs="?", default="open",
                    help="open (default) | answered | active | archived | all | fyi | "
                         "deferred | flagged | due | remind | <status>")
    p.add_argument("--stale", action="store_true", help="only in-progress nodes past their own stale_after")
    _add_common(p)

    p = verbs.add_parser("views", help="the bench's tabs and their definitions")
    _add_common(p)

    p = verbs.add_parser("view", help="make your own tabs, change or remove one, restore the built-ins")
    view_sub = p.add_subparsers(dest="view_verb", required=True)
    vp = view_sub.add_parser("add", help="a new tab")
    vp.add_argument("id")
    vp.add_argument("--name")
    vp.add_argument("--icon")
    vp.add_argument("--filter")
    vp.add_argument("--layout", choices=["tree", "list"], default="list")
    vp.add_argument("--group", choices=["project", "state", "assignee", "none"])
    vp.add_argument("--sort")
    vp.add_argument("--finished", choices=["hide", "show", "fold"])
    vp.add_argument("--deferred", choices=["hide", "show", "fold"])
    _add_common(vp)
    vp = view_sub.add_parser("rm", help="remove tabs (the last one stays)")
    vp.add_argument("ids", nargs="+")
    _add_common(vp)
    vp = view_sub.add_parser("reset", help="back to the built-ins: every tab, or just that built-in")
    vp.add_argument("id", nargs="?")
    _add_common(vp)
    vp = view_sub.add_parser("show", help="one view's definition")
    vp.add_argument("id")
    _add_common(vp)

    p = verbs.add_parser("tree", help="the node tree")
    p.add_argument("ref", nargs="?")
    p.add_argument("--all", action="store_true", help="include archived")
    _add_common(p)

    p = verbs.add_parser("search", help="find text across nodes")
    p.add_argument("words", nargs="+")
    p.add_argument("--all", action="store_true", help="include archived")
    _add_common(p)

    p = verbs.add_parser("start", help="to do -> in progress")
    p.add_argument("ref")
    p.add_argument("--force", action="store_true")
    # Not full `_add_common(p, write=True)` (no --human/--agent: there is no
    # "role" to pick when starting a node) — just the one flag `nod start`
    # itself resolves (`resolveAuthor(args)`), remembered for this session
    # (task_sessions.record_started) the same as an explicit --author on
    # `claim` already is.
    p.add_argument("--author", help='"<agent> · <doing what>" (default: $NODDIT_AUTHOR)')
    _add_common(p)

    p = verbs.add_parser("done", help="-> done, archived")
    p.add_argument("ref")
    p.add_argument("--note")
    _add_common(p, write=True)

    p = verbs.add_parser("close", help="-> closed, archived")
    p.add_argument("ref")
    p.add_argument("--reason", required=True)
    _add_common(p, write=True)

    p = verbs.add_parser("archive", help="file a finished node away (same as done)")
    p.add_argument("ref")
    p.add_argument("--seen", action="store_true")
    p.add_argument("--undo", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("defer", help="passive delay, or --on for an active wait")
    p.add_argument("ref")
    p.add_argument("--on")
    p.add_argument("--until")
    p.add_argument("--off", action="store_true")
    p.add_argument("--owner-said")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("undefer", help="lift a defer/hold")
    p.add_argument("ref")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("retitle", help="change the title")
    p.add_argument("ref")
    p.add_argument("title")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("move", help="reparent a node with its whole subtree")
    p.add_argument("ref")
    p.add_argument("--parent")
    p.add_argument("--root", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("supersede", help="old node is superseded by the new one")
    p.add_argument("old_ref")
    p.add_argument("--by", dest="new_ref", required=True)
    _add_common(p, write=True)

    p = verbs.add_parser("assign", help="who does it: me|ai|milestone")
    p.add_argument("ref")
    p.add_argument("who")
    p.add_argument("--note")
    _add_common(p, write=True)

    p = verbs.add_parser("flag", help="important|milestone|normal")
    p.add_argument("ref")
    p.add_argument("flag", choices=["important", "milestone", "normal"])
    p.add_argument("--due")
    _add_common(p, write=True)

    p = verbs.add_parser("after", help="queue a node behind others")
    p.add_argument("ref")
    p.add_argument("refs", nargs="*")
    p.add_argument("--clear", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("order", help="put these nodes in this order by hand (first highest)")
    p.add_argument("refs", nargs="+")
    p.add_argument("--clear", action="store_true", help="back to the view's own order")
    _add_common(p, write=True)

    p = verbs.add_parser("ask", help="needs the owner to rule -> needs decision")
    p.add_argument("ref")
    p.add_argument("--option", "-o", action="append", default=[], metavar="KEY=LABEL")
    p.add_argument("--default")
    p.add_argument("--text")
    _add_common(p, write=True)

    p = verbs.add_parser("push", help="record an answer (choose/answer are the same verb)")
    p.add_argument("ref")
    p.add_argument("choice", nargs="?")
    p.add_argument("--note")
    p.add_argument("--relayed", action="store_true")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("read", help="collect an update from the queue (pop is the same verb)")
    p.add_argument("refs", nargs="+")
    p.add_argument("--ack")
    p.add_argument("--peek", action="store_true")
    p.add_argument("--done", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("queue", help="answers no agent has collected yet")
    _add_common(p)

    p = verbs.add_parser("note", help="append a note; does not archive")
    # ref may be omitted (falls back to this session's current node, local
    # mode only — see task_sessions.py's module docstring for exactly why
    # this verb's shape (one optional slot, one required) is safe to
    # retrofit this way and progress/steps below are not).
    p.add_argument("ref", nargs="?")
    p.add_argument("text")
    p.add_argument("--summary")
    p.add_argument("--relayed", action="store_true")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("edit", help="replace the description or one note's text")
    p.add_argument("ref")
    p.add_argument("--body")
    p.add_argument("--note", dest="note_index", type=int)
    p.add_argument("--summary")
    p.add_argument("--force", action="store_true")
    _add_common(p, write=True)
    _add_by(p)

    p = verbs.add_parser("progress", help="progress for a node")
    # ref stays required here (not an inconsistency with note/step/beat
    # below): with it optional, "ref? pct msg?" has TWO optional slots
    # around the required one, so argparse's positional matching greedily
    # assigns the first plain argument to ref instead of falling back
    # whenever both pct AND msg are given — see task_sessions.py's module
    # docstring for the argparse mechanics.
    p.add_argument("ref")
    p.add_argument("pct", type=int)
    p.add_argument("msg", nargs="?")
    _add_common(p)

    p = verbs.add_parser("steps", help="define a node's steps")
    # Same reason as progress above: "ref? labels+" lets 2+ given labels
    # swallow the first one as ref instead of falling back.
    p.add_argument("ref")
    p.add_argument("labels", nargs="+")
    _add_common(p)

    p = verbs.add_parser("step", help="step n done -> progress n/total")
    p.add_argument("ref", nargs="?")  # see note's own comment above
    p.add_argument("n", type=int)
    p.add_argument("--note")
    _add_common(p, write=True)

    p = verbs.add_parser("beat", help="still on it (silent unless --note)")
    p.add_argument("ref", nargs="?")  # see note's own comment above
    p.add_argument("--note")
    _add_common(p, write=True)

    p = verbs.add_parser("attach", help="attach a screenshot, file or link")
    p.add_argument("ref")
    p.add_argument("sources", nargs="+")
    p.add_argument("--link", action="store_true")
    p.add_argument("--name")
    p.add_argument("--caption")
    p.add_argument("--label", dest="group")
    p.add_argument("--approval-content", action="store_true")
    p.add_argument("--approve-on")
    _add_common(p, write=True)

    p = verbs.add_parser("approved", help="does <path> match what was approved?")
    p.add_argument("ref")
    p.add_argument("--check", dest="path", required=True)
    _add_common(p)

    p = verbs.add_parser("claim", help="take a brief")
    p.add_argument("ref")
    p.add_argument("--force", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("expand", help="write the full description of a brief")
    p.add_argument("ref")
    p.add_argument("--title")
    p.add_argument("--body")
    _add_common(p, write=True)

    p = verbs.add_parser("briefs", help="briefs nobody has picked up yet")
    p.add_argument("--all", action="store_true")
    _add_common(p)

    p = verbs.add_parser("check", help="validate every node")
    _add_common(p)

    p = verbs.add_parser("changes", help="everything after --since")
    p.add_argument("--since", required=True)
    _add_common(p)

    p = verbs.add_parser("mirror", help="push changed local nodes and deletions to a mirror Section")
    p.add_argument("--section", required=True, help="destination mirror Section id or letter")
    p.add_argument("--full", action="store_true", help="re-send every live local node")
    _add_common(p)

    p = verbs.add_parser(
        "tell", help="the owner's own words, straight to the space's main agent (contract §4's `tell` row)",
    )
    p.add_argument("text", nargs="?", help='the owner\'s words (omit with --read/--finish/--list)')
    p.add_argument("--attach", action="append", default=[], help="a path to copy in beside the message; repeatable")
    p.add_argument("--under", help="the node the owner was looking at when they said this — a hint, not where to file it")
    p.add_argument("--important", action="store_true")
    p.add_argument("--read", metavar="ID", help="print one message; does not change its status")
    p.add_argument("--finish", metavar="ID", help="record which refs this message became")
    p.add_argument("--refs", help="comma-separated refs, with --finish")
    p.add_argument("--list", action="store_true", help="unfinished messages (--all for finished ones too)")
    p.add_argument("--all", action="store_true")
    _add_common(p, write=True)

    p = verbs.add_parser("monitor", help="one line per event (for an agent's Monitor tool)")
    p.add_argument("--since")
    p.add_argument("--mine", action="store_true", help="only this agent's own nodes (name before ·)")
    p.add_argument("--author", help="only one agent's own nodes, by name")
    p.add_argument("--idle-exit", type=float, default=7200,
                    help="go offline after this many seconds with no event, 0 = never (default 7200)")
    p.add_argument("--once", action="store_true", help="print one pass and exit (fm's own test/CI hook)")
    p.add_argument(
        "--here", action="store_true",
        help="watch whatever this context already is (local decisions/ found upward from "
             "cwd, else fm use's remembered cloud space), never idle out, exit quietly (code "
             "0) if neither applies — what the Claude Code plugin's monitors.json runs",
    )
    _add_common(p)

    p = verbs.add_parser("wait", help="block until there's something on the queue, then exit")
    p.add_argument("refs", nargs="*")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--author")
    p.add_argument("--timeout", type=float, help="seconds; exit code 2 on timeout (default: forever)")
    _add_common(p)

    p = verbs.add_parser("watch", help="stream changes, never exits (for a human's pane)")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--author")
    _add_common(p)

    # Internal: what the Claude Code plugin's hooks/hooks.json runs directly
    # (C1053/C1139) — read Claude Code's hook JSON from stdin, print its own
    # hook-output JSON to stdout. Not meant to be run by hand; see
    # plugin_hooks.py's module docstring for why there is no separate
    # plugin/*.py wrapper script the way noddit's hooks need (fm is an
    # installed console script, not a git checkout a wrapper can import
    # from, so the hook's "thin script" IS this subcommand).
    p = verbs.add_parser("plugin-session-start", help=argparse.SUPPRESS)
    _add_common(p)

    p = verbs.add_parser("plugin-prompt-refs", help=argparse.SUPPRESS)
    _add_common(p)

    return parser


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


# -- dispatch -----------------------------------------------------------------


def run(args) -> int:
    verb = args.verb
    handler = _VERBS.get(verb)
    if handler is None:
        raise TasksError(f"unknown `fm task` verb: {verb}")
    return handler(args)


def _task_user_errors() -> tuple[type[BaseException], ...]:
    """Exceptions this subcommand's own CLI layer should print as operator
    prose rather than a traceback. ``cli.py``'s own ``USER_ERRORS`` folds
    these in (``*tasks_mod._task_user_errors()``, C1141's rebase onto
    ``feat/fm-slim``) so `fm task` gets the same "print the message,
    return 2, never a traceback" handling as every other `fm` subcommand.
    :func:`main` below (the standalone entry point, kept for this
    module's own tests and for any other front end) applies the same
    tuple itself, so the behavior is identical either way it's called.
    Imported lazily to avoid a load-time dependency from this module onto
    the Store/questions/attach/title modules just to build an error
    tuple."""
    from formist_cli.api import ApiError
    from formist_cli.prefs import PrefsError
    from formist_cli.targets import TargetError
    from formist_cli.task_attach import AttachError
    from formist_cli.task_cloud import CloudError
    from formist_cli.task_mirror import MirrorError
    from formist_cli.task_questions import QuestionsError
    from formist_cli.task_store import StoreError
    from formist_cli.task_title import TitleTooLong
    from formist_cli.tokens import TokenError

    return (
        TasksError, StoreError, QuestionsError, AttachError, MirrorError, TitleTooLong,
        CloudError, ApiError, PrefsError, TargetError, TokenError,
    )


def main(argv: list[str]) -> int:
    """Standalone ``fm task <verb> ...`` entry point — ``argv`` WITHOUT the
    leading ``"task"`` token (``build_standalone_parser()`` already is the
    ``task`` subparser, same convention ``cli_support.build_standalone_parser``
    uses for every other module's own standalone parser).

    Used by this module's own test suite, and by anything else that wants
    `fm task` without the rest of `fm` (``cli.main`` itself dispatches
    through :func:`run` directly instead, sharing its own ``USER_ERRORS``
    loop — see :func:`_task_user_errors`) — so the error handling here is
    deliberately the same shape as `cli.main`'s: print the message,
    return 2, never a traceback.
    """
    parser = build_standalone_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except _task_user_errors() as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _cmd_init(args) -> int:
    layout = task_paths.init_layout(args.dir)
    print(f"created {layout.root}")
    return 0


def _cmd_config(args) -> int:
    from formist_cli import task_project

    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task config` is local-mode only")
    layout = target.layout
    if args.value is None:
        cfg = task_project.read_project_config(layout)
        print(cfg.get(args.key) or "(not set)")
        return 0
    task_project.write_project_config(layout, **{args.key: args.value})
    print(f"{args.key} = {args.value}")
    return 0


def _cmd_whoami(args) -> int:
    identity = task_identity.current_identity()
    if getattr(args, "json", False):
        print(json.dumps({
            "identity": identity, "author": task_identity.current_author(),
            "session": task_identity.current_session(),
        }, indent=2))
    else:
        print(identity or "(no identity: neither NODDIT_IDENTITY nor NODDIT_AUTHOR/session is set)")
    return 0


def _read_body(args) -> str:
    if getattr(args, "body", None):
        return args.body
    if getattr(args, "body_file", None):
        with open(args.body_file, encoding="utf-8") as fh:
            return fh.read()
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read()
            if data.strip():
                return data
    except OSError:
        pass  # stdin isn't readable here (no pipe, a test's captured stdin, …)
    return ""


def _parse_options(raw: list[str]):
    from formist_cli.task_model import Option

    out = []
    for item in raw:
        if "=" not in item:
            raise TasksError(f"--option must be KEY=LABEL, got {item!r}")
        key, label = item.split("=", 1)
        out.append(Option(key=key.strip(), label=label.strip()))
    return out


def _cmd_add(args) -> int:
    from formist_cli.task_assignee import assignee_arg

    _mode, target = resolve_mode(args)
    author = _author(args)
    options = _parse_options(args.option)
    assignee = assignee_arg(args.for_) if args.for_ else None
    reversible = {"yes": True, "no": False}.get(args.reversible)
    node = target.create(
        args.title, author, body=_read_body(args), options=options or None, default=args.default,
        task=args.task, fyi=args.fyi, tags=args.tag, urgency=args.urgency, due=_resolve_due(args.due),
        parent=args.parent, after=args.after, est=args.est, assignee=assignee,
        milestone=args.milestone or False, intake=args.brief, remind_at=_resolve_remind(args.remind),
        if_no_answer=args.if_no_answer, reversible=reversible,
    )
    if args.important:
        node = target.flag(node.id, important=True)
    _print_node(node, args.json)
    return 0


def _cmd_get(args) -> int:
    mode, target = resolve_mode(args)
    node = target.resolve(args.ref) if mode == "local" else _cloud(args, target, "get")
    _print_node(node, args.json)
    return 0


def _cmd_show(args) -> int:
    mode, target = resolve_mode(args)
    nodes = target.show(args.scope, args.stale) if mode == "local" else _cloud(args, target, "show")
    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in nodes], indent=2, ensure_ascii=False, default=str))
        return 0
    from formist_cli.task_ref import format_ref

    for n in nodes:
        print(f"{format_ref(n.id, n.letter)}  {n.state:<14} {n.title}")
    return 0


def _view_to_json(v) -> dict:
    from formist_cli import task_filter

    return {
        "id": v.id, "layout": v.layout, "name": v.name, "icon": v.icon,
        "filter": None if v.filter is None else task_filter.filter_text(v.filter),
        "group": v.group, "sort": v.sort, "finished": v.finished, "deferred": v.deferred,
        "window": v.window, "count": v.count,
    }


def _cmd_views(args) -> int:
    from formist_cli import task_views

    mode, _target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task views` is local-mode only for now")
    views = task_views.load_views()
    if args.json:
        print(json.dumps({"file": task_views.views_path(), "views": [_view_to_json(v) for v in views]},
                          indent=2, ensure_ascii=False))
        return 0
    for v in views:
        name = v.name if isinstance(v.name, str) else v.id
        print(f"{v.id:<12} {name}")
    return 0


def _cmd_view(args) -> int:
    from formist_cli import task_filter, task_views

    mode, _target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task view` is local-mode only for now")

    if args.view_verb == "add":
        view = task_views.ViewDef(
            id=args.id, layout=args.layout,
            name=args.name, icon=args.icon,
            filter=task_filter.parse_filter(args.filter) if args.filter else None,
            group=args.group, sort=args.sort.split(",") if args.sort else ["bench"],
            finished=args.finished or "hide", deferred=args.deferred or "hide",
        )
        views = [v for v in task_views.load_views() if v.id != view.id] + [view]
        task_views.save_views(views)
        print(f"added view {view.id}")
        return 0
    if args.view_verb == "rm":
        views = task_views.load_views()
        remaining = [v for v in views if v.id not in args.ids]
        if not remaining:
            raise TasksError("can't remove the last view")
        task_views.save_views(remaining)
        print(f"removed: {' '.join(args.ids)}")
        return 0
    if args.view_verb == "reset":
        views = task_views.reset_views(args.id)
        print(f"reset: {args.id or 'all views'}")
        if args.json:
            print(json.dumps([_view_to_json(v) for v in views], indent=2, ensure_ascii=False))
        return 0
    if args.view_verb == "show":
        v = task_views.find_view(args.id)
        if not v:
            raise TasksError(f"no such view: {args.id}")
        if args.json:
            print(json.dumps(_view_to_json(v), indent=2, ensure_ascii=False))
        else:
            print(f"{v.id}  layout={v.layout}  filter={task_filter.filter_text(v.filter) or '(none)'}")
        return 0
    raise TasksError(f"unknown `fm task view` subcommand: {args.view_verb}")


def _cmd_tree(args) -> int:
    mode, target = resolve_mode(args)
    nodes = target.tree(args.ref, include_archived=args.all) if mode == "local" else _cloud(args, target, "tree")
    if args.json:
        print(json.dumps(_tree_json(nodes, target), indent=2, ensure_ascii=False, default=str))
        return 0
    from formist_cli.task_ref import format_ref

    for n in nodes:
        print(f"{format_ref(n.id, n.letter)}  {n.state:<14} {n.title}")
    return 0


def _tree_json(nodes, store):
    from formist_cli import task_json

    return [task_json.to_json(n) for n in nodes]


def _cmd_search(args) -> int:
    mode, target = resolve_mode(args)
    hits = target.search(args.words, include_archived=args.all) if mode == "local" else _cloud(args, target, "search")
    from formist_cli.task_ref import format_ref

    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in hits], indent=2, ensure_ascii=False, default=str))
        return 0
    for n in hits:
        print(f"{format_ref(n.id, n.letter)}  {n.title}")
    return 0


def _resolve_local(target, ref):
    """Resolve ``ref`` against either mode. Despite the name (kept so the
    ~20 call sites below didn't all need touching when cloud mode landed,
    C1141), this no longer refuses cloud mode itself — ``target.resolve``
    works against a ``CloudStore`` too (``GET /nodes/{ref}``). A verb this
    CLOUD store doesn't implement yet still fails, just later and with a
    clearer, verb-specific message (``task_cloud.CloudNotAvailable``) than
    a blanket refusal here would give."""
    from formist_cli import task_cloud

    if not isinstance(target, (task_store.LocalStore, task_cloud.CloudStore)):
        raise TasksError("unexpected `fm task` target — this is an internal error, not a usage mistake")
    return target.resolve(ref)


def _resolve_ref_or_current(target, ref):
    """Like :func:`_resolve_local`, but ``ref`` may be ``None`` — only on
    the few verbs whose own positional shape makes that unambiguous for
    argparse (``beat``/``step``/``note``; see ``task_sessions.py``'s
    module docstring for why not every verb gets this). Falls back to
    this session's current node, local mode only — cloud mode has no
    local session file to remember one in (C1141's own ``agent_session``
    row is the cloud equivalent, not consulted here)."""
    if ref is not None:
        return _resolve_local(target, ref)
    if not isinstance(target, task_store.LocalStore):
        raise TasksError("this verb needs an explicit ref in cloud mode (no local session to fall back to)")
    from formist_cli import task_sessions

    current = task_sessions.current_node()
    if not current:
        raise TasksError(
            "no ref given, and this session has no current node — "
            "`fm task start <ref>` (or `claim`) first, or pass one explicitly"
        )
    return target.resolve(current["ref"])


def _cmd_start(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    # `force` is a local-mode-only override (CloudStore.start ignores an
    # extra kwarg it was never given — the server route takes none), same
    # as every other lifecycle verb below that doesn't branch on mode.
    result = target.start(node.id, force=args.force) if isinstance(target, task_store.LocalStore) else target.start(node.id)
    _remember_started(target, result, args)
    _print_node(result, args.json)
    return 0


def _remember_started(target, node, args) -> None:
    """local mode only: ``fm task start``/``claim``'s session-state side
    effect (``task_sessions.record_started``) — cloud mode's equivalent
    is the server's own ``agent_session.current_task_id``, set through
    ``register_session``/``fm task monitor``, not this file.

    Gated on a resolved author, matching noddit's own ``if (author &&
    rememberStarted(...))``: a human running this from a bare terminal
    (no --author, no NODDIT_AUTHOR) gets no session state recorded at
    all, same as nod — there is no repeated-call convenience to build
    for someone who isn't making repeated CLI calls."""
    if not isinstance(target, task_store.LocalStore):
        return
    author = getattr(args, "author", None) or task_identity.current_author()
    if not author:
        return
    from formist_cli import task_sessions

    task_sessions.record_started(target.layout.project_root, node.id, _ref_of(node), author)


def _ref_of(node) -> str:
    from formist_cli.task_ref import format_ref

    return format_ref(node.id, node.letter)


def _cmd_done(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.complete(node.id, note_text=args.note, author=_author_opt(args))
    _print_node(result, args.json)
    return 0


def _author_opt(args) -> str | None:
    try:
        return _author(args)
    except TasksError:
        return None


def _cmd_close(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.close(node.id, args.reason)
    _print_node(result, args.json)
    return 0


def _cmd_archive(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    if args.seen:
        result = target.dismiss(node.id, _author(args))
    elif args.undo:
        result = target.unarchive(node.id)
    else:
        result = target.complete(node.id)
    _print_node(result, args.json)
    return 0


def _cmd_defer(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    author = _by(args)
    role = _role(args)
    if args.off:
        if node.state == "waiting":
            result = target.unhold(node.id, author, role)
        else:
            result = target.undefer(node.id, author, role)
    elif args.on:
        result = target.hold(node.id, args.on, author, role, until=_resolve_until(args.until))
    else:
        result = target.defer(node.id, author, role, until=_resolve_until(args.until), owner_said=args.owner_said)
    _print_node(result, args.json)
    return 0


def _cmd_undefer(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    author = _by(args)
    role = _role(args)
    if node.state == "waiting":
        result = target.unhold(node.id, author, role)
    else:
        result = target.undefer(node.id, author, role)
    _print_node(result, args.json)
    return 0


def _cmd_retitle(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.retitle(node.id, args.title, _by(args), _role(args))
    _print_node(result, args.json)
    return 0


def _cmd_move(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.move(node.id, parent=args.parent, root=args.root)
    _print_node(result, args.json)
    return 0


def _cmd_supersede(args) -> int:
    _mode, target = resolve_mode(args)
    old = _resolve_local(target, args.old_ref)
    new = _resolve_local(target, args.new_ref)
    old_result, _new_result = target.supersede(old.id, new.id, _author(args))
    _print_node(old_result, args.json)
    return 0


def _cmd_assign(args) -> int:
    from formist_cli.task_assignee import assignee_arg, is_milestone_word

    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    if is_milestone_word(args.who):
        result = target.set_milestone(node.id, True)
    else:
        result = target.assign(node.id, assignee_arg(args.who))
    _print_node(result, args.json)
    return 0


def _cmd_flag(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    due = _resolve_due(args.due)
    if args.flag == "important":
        result = target.flag(node.id, important=True, due=due)
    elif args.flag == "milestone":
        result = target.flag(node.id, milestone=True, due=due)
    else:
        result = target.flag(node.id, important=False, milestone=False, due=due)
    _print_node(result, args.json)
    return 0


def _cmd_after(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.set_after(node.id, [] if args.clear else args.refs)
    _print_node(result, args.json)
    return 0


def _cmd_order(args) -> int:
    _mode, target = resolve_mode(args)
    nodes = [_resolve_local(target, ref) for ref in args.refs]
    if args.clear:
        results = [target.unarrange(n.id) for n in nodes]
    else:
        results = target.arrange([n.id for n in nodes])
    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in results], indent=2, ensure_ascii=False, default=str))
        return 0
    for r in results:
        _print_node(r, False)
    return 0


def _cmd_ask(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    options = _parse_options(args.option) or None
    result = target.ask(node.id, options=options, text=args.text, default=args.default, by=_author(args))
    _print_node(result, args.json)
    return 0


def _cmd_push(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    author = _by(args)
    role = _resolved_role(args)
    choice = args.choice
    answers = None
    if choice and "=" in choice:
        answers = {}
        for tok in [choice, *([] if not getattr(args, "extra", None) else args.extra)]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                answers[k] = v
        choice = None
    result = target.answer(
        node.id, choice=choice, note_text=args.note, by=author, role=role, answers=answers,
        relayed=args.relayed,
    )
    _print_node(result, args.json)
    return 0


def _cmd_read(args) -> int:
    _mode, target = resolve_mode(args)
    results = []
    for ref in args.refs:
        node = _resolve_local(target, ref)
        if args.peek:
            results.append(node)
            continue
        if args.ack:
            target.note(node.id, args.ack, _author(args), kind="已收到")
        result = target.deliver(node.id)
        if args.done:
            result = target.complete(result.id)
        results.append(result)
    for r in results:
        _print_node(r, args.json)
    return 0


def _cmd_queue(args) -> int:
    mode, target = resolve_mode(args)
    if mode != "local":
        # Deliberately a different --json shape from local mode below (full
        # nodes, not bare ids): the server's GET /nodes/queue hands back
        # whole NodeOut rows, and re-fetching each by id just to match
        # local's shape would be a wasted round trip per id for no benefit.
        nodes = target.queue_refs()
        if args.json:
            from formist_cli import task_json

            print(json.dumps([task_json.to_json(n) for n in nodes], indent=2, ensure_ascii=False, default=str))
            return 0
        from formist_cli.task_ref import format_ref

        for n in nodes:
            print(f"{format_ref(n.id, n.letter)}  {n.title}")
        return 0
    from formist_cli.task_queue import read_queue

    ids = read_queue(target.layout)
    if args.json:
        print(json.dumps(ids))
        return 0
    for i in ids:
        print(i)
    return 0


def _cmd_note(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_ref_or_current(target, args.ref)
    role = _resolved_role(args)
    author = _by(args)
    result = target.note(node.id, args.text, author, role=role, summary=args.summary, relayed=args.relayed)
    _print_node(result, args.json)
    return 0


def _cmd_edit(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    author = _by(args)
    role = _resolved_role(args)
    if args.note_index:
        # `None` (not given, nothing piped) means "only --summary changes" —
        # an empty string here would wrongly blank out the note's text.
        text = args.body if args.body is not None else (_read_body(args) or None)
        result = target.edit_note(
            node.id, args.note_index, text=text,
            summary=args.summary, by=author, role=role, force=args.force,
        )
    else:
        text = args.body if args.body is not None else _read_body(args)
        result = target.edit_body(node.id, text, author, role)
    _print_node(result, args.json)
    return 0


def _cmd_progress(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.progress(node.id, args.pct, args.msg)
    _print_node(result, args.json)
    return 0


def _cmd_steps(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.set_steps(node.id, args.labels)
    _print_node(result, args.json)
    return 0


def _cmd_step(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_ref_or_current(target, args.ref)
    result = target.mark_step(node.id, args.n, note_text=args.note, author=_author_opt(args))
    _print_node(result, args.json)
    return 0


def _cmd_beat(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_ref_or_current(target, args.ref)
    result = target.beat(node.id, note_text=args.note, author=_author_opt(args))
    _print_node(result, args.json)
    return 0


def _cmd_attach(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    author = _author_opt(args) or "fm"
    approve_on = args.approve_on.split(",") if args.approve_on else None
    result = target.attach(
        node.id, args.sources, author, link=args.link, name=args.name,
        approval_content=args.approval_content, approve_on=approve_on,
        caption=args.caption, group=args.group,
    )
    _print_node(result, args.json)
    return 0


def _cmd_approved(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    code, reason = target.approved_check(node.id, args.path)
    if args.json:
        print(json.dumps({"ok": code == 0, "reason": reason}))
    else:
        print(reason, file=sys.stderr)
    return code


def _cmd_claim(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result, _already = target.claim(node.id, _author(args), force=args.force)
    _remember_started(target, result, args)
    _print_node(result, args.json)
    return 0


def _cmd_expand(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    description = args.body if args.body is not None else _read_body(args)
    result = target.expand(node.id, description, title=args.title)
    _print_node(result, args.json)
    return 0


def _cmd_briefs(args) -> int:
    _mode, target = resolve_mode(args)
    nodes = target.briefs(include_claimed=args.all)
    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in nodes], indent=2, ensure_ascii=False, default=str))
        return 0
    from formist_cli.task_ref import format_ref

    for n in nodes:
        print(f"{format_ref(n.id, n.letter)}  {n.title}")
    return 0


def _cmd_check(args) -> int:
    _mode, target = resolve_mode(args)
    problems, total = target.check()
    errors = [p for p in problems if p.level == "error"]
    if args.json:
        print(json.dumps([{"level": p.level, "file": p.file, "message": p.message} for p in problems],
                          ensure_ascii=False))
        return 1 if errors else 0

    # The same warning across dozens of entries is one fact, not fifty.
    by_message: dict[str, list] = {}
    for p in problems:
        by_message.setdefault(p.message, []).append(p)
    for message, group in by_message.items():
        mark = "✗" if group[0].level == "error" else "!"
        for p in group[:5]:
            print(f"{mark} {p.file} — {message}")
        if len(group) > 5:
            extra = len(group) - 5
            if extra == 1:
                print(f'  …{extra} more entry is also "{message}"')
            else:
                print(f'  …{extra} more entries are also "{message}"')

    if problems:
        print(f"\n{total} entries, {len(errors)} errors, {len(problems) - len(errors)} warnings")
    else:
        print(f"{total} entries, no problems")
    return 1 if errors else 0


def _cmd_changes(args) -> int:
    _mode, target = resolve_mode(args)
    events = target.changes(args.since)
    if args.json:
        print(json.dumps({"since": args.since, "events": events}, indent=2, ensure_ascii=False))
        return 0
    for e in events:
        # cloud events: `kind` is the six-value discriminator (created/
        # answered/note/completed/closed/reopened); a `kind="note"` row's
        # finer activity type (ask/deferred/...) is the separate `note_kind`
        # field, more specific and preferred for display when present —
        # matching how local mode's own "kind" is already the specific
        # note kind, not a generic category.
        print(f"{e['at']}  {e['ref']}  {e.get('note_kind') or e.get('kind')}")
    return 0


def _cmd_tell(args) -> int:
    """`nod tell`'s shape (contract §4's `tell` row): the owner's own
    words into the space's main agent, no ref allocated anywhere until
    whoever reads it decides where each piece belongs. Cloud-only (see
    `task_cloud.CloudStore.tell*`'s docstring) — `fm` has no multi-project
    main-session registry the way `nod`'s does, so there is nowhere local
    for this to land."""
    _mode, target = resolve_mode(args)
    if args.read:
        result = target.tell_read(args.read)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('author', '?')}: {result.get('text', '')}")
        return 0
    if args.finish:
        if not args.refs:
            raise TasksError("--finish needs --refs <ref1,ref2,...>")
        result = target.tell_finish(args.finish, [r.strip() for r in args.refs.split(",") if r.strip()])
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"recorded: {args.refs}")
        return 0
    if args.list:
        items = target.tell_list(include_finished=args.all)
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
            return 0
        for item in items:
            print(f"{item.get('id')}  {item.get('author', '?')}: {item.get('text', '')}")
        return 0
    if not args.text:
        raise TasksError("`fm task tell` needs the owner's words, or --read/--finish/--list")
    # `nod tell`'s own default ("--author ... 默认 owner"): this is a relay
    # of the OWNER's words, so with nothing given it signs as the owner,
    # not whatever this agent's own NODDIT_AUTHOR happens to be — unlike
    # every other write verb, which uses _author(args) and raises with
    # neither.
    author = getattr(args, "author", None) or "owner"
    result = target.tell(args.text, author=author, attach=args.attach or None, under=args.under,
                          important=args.important)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        routed = result.get("routed")
        tail = f" (routed: {routed})" if routed else ""
        print(f"sent: {result.get('id', '?')}{tail}")
        done = result.get("done")
        if done:
            print(f"filed as: {', '.join(done.get('refs') or [])}")
    return 0


def _monitor_author(args) -> str | None:
    if getattr(args, "author", None):
        return args.author
    if getattr(args, "mine", False):
        from formist_cli.task_identity import agent_name_of

        author = agent_name_of(task_identity.current_author())
        if not author:
            raise TasksError("--mine needs a name: pass --author, or set NODDIT_AUTHOR.")
        return author
    return None


def _resolve_here(args) -> tuple[str, object] | None:
    """``--here``'s own resolution, not :func:`resolve_mode`'s: a local
    project found upward from the cwd wins (the most specific signal
    "here" can mean), THEN a remembered cloud default, and ``None`` —
    not a refusal — when neither applies. Exactly `nod`'s own
    ``--here``: "只盯当前目录所在的项目；不在 nod 项目里就直接退出"
    (exit quietly, the plugin's auto-started monitor must never error
    out of a directory that simply isn't a project)."""
    layout = task_paths.find_layout(getattr(args, "dir", None))
    if layout:
        return "local", task_store.LocalStore(layout)
    remembered = _use_remembered_host_with_credential()
    if remembered:
        from formist_cli import task_cloud

        return "cloud", task_cloud.cloud_store(args)
    return None


def _cmd_monitor(args) -> int:
    import time as _time

    from formist_cli import task_monitor

    if getattr(args, "here", False):
        resolved = _resolve_here(args)
        if resolved is None:
            return 0  # quietly: not a project, not a remembered cloud space either
        mode, target = resolved
        args.idle_exit = 0
    else:
        mode, target = resolve_mode(args)
    if mode != "local":
        return _cloud_monitor(args, target)
    author = _monitor_author(args)
    explicit_since = bool(args.since)
    since = args.since or task_monitor.load_cursor(target.layout, author) or _now_iso()
    idle_exit = args.idle_exit if args.idle_exit else None

    last_event_at = _time.monotonic()
    done = False
    try:
        while True:
            lines, since = task_monitor.run_pass(target, author, since)
            if not explicit_since:
                task_monitor.save_cursor(target.layout, author, since)
            for line in lines:
                print(line)
                if line.startswith("DECS-DONE"):
                    done = True
            if lines:
                last_event_at = _time.monotonic()
            if args.once or done:
                return 0
            if idle_exit and _time.monotonic() - last_event_at >= idle_exit:
                print(f"DECS-IDLE no new events for {int(idle_exit)}s, monitor going offline.")
                return 0
            _time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _cmd_wait(args) -> int:
    import time as _time

    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task wait` is local-mode only for now; cloud mode has `fm task monitor` instead")
    author = _monitor_author(args)
    want_ids = {target.resolve(r).id for r in args.refs} if args.refs else None
    start = _time.monotonic()
    since = _now_iso()

    def snapshot():
        from formist_cli.task_queue import read_queue

        ids = read_queue(target.layout)
        if want_ids is not None:
            ids = [i for i in ids if i in want_ids]
        if author:
            from formist_cli import task_monitor

            ids = [i for i in ids if task_monitor.same_agent(target.get(i).author, author)]
        return ids

    while True:
        ids = snapshot()
        if ids:
            from formist_cli.task_ref import format_ref

            refs = [format_ref(i, target.letter()) for i in ids]
            if args.json:
                print(json.dumps({"updates": True, "count_at_wake": len(ids), "ids_at_wake": refs,
                                   "next": "fm task queue --json", "since": since}))
            else:
                print(f"there are updates: {' '.join(refs)}")
                print('fm task read <ref> --ack "<what you\'ll do>"  (or: fm task queue)')
                print(f"--since {since}")
            return 0
        if args.timeout is not None and _time.monotonic() - start >= args.timeout:
            if args.json:
                print(json.dumps({"updates": False, "timed_out": True, "since": since}))
            else:
                print("timed out waiting for an update.", file=sys.stderr)
            return 2
        try:
            _time.sleep(1)
        except KeyboardInterrupt:
            return 130


def _cmd_watch(args) -> int:
    import time as _time

    from formist_cli import task_monitor

    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task watch` is local-mode only for now; cloud mode has `fm task monitor` instead")
    author = _monitor_author(args)
    since = _now_iso()
    # Poll-based (no fs.watch equivalent — stdlib-only, C1138 research spec
    # §8): reacts within ~1s, not nod's ~100ms common case. Never exits.
    try:
        while True:
            lines, since = task_monitor.run_pass(target, author, since)
            for line in lines:
                print(line)
            _time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _cmd_mirror(args) -> int:
    from formist_cli import task_mirror

    result = task_mirror.sync(args)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"mirrored {result['changed']} changed, {result['deleted']} deleted; "
            f"last synced {result['last_synced_at']}"
        )
    return 0


def _now_iso() -> str:
    from formist_cli.task_store import now_stamp

    return now_stamp(ms=True)


def _read_hook_json() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _cmd_plugin_session_start(args) -> int:
    """``hooks/hooks.json``'s ``SessionStart`` entry: the plugin's own
    rules text, plus this project's/space's open work, exactly as
    noddit's ``session-start.mjs`` builds it — see ``plugin_hooks.py``'s
    module docstring for why this subcommand, not a separate wrapper
    script, is the "thin script" here."""
    from formist_cli import plugin_hooks, task_json

    hook_input = _read_hook_json()
    tree_json = None
    try:
        probe = argparse.Namespace(local=False, host=None, space=None, dir=hook_input.get("cwd"))
        _mode, target = resolve_mode(probe)
        tree_json = [task_json.to_json(n) for n in target.tree()]
    except Exception:  # noqa: BLE001 - no project/space here is not an error; the rules alone still apply
        tree_json = None
    text = plugin_hooks.build_session_start_context(plugin_hooks.RULES_TEXT, tree_json)
    print(json.dumps(plugin_hooks.SessionStartResult(text).to_hook_output()))
    return 0


def _cmd_plugin_prompt_refs(args) -> int:
    """``hooks/hooks.json``'s ``UserPromptSubmit`` entry: a reminder when
    the owner's message names a node — noddit's ``prompt-refs.mjs``."""
    from formist_cli import plugin_hooks

    hook_input = _read_hook_json()
    prompt = hook_input.get("prompt") or ""
    known = plugin_hooks.known_letters_for(hook_input.get("cwd"))
    refs = plugin_hooks.detect_refs(prompt, known)
    if not refs:
        return 0
    text = plugin_hooks.build_prompt_refs_reminder(refs)
    print(json.dumps(plugin_hooks.PromptSubmitResult(text).to_hook_output()))
    return 0


def _cloud_monitor(args, target) -> int:
    """``fm task monitor`` (cloud): the server's push SSE stream
    (``GET /nodes/events``, contract §5/§93) rather than a poll loop —
    the same monitor lines local mode's own ``task_monitor`` prints,
    rendered server-side, so this function does no line-formatting of
    its own.

    Registers a live agent session first (``POST /agent-sessions``,
    contract §7) so the space's member list of active sessions shows
    this one — best-effort: a registration failure is reported but does
    not stop the monitor, same as the server's own dispatcher-emit
    failures never block the write they are reporting on.

    ``--idle-exit`` is not ported for cloud mode (gap): the server's own
    SSE heartbeat (every 15s) keeps the connection itself from reading as
    idle, so there is no silence to measure the way local mode's poll
    loop does; ``DECS-DONE`` (nothing of this agent's left open) remains
    the one automatic stop condition, same as local."""
    author = _monitor_author(args)
    client_session = getattr(args, "session", None) or task_identity.current_session() or "fm-task-monitor"
    try:
        target.register_session(client_session, author=author, current=None)
    except Exception as exc:  # noqa: BLE001 - a session-registration failure must not block monitoring
        print(f"(could not register this session: {exc})", file=sys.stderr)
    try:
        for line in target.watch_events(mine=author, client_session=client_session, author=author):
            print(line)
            if line.startswith("DECS-DONE") or getattr(args, "once", False):
                return 0
    except KeyboardInterrupt:
        return 0
    return 0


def _cloud(args, target, verb):
    """The dispatch for the four verbs (``get``/``show``/``tree``/
    ``search``) that branch on ``mode == "local"`` at the call site
    instead of going through :func:`_resolve_local` — ``get`` needs no
    ref-then-verb split (it IS the resolve), and ``show``/``tree``/
    ``search`` take their own argument shapes. ``target`` is always a
    ``task_cloud.CloudStore`` here (the ``mode == "local"`` branch never
    reaches this function)."""
    if verb == "get":
        return target.resolve(args.ref)
    if verb == "show":
        return target.show(args.scope, args.stale)
    if verb == "tree":
        return target.tree(args.ref, include_archived=args.all)
    if verb == "search":
        return target.search(args.words, include_archived=args.all)
    from formist_cli import task_cloud

    raise task_cloud.CloudNotAvailable(verb)


_VERBS = {
    "init": _cmd_init, "config": _cmd_config, "whoami": _cmd_whoami,
    "add": _cmd_add, "get": _cmd_get, "show": _cmd_show, "views": _cmd_views, "view": _cmd_view,
    "tree": _cmd_tree, "search": _cmd_search,
    "start": _cmd_start, "done": _cmd_done, "close": _cmd_close, "archive": _cmd_archive,
    "defer": _cmd_defer, "undefer": _cmd_undefer, "retitle": _cmd_retitle, "move": _cmd_move,
    "supersede": _cmd_supersede,
    "assign": _cmd_assign, "flag": _cmd_flag, "after": _cmd_after, "order": _cmd_order,
    "ask": _cmd_ask, "push": _cmd_push,
    "read": _cmd_read, "queue": _cmd_queue, "note": _cmd_note, "edit": _cmd_edit,
    "progress": _cmd_progress, "steps": _cmd_steps, "step": _cmd_step, "beat": _cmd_beat,
    "attach": _cmd_attach, "approved": _cmd_approved, "claim": _cmd_claim, "expand": _cmd_expand,
    "briefs": _cmd_briefs, "check": _cmd_check, "changes": _cmd_changes, "monitor": _cmd_monitor,
    "wait": _cmd_wait, "watch": _cmd_watch, "tell": _cmd_tell, "mirror": _cmd_mirror,
    "plugin-session-start": _cmd_plugin_session_start, "plugin-prompt-refs": _cmd_plugin_prompt_refs,
}
