"""Who is writing right now — a port of noddit's ``core/identity.ts``.

Stamped as the invisible ``<!-- nod:by=... -->`` marker on every write a
local-mode ``fm task`` command makes, exactly as ``nod`` stamps it, so a
file either tool writes still shows the SAME marker shape and ``nod
monitor``/``fm task monitor`` can tell their own writes from everyone
else's (never echoing a relayed reply back to whoever relayed it).
"""

from __future__ import annotations

import os


def _noddit_env(suffix: str, env: dict | None = None) -> str | None:
    """``NODDIT_<suffix>``, falling back to the older ``DECS_<suffix>`` name."""
    e = os.environ if env is None else env
    return e.get(f"NODDIT_{suffix}") or e.get(f"DECS_{suffix}")


def current_session(env: dict | None = None) -> str | None:
    e = os.environ if env is None else env
    return e.get("CLAUDE_CODE_SESSION_ID") or e.get("CLAUDE_SESSION_ID") or _noddit_env("SESSION", e)


def agent_name_of(author: str | None) -> str | None:
    if not author:
        return None
    import re

    name = re.split(r"[ \t]*[·・][ \t]*", author, maxsplit=1)[0].strip()
    return name or None


def current_identity(env: dict | None = None) -> str | None:
    e = os.environ if env is None else env
    explicit = _noddit_env("IDENTITY", e)
    if explicit:
        return explicit
    name = agent_name_of(_noddit_env("AUTHOR", e))
    session = current_session(e)
    if name and session:
        return f"{name}#{session}"
    return name or session


def current_author(env: dict | None = None) -> str | None:
    """``NODDIT_AUTHOR``/``DECS_AUTHOR`` verbatim — the ``"<agent> · <doing what>"`` string."""
    return _noddit_env("AUTHOR", env)


def is_agent_shell(env: dict | None = None) -> bool:
    """Whether this process counts as an agent by default (``nod note``'s
    role-detection rule): Claude Code's own shell, or ``NODDIT_ROLE=agent``.
    ``NODDIT_ROLE=human`` forces human.
    """
    e = os.environ if env is None else env
    role = _noddit_env("ROLE", e)
    if role == "human":
        return False
    if role == "agent":
        return True
    return bool(e.get("CLAUDECODE"))
