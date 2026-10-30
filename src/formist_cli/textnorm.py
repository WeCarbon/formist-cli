"""Character tables used to normalise text before comparing it.

``fm diff`` and ``fm ai-benchmark`` both decide whether two pieces of
text are the same, under different rules: ``diff`` matches a source
row against a database row to decide a write, and is case-sensitive,
since folding case can match the wrong row. ``ai-benchmark`` looks for
an expected string inside a model's prose answer, and folds case. The
two commands keep separate comparison rules but share the same
Unicode facts: which characters are a space, a quote, or a dash.

Stdlib only: this module must not add a runtime dependency.
"""

from __future__ import annotations

#: Unicode spaces mapped to U+0020. A bare ``.strip()`` already removes
#: most of these; the table is load-bearing for U+200B and U+FEFF,
#: which are ``isspace()``-false, unmatched by ``\s``, and untouched by
#: NFKC.
SPACES = "               　​﻿"

#: Curly quotes and the prime, mapped to their straight equivalents.
QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"}

#: En/em/figure/non-breaking dashes and the minus sign, mapped to ``-``.
DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
