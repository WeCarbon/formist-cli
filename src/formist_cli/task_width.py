"""East-Asian-width-aware column counting — a stdlib stand-in for the npm
``string-width`` package noddit uses (``core/title.ts``, ``core/intake.ts``,
``core/order.ts``'s ``WAIT_ON_MAX_COLS``).

``unicodedata.east_asian_width`` is the stdlib's own classification (the
same Unicode East Asian Width property the npm package reads): ``W``
(wide) and ``F`` (fullwidth) count as 2 display columns, everything else
as 1. Zero-width marks (combining characters) are not specially handled,
since none of the user-facing strings this tool widths (titles, tags,
``waiting_on`` text) are expected to contain them.
"""

from __future__ import annotations

import unicodedata

_WIDE = ("W", "F")


def string_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in _WIDE else 1 for ch in s)
