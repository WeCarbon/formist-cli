"""Who does a node — a port of noddit's ``core/assignee.ts``."""

from __future__ import annotations

HUMAN = "human"

_ME_WORDS = {"me", "human", "owner", "person", "moi", "我", "人"}
_AI_WORDS = {"ai", "agent", "bot", "ia", "机器", "機器"}
_MILESTONE_WORDS = {"milestone", "goal", "里程碑", "目标"}


def is_human_task(assignee: str | None) -> bool:
    return bool(assignee) and assignee != "ai"


def is_milestone(milestone: bool) -> bool:
    return bool(milestone)


class AssigneeError(ValueError):
    pass


def assignee_arg(raw: str) -> str | None:
    v = raw.strip().lower()
    if v in _ME_WORDS:
        return HUMAN
    if v in _AI_WORDS:
        return None
    raise AssigneeError(f"not me/ai/milestone: {raw!r}")


def is_milestone_word(raw: str) -> bool:
    return raw.strip().lower() in _MILESTONE_WORDS
