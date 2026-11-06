"""Node refs: ``<letter><number>`` — a port of noddit's ``core/ref.ts``."""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_LETTER = "X"

_REF_RE = re.compile(r"^([A-Za-z])?(\d+)$")


def format_ref(id_: int, letter: str | None = None) -> str:
    return f"{letter or DEFAULT_LETTER}{id_}"


@dataclass(frozen=True)
class ParsedRef:
    id: int
    letter: str | None = None
    project: str | None = None


class RefError(ValueError):
    pass


def parse_ref(raw: str) -> ParsedRef:
    """Accepted forms: ``305 W305 w305 D305 W:305 web:305 web:D305``.

    Ported byte-for-byte including one asymmetry noted in the C1052 research
    spec: the "head is a project letter" check tests the RAW head against
    ``^[A-Z]$`` (case-sensitive), so a lowercase letter immediately before a
    colon (``w:305``) is read as a project NAME, not the letter W — even
    though a bare ``w305`` (no colon) resolves to letter ``W``. Replicated
    as written, not "fixed".
    """
    s = raw.strip()
    colon = s.rfind(":")
    head = s[:colon] if colon != -1 else None
    tail = s[colon + 1:] if colon != -1 else s

    m = _REF_RE.match(tail)
    if not m:
        raise RefError(f"not a node number: {raw!r}")
    id_ = int(m.group(2))
    letter = m.group(1).upper() if m.group(1) else None

    if head is not None and re.match(r"^[A-Z]$", head):
        if letter and letter != "D" and letter != head:
            raise RefError(f"two different letters in ref: {raw!r}")
        return ParsedRef(id=id_, letter=head)
    if head:
        if letter and letter != "D":
            raise RefError(f"a project name and a letter don't mix: {raw!r}")
        return ParsedRef(id=id_, project=head)
    if letter:
        return ParsedRef(id=id_, letter=letter)
    return ParsedRef(id=id_)
