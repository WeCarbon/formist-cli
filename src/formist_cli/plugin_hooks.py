"""The testable half of `fm`'s Claude Code plugin hooks (C1053/C1139): a
port of noddit's own ``plugin/session-start.mjs`` and
``plugin/prompt-refs.mjs`` (``~/Code/decisions-md``).

The thin entry points Claude Code actually invokes
(``plugin/session_start.py``, ``plugin/prompt_refs.py`` at the repo root,
outside this package, matching where the plugin manifests point) do the
stdin/stdout JSON dance and the ``fm`` subprocess call, then hand off to
the pure functions here — the same split noddit's own hooks keep between
a thin ``.mjs`` script and ``src/core/*.ts``, for the same reason: a
function that reads argv/stdin and prints to stdout cannot be unit
tested without a subprocess, a function that transforms already-parsed
data can.

**What this plugin does NOT port** (documented gaps, not oversights):

- noddit's "main session" (``core/main-session.ts``): a session that
  belongs to no project, watches every REGISTERED project, and owns
  ``nod tell``'s inbox. `fm` has no multi-project registry at all — one
  process is always scoped to one ``--dir`` or one cloud space — so
  there is no "watches everything" role to port. Contract's own model
  (C1130) is simpler: one main agent PER SPACE, which is just the
  ordinary case below, not a separate role.
- ``nod session start``/``nod session goto`` (opening a new terminal
  window running an agent, switching an existing one to the
  foreground): a convenience for noddit's own "I have a dozen projects
  open at once" workflow, not something the brief asked to port, and
  genuinely platform-specific (AppleScript/iTerm2 on macOS, Windows
  Terminal, a Linux terminal search) in a way the rest of `fm` never
  needs to be.
- ``NODDIT-DAYDREAM`` (the nightly compaction pass): depends on
  ``nod compact``/``nod daydream``, both already out of scope (see
  ``task_store.py``'s own module docstring).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

#: noddit's own filter (session-start.mjs): a node still worth a resumed
#: session's attention. Matches contract's seven states minus the two
#: terminal ones (completed, closed) and minus "waiting" (not actionable
#: right now by definition).
OPEN_STATUSES = frozenset({"not_started", "in_progress", "needs_decision", "decided"})

#: noddit's own cap (session-start.mjs: ``open.slice(0, 30)``) — the
#: SessionStart hook's ``additionalContext`` has a hard ceiling (10,000
#: characters) the harness enforces, so a long list is capped rather than
#: truncated mid-line.
MAX_OPEN_NODES_SHOWN = 30

#: SessionStart's rules block (noddit's own ``plugin/rules.md``, ported
#: and renamed to `fm task`; a plain Python string, not a packaged data
#: file, so there is nothing for a wheel build to forget to include).
#: English only — this plugin does not localize (see the module
#: docstring).
RULES_TEXT = """\
## fm task: this space's (or this decisions/ directory's) work lives in nodes (MUST FOLLOW)

The owner reads nodes, not chat — work not recorded as a node is invisible to
them. Every `fm task <verb>` prints its next step; `fm task <verb> --help`
for the exact flags.

- **The owner's briefs come first**: `fm task briefs` lists work the owner
  filed — `fm task claim <ref>` it and follow what it prints, instead of
  filing your own node for the same thing.
- **Record work as nodes before doing it.** A task and a decision are the
  same thing: `fm task add "<short title>" --task --author "<you> · <doing
  what>"`, then `fm task start <ref>`. New work found mid-task gets its own
  node too, not just a chat mention.
- **Need the owner to decide**: `fm task ask <ref> --option 1=... --option
  2=...`, with your recommendation in the body.
- **Whoever finishes a node closes it**: `fm task done <ref> --note
  "<result, commit>"` right away once the work and its gate both pass.
- **Relay what the owner says in chat onto the node it is about** before
  replying — `fm task note <ref> "<their words>" --human` — rather than
  letting it live only in the chat transcript.

## Getting notified: fm task monitor (MUST FOLLOW)

- The main session for this project/space runs `fm task monitor --here`
  (the Claude Code plugin starts this for you automatically — never start
  it yourself); it never goes offline on its own, only when the session
  does.
- A sub-agent or teammate working on its own slice watches only its own
  nodes: `fm task monitor --mine` (name before the `·` in your `--author`).
  Arm it as soon as you start, not only once you are waiting on a reply;
  re-arm it unchanged on expiry only while you still have open nodes.
  `DECS-DONE` ("nothing of yours is open") means stop, not re-arm.
- Each monitor line is one thing to act on, ending with its own next step.
"""

#: A ref is either `<LETTER><digits>` or a source-qualified
#: `<source>:<LETTER?><digits>`.  The latter is unambiguous even without a
#: letter, and must reach the prompt reminder so a cross-source operation is
#: recorded against the right node.  This stays a text scan rather than
#: importing ``task_ref``: hooks run before a local source is known.
_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:(?:[A-Za-z0-9][A-Za-z0-9_-]*:)([A-Z]?)(\d{1,5})|([A-Z])(\d{1,5}))(?![A-Za-z0-9])"
)


def run_fm_json(args: list[str], *, timeout: float = 8.0) -> object | None:
    """Run ``fm <args> --json`` and return the parsed result, or ``None``
    on anything going wrong (not a project here, fm not on PATH, a
    timeout, bad JSON) — same shape as noddit's own ``session-start.mjs``
    try/catch around ``execFileSync``: the rules text alone still applies
    when there is nothing to add to it."""
    try:
        raw = subprocess.run(
            ["fm", *args, "--json"], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if raw.returncode != 0 or not raw.stdout.strip():
        return None
    try:
        return json.loads(raw.stdout)
    except json.JSONDecodeError:
        return None


def open_nodes_from_tree(tree_json: object) -> list[dict]:
    """Filter ``fm task tree --json``'s flat node list down to the ones
    worth telling a resumed session about — :data:`OPEN_STATUSES`,
    oldest-created first isn't required (unlike noddit, which reads
    ``decisions/`` in id order already); sorted by ``created`` for a
    stable, readable order."""
    if not isinstance(tree_json, list):
        return []
    open_nodes = [n for n in tree_json if isinstance(n, dict) and n.get("status") in OPEN_STATUSES]
    open_nodes.sort(key=lambda n: n.get("created") or "")
    return open_nodes


def format_open_nodes_block(nodes: list[dict], *, heading: str = "## Open work") -> str:
    """The context block session-start.mjs builds: one line per node
    (ref, status, title, author), capped, with a "N more" tail."""
    if not nodes:
        return ""
    shown = nodes[:MAX_OPEN_NODES_SHOWN]
    lines = [f"{n.get('ref', '?')} · {n.get('status', '?')} · {n.get('title', '')} — {n.get('author', '')}"
              for n in shown]
    more = len(nodes) - len(shown)
    tail = f"\n...and {more} more open." if more > 0 else ""
    return f"\n\n{heading}\n\n" + "\n".join(lines) + tail + "\n"


def build_session_start_context(rules_text: str, tree_json: object) -> str:
    """The full SessionStart ``additionalContext``: the rules, plus the
    open-work block when there is one to show (no project here, or a
    clean one with nothing open, means the block is simply absent —
    exactly noddit's own behavior, not an error state)."""
    text = rules_text
    nodes = open_nodes_from_tree(tree_json) if tree_json is not None else []
    text += format_open_nodes_block(nodes)
    return text


def known_letters_for(cwd: str | None) -> str:
    """``X``/``D`` (no-letter default / legacy) plus this LOCAL project's
    own letter, read straight from ``decisions/decs.json`` walking up
    from ``cwd`` — matching prompt-refs.mjs's own ``projectLetter``.

    Cloud mode has no equivalent to walk to (a space's letter isn't
    discoverable from a directory at all), so :func:`detect_refs` is
    given an empty string here and widens its own rule instead of
    guessing a letter that isn't there."""
    import os

    d = cwd or os.getcwd()
    while True:
        try:
            with open(os.path.join(d, "decisions", "decs.json"), encoding="utf-8") as fh:
                letter = json.load(fh).get("letter")
            if letter:
                return f"XD{str(letter).upper()}"
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(d)
        if parent == d:
            return "XD"
        d = parent


def detect_refs(prompt: str, known_letters: str) -> list[str]:
    """Node refs named in the owner's words — prompt-refs.mjs's own rule:
    a single digit needs a KNOWN letter (``Q1``/``F1`` are common prose
    that are not refs), 2+ digits are accepted under any single
    uppercase letter. ``known_letters`` empty (cloud mode, no local
    ``decisions/`` to read a letter from) widens this to "any letter",
    since there is no local signal to narrow it with and a false
    positive here costs one reminder line, not a wrong write."""
    seen: list[str] = []
    for m in _REF_RE.finditer(prompt):
        scoped = m.group(2) is not None
        letter = m.group(1) if scoped else m.group(3)
        digits = m.group(2) if scoped else m.group(4)
        if scoped or len(digits) > 1 or not known_letters or letter in known_letters:
            ref = m.group(0)
            if ref not in seen:
                seen.append(ref)
    return seen


def build_prompt_refs_reminder(refs: list[str]) -> str:
    """The UserPromptSubmit ``additionalContext`` when the owner's
    message names at least one node — prompt-refs.mjs's own reminder,
    in English (this plugin does not localize; see the module
    docstring)."""
    joined = " ".join(refs)
    return (
        f"The owner's message mentions {joined}. Before replying, record what "
        f"they said on that node (`fm task note {refs[0]} \"<their words>\" --human`), "
        "so it isn't lost to the chat transcript."
    )


@dataclass(frozen=True)
class SessionStartResult:
    additional_context: str

    def to_hook_output(self) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": self.additional_context}}


@dataclass(frozen=True)
class PromptSubmitResult:
    additional_context: str | None

    def to_hook_output(self) -> dict | None:
        if self.additional_context is None:
            return None
        return {
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": self.additional_context},
        }
