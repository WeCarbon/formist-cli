"""The literal tokens ``decisions/*.md`` is written in — a port of noddit's
``core/format.ts``.

These are part of the FILE FORMAT, not UI text: they stay byte-identical
whatever language a CLI speaks, so a repo written by a mixed ``nod``/``fm``
team keeps parsing either way (contract §6.3). Display text for `fm`'s own
``--help`` output is free to be English; these constants are not display
text, they are what gets written to disk.
"""

from __future__ import annotations

HEADING = {
    "options": "选项",
    "attachments": "附件",
    "questions": "子问题",
}


def options_heading(round_: int = 1) -> str:
    return f"{HEADING['options']}（第 {round_} 轮）" if round_ > 1 else HEADING["options"]


def questions_heading(round_: int = 1) -> str:
    return f"{HEADING['questions']}（第 {round_} 轮）" if round_ > 1 else HEADING["questions"]


NOTE_KIND = {
    "answered": "已答",
    "follow_up": "追问",
    "reply": "回复",
    "addendum": "补充",
    "seen": "已看过",
    "log": "执行记录",
    "waiting": "等待",
    "deferred": "延迟",
    "undeferred": "解除延迟",
    "completed": "完成",
    "superseded": "被取代",
    "new_question": "新问题",
    "original": "原话",
    "claimed": "接活",
    "heartbeat": "心跳",
    "received": "已收到",
    "reminded": "已提醒",
    "partly_answered": "部分已答",
    "ran": "已执行",
    "condensed": "整理",
    "sent": "已发送",
}


def note_kind_key(kind: str) -> str | None:
    for key, value in NOTE_KIND.items():
        if value == kind:
            return key
    return None


def is_answer_kind(kind: str) -> bool:
    return kind == NOTE_KIND["answered"] or kind == NOTE_KIND["seen"]


OPTION_LABEL = {
    "accept": "验收通过",
    "fyi": "知道了",
    "review_pass": "通过",
    "review_fail": "不通过",
    "review_change": "改（答复里写改法）",
    "other": "其他",
}


def all_defaults_label(defaults: str) -> str:
    return f"全部按默认（{defaults}）"


def item_by_item_label(keys: str) -> str:
    return f"逐条回复（{keys}）"


DEFAULT_MARK = "（默认）"

ATTACHMENT_GROUP_END = "<!-- /分组 -->"

#: ``nod add``'s auto-generated single option when no ``-o``/``--task``/
#: ``--fyi``/``--for``/``--milestone``/``--brief`` was given: the
#: "done, take a look" acceptance shape.
ACCEPTANCE_OPTION = {"key": "1", "label": OPTION_LABEL["accept"]}
FYI_OPTION = {"key": "1", "label": OPTION_LABEL["fyi"]}
