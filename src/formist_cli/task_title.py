"""Title width rule — a port of noddit's ``core/title.ts``."""

from __future__ import annotations

from formist_cli.task_width import string_width

TITLE_MAX_COLS = 32
WAIT_ON_MAX_COLS = 30


class TitleTooLong(ValueError):
    def __init__(self, title: str, width: int, max_cols: int):
        super().__init__(
            f"title is {width} columns wide, max is {max_cols} "
            f"(about half that many characters if it's mostly CJK): {title!r}"
        )
        self.title = title
        self.width = width
        self.max_cols = max_cols


def title_width(title: str) -> int:
    return string_width(title.strip())


def assert_title_fits(title: str, max_cols: int = TITLE_MAX_COLS) -> None:
    w = title_width(title)
    if w > max_cols:
        raise TitleTooLong(title.strip(), w, max_cols)
