"""Node refs: ``<source>:<letter><number>`` (or the local short form)."""

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
    source: str | None = None

    @property
    def project(self) -> str | None:
        """Compatibility name for callers predating source-qualified refs.

        A source is the thing which owns an id namespace.  Today that is a
        local decisions project, and later it can be a mirrored cloud
        section.  Keep the old property while callers move to the less
        misleading name.
        """
        return self.source


class RefError(ValueError):
    pass


def parse_ref(raw: str) -> ParsedRef:
    """Accepted forms: ``305 W305 w305 D305 W:305 web:305 web:C305``.

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

    if head is not None and re.match(r"^[A-Z]$", head) and not letter:
        # Preserve the older ``W:305`` spelling for a letter-qualified local
        # reference.  When the tail has its own letter, ``W:C305`` is an
        # unambiguous source-qualified reference instead.
        return ParsedRef(id=id_, letter=head)
    if head:
        # The source selects the id namespace; the letter is display-only.
        # Deliberately retain it even when stale so resolution can find by
        # number and callers can print the canonical letter as a correction.
        return ParsedRef(id=id_, letter=letter, source=head)
    if letter:
        return ParsedRef(id=id_, letter=letter)
    return ParsedRef(id=id_)
