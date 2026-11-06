"""``fm task <verb>``: parity with every `nod` command (contract §6.2/§6.3).

Mode resolution (contract §6.3), printed on the first line of every
command's output (or as ``"mode"`` in ``--json``):

- ``--local`` forces local mode.
- ``--host <name>`` (or a stored credential for the default host) asks for
  remote mode — **not wired to a live server yet**: the server side
  (C1048, ``feat/noddit-tasks``) hasn't landed the ``/projects/{id}/nodes``
  router this would call. ``task_cloud`` implements the client against
  the contract so it is ready the day that router exists; today it
  raises a clear "not available yet" error rather than guessing at a
  shape.
- Otherwise, local mode when a ``decisions/`` folder is found upward from
  the cwd (the same lookup `nod` does, including the git-worktree
  main-checkout hop).
- Otherwise: refuse with a hint (`fm task init` or `fm login`). Modes
  never fall back into each other silently.
"""

from __future__ import annotations

import argparse
import json
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

        return "cloud", task_cloud.CloudTarget(host=host or remembered)
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
    raise TasksError('who is writing this? pass --author "<agent> · <doing what>", or set NODDIT_AUTHOR.')


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
    from formist_cli.cli import _add_env  # lazy: avoids a cli<->tasks import cycle

    _add_env(p, for_write=write)
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
    p.add_argument("view", nargs="?", default="available")
    _add_common(p)

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
    p.add_argument("ref")
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
    p.add_argument("ref")
    p.add_argument("pct", type=int)
    p.add_argument("msg", nargs="?")
    _add_common(p)

    p = verbs.add_parser("steps", help="define a node's steps")
    p.add_argument("ref")
    p.add_argument("labels", nargs="+")
    _add_common(p)

    p = verbs.add_parser("step", help="step n done -> progress n/total")
    p.add_argument("ref")
    p.add_argument("n", type=int)
    p.add_argument("--note")
    _add_common(p, write=True)

    p = verbs.add_parser("beat", help="still on it (silent unless --note)")
    p.add_argument("ref")
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

    p = verbs.add_parser("changes", help="everything after --since")
    p.add_argument("--since", required=True)
    _add_common(p)

    p = verbs.add_parser("monitor", help="one line per event (for an agent's Monitor tool)")
    p.add_argument("--since")
    p.add_argument("--mine", action="store_true", help="only this agent's own nodes (name before ·)")
    p.add_argument("--author", help="only one agent's own nodes, by name")
    p.add_argument("--idle-exit", type=float, default=7200,
                    help="go offline after this many seconds with no event, 0 = never (default 7200)")
    p.add_argument("--once", action="store_true", help="print one pass and exit (fm's own test/CI hook)")
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

    mode, target = resolve_mode(args)
    if mode != "local":
        from formist_cli import task_cloud

        return task_cloud.add(target, args)
    author = _author(args)
    options = _parse_options(args.option)
    assignee = assignee_arg(args.for_) if args.for_ else None
    reversible = {"yes": True, "no": False}.get(args.reversible)
    node = target.create(
        args.title, author, body=_read_body(args), options=options or None, default=args.default,
        task=args.task, fyi=args.fyi, tags=args.tag, urgency=args.urgency, due=args.due,
        parent=args.parent, after=args.after, est=args.est, assignee=assignee,
        milestone=args.milestone or False, intake=args.brief, remind_at=args.remind,
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
    nodes = target.show(args.view) if mode == "local" else _cloud(args, target, "show")
    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in nodes], indent=2, ensure_ascii=False, default=str))
        return 0
    from formist_cli.task_ref import format_ref

    for n in nodes:
        print(f"{format_ref(n.id, n.letter)}  {n.state:<14} {n.title}")
    return 0


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
    """Resolve ``ref`` and, in the same breath, enforce that this verb only
    runs in local mode — most verbs below don't yet have a cloud-mode
    implementation to branch into (C1048 hasn't landed), so this is the one
    place that refusal needs stating rather than 18 copies of it."""
    if not isinstance(target, task_store.LocalStore):
        raise TasksError(
            "this `fm task` verb is local-mode only until the server's node "
            "router exists (C1048) — pass --local, or run it from a decisions/ "
            "directory with no `fm use` default set."
        )
    return target.resolve(ref)


def _cmd_start(args) -> int:
    mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
    result = target.start(node.id, force=args.force) if mode == "local" else _cloud(args, target, "start")
    _print_node(result, args.json)
    return 0


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
        result = target.hold(node.id, args.on, author, role, until=args.until)
    else:
        result = target.defer(node.id, author, role, until=args.until, owner_said=args.owner_said)
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
    if args.flag == "important":
        result = target.flag(node.id, important=True, due=args.due)
    elif args.flag == "milestone":
        result = target.flag(node.id, milestone=True, due=args.due)
    else:
        result = target.flag(node.id, important=False, milestone=False, due=args.due)
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
        raise TasksError("`fm task queue` is local-mode only for now")
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
    node = _resolve_local(target, args.ref)
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
    node = _resolve_local(target, args.ref)
    result = target.mark_step(node.id, args.n, note_text=args.note, author=_author_opt(args))
    _print_node(result, args.json)
    return 0


def _cmd_beat(args) -> int:
    _mode, target = resolve_mode(args)
    node = _resolve_local(target, args.ref)
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
    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task briefs` is local-mode only for now")
    nodes = target.briefs(include_claimed=args.all)
    if args.json:
        from formist_cli import task_json

        print(json.dumps([task_json.to_json(n) for n in nodes], indent=2, ensure_ascii=False, default=str))
        return 0
    from formist_cli.task_ref import format_ref

    for n in nodes:
        print(f"{format_ref(n.id, n.letter)}  {n.title}")
    return 0


def _cmd_changes(args) -> int:
    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task changes` is local-mode only for now")
    events = target.changes(args.since)
    if args.json:
        print(json.dumps({"since": args.since, "events": events}, indent=2, ensure_ascii=False))
        return 0
    for e in events:
        print(f"{e['at']}  {e['ref']}  {e.get('kind')}")
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


def _cmd_monitor(args) -> int:
    import time as _time

    from formist_cli import task_monitor

    mode, target = resolve_mode(args)
    if mode != "local":
        raise TasksError("`fm task monitor` is local-mode only for now")
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
        raise TasksError("`fm task wait` is local-mode only for now")
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
        raise TasksError("`fm task watch` is local-mode only for now")
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


def _now_iso() -> str:
    from formist_cli.task_store import now_stamp

    return now_stamp(ms=True)


def _cloud(args, target, verb):
    from formist_cli import task_cloud

    raise task_cloud.CloudNotAvailable(verb)


_VERBS = {
    "init": _cmd_init, "config": _cmd_config, "whoami": _cmd_whoami,
    "add": _cmd_add, "get": _cmd_get, "show": _cmd_show, "tree": _cmd_tree, "search": _cmd_search,
    "start": _cmd_start, "done": _cmd_done, "close": _cmd_close, "archive": _cmd_archive,
    "defer": _cmd_defer, "undefer": _cmd_undefer, "retitle": _cmd_retitle, "move": _cmd_move,
    "supersede": _cmd_supersede,
    "assign": _cmd_assign, "flag": _cmd_flag, "after": _cmd_after, "order": _cmd_order,
    "ask": _cmd_ask, "push": _cmd_push,
    "read": _cmd_read, "queue": _cmd_queue, "note": _cmd_note, "edit": _cmd_edit,
    "progress": _cmd_progress, "steps": _cmd_steps, "step": _cmd_step, "beat": _cmd_beat,
    "attach": _cmd_attach, "approved": _cmd_approved, "claim": _cmd_claim, "expand": _cmd_expand,
    "briefs": _cmd_briefs, "changes": _cmd_changes, "monitor": _cmd_monitor,
    "wait": _cmd_wait, "watch": _cmd_watch,
}
