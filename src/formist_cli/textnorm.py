"""The character tables behind every "is this the same text?" question in ``fm``.

WHY THE TABLES MOVED HERE AND THE RULES DID NOT
-------------------------------------------------
Two commands ask whether two pieces of text are the same, for different reasons
and under different rules:

* ``fm diff`` matches a source row against a database row, to decide a WRITE. Its
  rule is deliberately **case-sensitive** — "folding case is how a resolver picks
  the wrong row" (``evtool/diff.py::normalise_key``).
* ``fm ai-benchmark`` looks for an expected string inside a model's prose answer.
  Its rule **folds case**, because an answer that said "AL REEM BALLROOM" is not a
  different answer.

Those two rules must stay separate. What they share is not a rule at all, it is a
FACT about Unicode: which characters are a space, which are a hyphen, which are a
quote. That fact had two copies — ``scripts/cross_module_drift.py`` found them the
day the second one was written — and two copies of a character table drift the way
every other pair in this repo has: silently, because nothing compares them and
nothing breaks when they disagree.

⚠️ The tables below are **byte-identical to the ones ``evtool/diff.py`` carried**,
on purpose. ``normalise_key`` decides writes, so moving its tables must not change
its behaviour; anything a later caller needs in addition belongs in that caller,
declared separately, not smuggled in here. ``evtool/benchmark.py`` does exactly
that with its own invisible-character set.

Stdlib only — ``ev`` must add no runtime dependency.
"""

from __future__ import annotations

#: Unicode spaces mapped to U+0020. MEASURED, 2026-08-27 (``evtool/diff.py``): of
#: these 18 characters a bare ``.strip()`` already removes 16 — U+00A0 included,
#: the one that was actually in the data. The table is load-bearing for exactly
#: two, U+200B and U+FEFF, which are ``isspace()``-False, unmatched by ``\s``, and
#: left alone by NFKC. The other 16 stay because a list that holds only the two
#: surprising members reads like an accident and invites being "tidied".
SPACES = "               　​﻿"

#: Curly quotes and the prime, mapped to their straight equivalents.
QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"}

#: En/em/figure/non-breaking dashes and the minus sign, mapped to ``-``.
DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}
