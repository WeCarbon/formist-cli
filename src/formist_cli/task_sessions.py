"""Machine-local, per-session bookkeeping in ``$NODDIT_HOME/session-nodes.json``
— a port of noddit's ``core/session-nodes.ts``, LOCAL MODE ONLY (cloud
mode's equivalent is the server's ``agent_session`` row, already wired
through ``task_cloud.CloudStore.register_session``'s own ``current``
field — see C1141).

Two of noddit's three maps, not all three (a documented gap, not an
oversight):

- ``current``: the node this session last ran ``fm task start``/
  ``claim`` on — used by :func:`current_node` so a caller command can
  fall back to it when no ref is given.
- ``authors``: the author this session last signed as, explicitly with
  ``--author`` or by starting a node under one, so a later write that
  gives neither ``--author`` nor ``NODDIT_AUTHOR`` still signs the same
  way (:func:`remembered_author`, wired into ``tasks._author``'s own
  fallback chain, right where noddit's own N267 note says it sits:
  "between an explicit --author/NODDIT_AUTHOR and the branch-name
  guess" — fm never had a branch-name guess to begin with).

**Not ported**: noddit's third map, ``starters`` (which session started
each node, keyed by ``<root>#<id>``, feeding ``nod tidy``'s orphan
detection) — ``tidy`` itself is out of scope (``task_store.py``'s own
module docstring), so there is nothing for ``starters`` to serve here.

**``session_key`` is a simplified ``sessionKey()``**: noddit's own falls
back to a process-tree walk (``ps``/PowerShell CIM, ``core/session-
process.ts``) when neither an explicit identity nor
``CLAUDE_CODE_SESSION_ID`` is set — genuinely platform-specific plumbing
for a tier that, in `fm`'s primary context (running under Claude Code,
which always sets that env var), is rarely the one actually reached.
Not ported (gap): outside any harness and with no ``NODDIT_IDENTITY``
pinned, this session has no key, matching noddit's own "the owner's
bare terminal has no current node" by coincidence rather than design —
TODO if a non-Claude-Code harness ever needs this tier.

**Why the CLI doesn't let every verb omit its ref**: making ``ref`` an
optional positional (``nargs="?"``) is only unambiguous for a verb
whose OTHER positional, if any, is itself required with no further
optional slot after it (``beat``'s lone ``ref``, ``step``'s ``ref`` +
required ``n``, ``note``'s ``ref`` + required ``text``) — argparse's own
positional matching (a regex over argument COUNTS, resolved before any
type conversion) backtracks correctly for exactly one optional slot
sitting next to one required slot, but NOT when a second optional slot
follows (``progress``'s ``ref? pct msg?``, ``steps``'s ``ref?
labels+``): with the ref omitted and enough plain arguments to fill
every slot, it greedily assigns the first argument to ``ref`` instead,
silently misparsing rather than falling back. ``tasks.py`` wires the
omission only onto ``beat``/``step``/``note``; ``progress``/``steps``
still require an explicit ref (a documented gap, not an inconsistency —
see the comment at each of their parsers).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

from formist_cli.task_identity import current_identity
from formist_cli.task_lock import with_file_lock, write_file_atomic

#: Matches noddit's own KEEP_MS (14 days) — old entries for a session
#: that is long gone are pruned on the next write, not left to grow the
#: file forever.
KEEP_SECONDS = 14 * 86_400


def _noddit_env(name: str, env: dict | None = None) -> str | None:
    e = os.environ if env is None else env
    return e.get(f"NODDIT_{name}") or e.get(f"DECS_{name}")


def state_path(env: dict | None = None) -> str:
    explicit = _noddit_env("SESSION_NODES", env)
    if explicit:
        return explicit
    home = _noddit_env("HOME", env) or os.path.join(os.path.expanduser("~"), ".decs")
    return os.path.join(home, "session-nodes.json")


def _worktree_of(cwd: str) -> str | None:
    """This cwd's own worktree root (``git rev-parse --show-toplevel``) —
    distinct from ``task_paths.main_worktree``'s ``--git-common-dir``,
    which names the one shared ``.git`` every linked worktree points at;
    this names the worktree ITSELF, so two of them differ."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    return path or None


def _worktree_scope(env: dict, cwd: str) -> str | None:
    return _noddit_env("WORKTREE", env) or _worktree_of(cwd)


def session_key(env: dict | None = None, cwd: str | None = None) -> str | None:
    """Which session is asking — ``task_identity.current_identity``'s own
    two tiers (an explicit ``NODDIT_IDENTITY`` pin, else this agent's
    name plus ``CLAUDE_CODE_SESSION_ID``), scoped by worktree so a lead
    and a lane it spawned in the same harness process don't collide
    (noddit's own N280/N296: ``NODDIT_WORKTREE``/``DECS_WORKTREE`` names
    a lane's own worktree when ``cwd`` can't). ``None`` outside any
    session with no identity pinned — this session has no "current
    node" to remember or recall."""
    e = os.environ if env is None else env
    identity = current_identity(e)
    if not identity:
        return None
    scope = _worktree_scope(e, cwd or os.getcwd())
    return f"{identity}@{scope}" if scope else identity


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _parse_epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    current = raw.get("current") if isinstance(raw.get("current"), dict) else {}
    authors = raw.get("authors") if isinstance(raw.get("authors"), dict) else {}
    return {"current": current, "authors": authors}


def _prune(mapping: dict, now: float) -> dict:
    out = {}
    for key, value in mapping.items():
        if not isinstance(value, dict):
            continue
        at = _parse_epoch(value.get("at"))
        if at is None or now - at < KEEP_SECONDS:
            out[key] = value
    return out


def _update(fn, env: dict | None = None) -> None:
    path = state_path(env)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def _do() -> None:
            state = _read(path)
            fn(state)
            now = time.time()
            write_file_atomic(path, json.dumps({
                "current": _prune(state["current"], now),
                "authors": _prune(state["authors"], now),
            }, ensure_ascii=False))

        with_file_lock(f"{path}.lock", _do)
    except OSError:
        pass  # a convenience: an unwritable $NODDIT_HOME only means refs/author have to be spelled out


def record_started(root: str, id_: int, ref: str, author: str | None = None,
                    env: dict | None = None, cwd: str | None = None) -> bool:
    """``fm task start``/``claim``: this session is now on ``ref``.
    Returns whether there was a session to record it against (``False``
    outside any session — the caller still did the start/claim; this is
    purely the "remember it for next time" side effect)."""
    key = session_key(env, cwd)
    at = _now_iso()

    def _set(state: dict) -> None:
        if key:
            state["current"][key] = {"root": root, "id": id_, "ref": ref, "at": at}

    _update(_set, env)
    if author:
        record_author(author, env=env, cwd=cwd)
    return bool(key)


def current_node(env: dict | None = None, cwd: str | None = None) -> dict | None:
    """The node this session last started, if any — ``{root, id, ref,
    at}``, or ``None`` with no session or nothing recorded yet."""
    key = session_key(env, cwd)
    if not key:
        return None
    return _read(state_path(env))["current"].get(key)


def record_author(author: str, env: dict | None = None, cwd: str | None = None) -> bool:
    key = session_key(env, cwd)
    if not key:
        return False
    at = _now_iso()

    def _set(state: dict) -> None:
        state["authors"][key] = {"author": author, "at": at}

    _update(_set, env)
    return True


def remembered_author(env: dict | None = None, cwd: str | None = None) -> str | None:
    """This session's remembered author, if any — between an explicit
    ``--author``/``NODDIT_AUTHOR`` and giving up (noddit's own N267;
    fm never had a branch-name guess after it to fall further to)."""
    key = session_key(env, cwd)
    if not key:
        return None
    entry = _read(state_path(env))["authors"].get(key)
    return entry.get("author") if isinstance(entry, dict) else None
