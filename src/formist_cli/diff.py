"""``fm diff`` — what to write. ``fm update`` already answers how.

Nothing in this toolchain answered "what does this environment need in order to
match the source of truth". On 2026-08-26/27 that gap was filled by hand twice and
by ~20 one-off scripts; ``scratchpad/parity/`` holds 199 files from a single
reconciliation. So this compares a SOURCE against a target environment and emits a
payload ``fm update`` consumes. The two compose: ``fm diff`` → read it → ``ev
update``.

THE OUTPUT NAMES ROWS BY NAME, NEVER BY ID
------------------------------------------
Ops carry ``$name``, which ``fm update`` re-resolves against the target seconds
before the write and copies into ``$expect`` so the row itself refuses a
mis-resolution (``evtool/update.py:196-215``). Emitting ids here would move the
transcribed-id failure one file earlier AND drop that copy-back. A target id never
appears in the output at all.

EVERY DEFECT LIVED IN THE COMPARISON, SO THE COMPARISON IS THE DESIGN
---------------------------------------------------------------------
Measured, all of them, on this data:

- **Trailing and non-breaking whitespace in the match key** — ``'ODA '``,
  ``'FAO '``, and a *name* with a trailing space. An untrimmed name is a phantom
  new row, not a visible duplicate: it creates silently. The characters that
  actually require the explicit table are the two ZERO-WIDTH ones; see
  :func:`normalise_key`, which measured it rather than reasoning about it.
- **Curly vs straight quotes, en-dash vs hyphen**, present in real names on both
  sides.
- **The same column holding two shapes.** ``notes`` is a bare JSON string in
  production and an ``{"en": …}`` map in development, and the cause is not random:
  the frontend writes a plain string while English is the only locale and a map
  once a second exists, while importers always write maps. A comparison that
  coalesces them calls them equal — which nearly converted 14 production rows.
  ``services/bulk_data.py:290-300`` refuses to coalesce for exactly this reason, so
  neither does this. And nothing is rendered through ``str()``: ``str({"en":
  "FAO"})`` is a dict repr and matches nothing at all.
- **Ids differ between environments** — every organisation id does. Matching is by
  name, and 0 or >1 matches is a refusal rather than a choice.
- **An id prefix is not a key.** An 8-character UUID prefix was used as a primary
  key over a table where a prefix collision has been measured.
- **A row missing from the source is never authority to delete.** Additive and
  updating only; what would have been deleted is reported.
- **Two measurements of one delta agreed on the creates and differed 4 vs 11 on
  the updates**, because "already equal" meant different things on each side. So
  the rule used for equality is PRINTED, per field, on every run. A disagreement
  then has somewhere to land.

Stdlib only — ``ev`` must add no runtime dependency, which is also why xlsx is
refused with a conversion hint rather than parsed by hand.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from formist_cli import resolve as resolve_mod
from formist_cli import textnorm
from formist_cli.targets import Target


class DiffError(Exception):
    """A comparison that will not be made, or a payload that will not be written."""


#: The resolver's remedy for a truncated page is "narrow the search". A diff has no
#: such option and must not be handed one: narrowing is what makes the unseen rows
#: look absent, and absent is what this tool reports as a would-delete. It is a
#: whole table or nothing — which is why these callers paginate, and why the only
#: honest remedy left once a paginated walk has still come up short is to stop.
_PARTIAL_REMEDY = (
    "A diff over a partial table is not a partial diff — every row the walk did not "
    "reach would be reported as a would-delete, so there is no narrowing that helps "
    "and nothing was compared.\n"
    "  This kind is larger than the walk's ceiling "
    f"({resolve_mod.MAX_PAGES} × {resolve_mod.MAX_PAGE} rows), or the server stopped "
    "returning rows before it reached the total it reported."
)


# ===========================================================================
# The two rules. They are different on purpose, and both are printed.
# ===========================================================================
# The three character tables — which characters ARE a space / a quote / a dash —
# used to live here. They now live in evtool/textnorm.py, byte-identical, because
# `fm ai-benchmark` needs the same FACT under a different RULE and a second copy
# of a character table drifts the way every other pair in this repo has: silently.
# What stays here is the rule, and it stays case-SENSITIVE. The tables are used
# through `textnorm.` below rather than re-bound to local names, so there is one
# definition and `scripts/cross_module_drift.py` can see that there is.

KEY_RULE = (
    "NFKC → Unicode spaces (incl. U+00A0 and the zero-width U+200B / U+FEFF, which "
    "no trim reaches) → U+0020 → collapse runs → trim → curly quotes → straight, "
    "en/em/non-breaking dashes → '-'; CASE-SENSITIVE"
)
VALUE_RULE = (
    "exact and SHAPE-SENSITIVE: a bare string never equals an {en,…} map, and no "
    "value is rendered through str()"
)


def normalise_key(value: str) -> str:
    """The MATCH key. Lenient, because its job is to find the row.

    The ``textnorm.SPACES`` pass runs BEFORE the trim, and that ordering is the only part
    of this function that is load-bearing rather than belt-and-braces. Measured
    rather than reasoned about, because the reason that first stood here was
    false: ``.strip()`` *does* remove U+00A0 (it is ``isspace()``), and NFKC maps
    it to U+0020 besides. What no generic step reaches is U+200B and U+FEFF —
    zero-width, ``isspace()``-False, unmatched by ``\\s``, unchanged by NFKC. They
    are also invisible on screen, so the phantom row they create is named
    identically to the row it duplicates.

    Case is NOT folded — folding case is how a resolver picks the wrong row
    (``evtool/resolve.py:283``), and this must agree with the resolver that
    ``fm update`` will use seconds later.
    """
    text = unicodedata.normalize("NFKC", value)
    for space in textnorm.SPACES:
        text = text.replace(space, " ")
    for src, dst in {**textnorm.QUOTES, **textnorm.DASHES}.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


def values_equal(left: Any, right: Any) -> tuple[bool, str]:
    """Are two column values the same value? ``(equal, why)``.

    STRICTER than the key rule, and deliberately not the same function. A key is
    matched so the row can be found; a value decides whether to write. Shape is
    part of a value: ``"FAO"`` and ``{"en": "FAO"}`` are different, they are the
    documented 14-row production case, and the endpoint's own ``$expect`` check
    refuses to coalesce them (``services/bulk_data.py:290-300``).
    """
    if type(left) is not type(right):
        if _shape_of(left) != _shape_of(right):
            # NULL is absence, not a shape. Filling an empty column is the most
            # ordinary update there is, and blocking it would have blocked most of
            # the real work — measured: the first live run against a real space classified
            # every fill-in as a shape change and emitted no op for any of them.
            # The reverse is NOT symmetric and stays blocked: a source null over a
            # populated column is a field-level delete, and "a row missing from the
            # source is never authority to delete" is this module's rule one level
            # down. Clearing a value takes --allow-shape-change, deliberately.
            if left is None:
                return False, "target holds no value"
            return False, f"shape differs: {_shape_of(left)} vs {_shape_of(right)}"
    if isinstance(left, str) and isinstance(right, str):
        if left == right:
            return True, "identical"
        if normalise_key(left) == normalise_key(right):
            return False, "differs only by whitespace/quote/dash normalisation"
        return False, "differs"
    return (True, "identical") if left == right else (False, "differs")


def _shape_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "map"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def key_strings(value: Any) -> list[str]:
    """Every normalised string a column value could be matched BY.

    Mirrors ``evtool/resolve.py``'s ``_strings_in`` so that a row this module
    matches is the row ``fm update`` will resolve. Two different notions of "the
    name of this row" in one pipeline is how a diff proposes an op that then
    refuses.
    """
    if isinstance(value, str):
        return [normalise_key(value)]
    if isinstance(value, dict):
        return [normalise_key(v) for v in value.values() if isinstance(v, str)]
    return []


# ===========================================================================
# Sources of truth
# ===========================================================================
@dataclass(frozen=True)
class Source:
    rows: list[dict]
    origin: str
    #: sha256 of the bytes compared, so a report can be tied to an input.
    digest: str
    fmt: str


def load_source(path: str) -> Source:
    """A JSON array / ``{"rows": [...]}`` or a CSV with a header row.

    xlsx is REFUSED rather than parsed. ``ev`` is stdlib-only by contract
    (``evtool/cli.py:24``), and a hand-rolled xlsx reader — shared strings, date
    serials, inline vs shared cells — is precisely the class of thing that reads
    almost right and is believed. The refusal names the conversion instead.
    """
    lower = path.lower()
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        raise DiffError(
            f"{path} is a spreadsheet, and fm refuses to parse one.\n"
            "`fm` is stdlib-only (evtool/cli.py:24) so an xlsx reader here would be "
            "hand-rolled — shared strings, date serials and inline-vs-shared cells "
            "are where such a reader is subtly wrong, and a subtly wrong source of "
            "truth is worse than none.\n"
            "\n"
            "→ convert it first, then pass the result:\n"
            "    python -c \"import openpyxl,json,sys; "
            "wb=openpyxl.load_workbook(sys.argv[1]); ws=wb.active; "
            "rows=list(ws.values); hdr=[str(h).strip() for h in rows[0]]; "
            "json.dump([dict(zip(hdr,r)) for r in rows[1:]], open(sys.argv[2],'w'), "
            'default=str)" book.xlsx rows.json'
        )

    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        raise DiffError(f"no such source file: {path}") from None
    except OSError as exc:
        raise DiffError(f"could not read {path}: {exc}") from None

    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")

    if lower.endswith(".csv"):
        reader = csv.DictReader(text.splitlines())
        rows = [dict(r) for r in reader]
        fmt = "csv"
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DiffError(f"{path} is not valid JSON: {exc}") from None
        if isinstance(payload, dict):
            payload = payload.get("rows")
        if not isinstance(payload, list):
            raise DiffError(
                f"{path} must hold a JSON array of row objects, or "
                '{"rows": [...]} — got a ' + type(payload).__name__ + "."
            )
        rows = [r for r in payload if isinstance(r, dict)]
        fmt = "json"

    if not rows:
        raise DiffError(f"{path} has no rows — nothing to compare.")
    return Source(rows=rows, origin=path, digest=digest, fmt=fmt)


def source_from_env(
    *, kind: str, target: Target, space: str, token: str | None, fetch=None
) -> Source:
    """Another environment as the source of truth.

    The rows come back exactly as the server sent them, ids included — and the ids
    are then never used for anything. They are carried so the report can say WHICH
    source row a change came from, in the source environment's own vocabulary.
    """
    rows = resolve_mod.list_rows(
        kind=kind, target=target, space=space, token=token, fetch=fetch,
        subject=f"{kind} in {target.name} cannot be used as a source of truth",
        remedy=_PARTIAL_REMEDY,
        paginate=True,
    )
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    return Source(
        rows=rows,
        origin=f"{target.name}:{space}",
        digest=hashlib.sha256(blob.encode()).hexdigest(),
        fmt="env",
    )


# ===========================================================================
# The comparison
# ===========================================================================
@dataclass(frozen=True)
class Change:
    """One op, already reduced to the exact ``$set``/``$expect`` it will carry.

    The payloads are computed HERE rather than in :meth:`DiffResult.ops`, so
    exactly one place knows that a jsonb-backed kind writes a whole column while a
    column-backed kind writes fields. Two representations of the same op is how a
    diff and the payload it emits come to disagree.
    """

    action: str  # "add" | "update"
    key: str
    #: Column → (target value, source value, why they differ). For display.
    fields: dict[str, tuple[Any, Any, str]] = field(default_factory=dict)
    #: Fields the target holds that the source did not mention. Carried forward,
    #: never dropped — same rule as rows.
    preserved: tuple[str, ...] = ()
    shape_changes: tuple[str, ...] = ()
    set_payload: dict = field(default_factory=dict)
    expect_payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DiffResult:
    kind: str
    env: str
    space: str
    key_field: str
    source: Source
    creates: list[Change]
    updates: list[Change]
    unchanged: list[str]
    would_delete: list[str]
    #: Source keys whose text CHANGED under normalisation. A silent trim is how a
    #: phantom row is born, so every one is named.
    normalised: list[tuple[str, str]]
    blocked_shape: list[Change]
    data_field: str | None
    #: How many target rows were actually read. NOT derived from the buckets — the
    #: printed header used to sum three of the four and silently omit
    #: `blocked_shape`, so it read 985 against a table of 986 and nothing said so.
    #: A count that is computed from the same numbers it is supposed to check
    #: cannot check them.
    target_count: int = 0
    #: Target names that identify more than one row, which the SOURCE does not
    #: mention. Reported with ids — the no-ids rule governs :meth:`ops`, the payload
    #: that gets written; a human report of rows that need de-duplicating is
    #: actionable only by id, and nothing here is ever fed back to a write.
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)

    def ops(self) -> list[dict]:
        """The payload ``fm update`` consumes.

        Ops only: the endpoint forbids extra keys, so the deletion list and the
        equality rules live in the ``--report`` sidecar rather than in here.

        ``$expect`` carries the values this diff READ, and that is deliberate even
        though GATE.md:239 warns against auto-filling a before-value. The warning
        is about a read milliseconds earlier IN THE SAME COMMAND, which converts a
        staleness check into one that cannot fail. Here the read and the write are
        two commands with a human review between them, which is the exact gap
        ``$expect`` exists to cover — `a plan is valid only against the state it
        was generated from` (services/bulk_data.py:285). The key field is left out
        so that ``fm update``'s own copy-back can fill it from its own resolution.
        """
        ops: list[dict] = []
        for change in self.creates:
            ops.append({"$action": "add", "$kind": self.kind, "$set": change.set_payload})
        for change in self.updates:
            op = {
                "$action": "update",
                "$kind": self.kind,
                "$name": change.key,
                "$set": change.set_payload,
            }
            if change.expect_payload:
                op["$expect"] = change.expect_payload
            ops.append(op)
        return ops

    def report(self) -> dict:
        return {
            "kind": self.kind,
            "target": {"env": self.env, "space": self.space},
            "source": {
                "origin": self.source.origin,
                "format": self.source.fmt,
                "sha256": self.source.digest,
                "rows": len(self.source.rows),
            },
            "equality_rules": {
                "match_key": {"field": self.key_field, "rule": KEY_RULE},
                "values": {"rule": VALUE_RULE},
                "written_column": (
                    f"{self.data_field} (whole jsonb column; unmentioned fields "
                    "merged forward, never dropped)"
                    if self.data_field
                    else "per-column"
                ),
            },
            "counts": {
                "create": len(self.creates),
                "update": len(self.updates),
                "unchanged": len(self.unchanged),
                "would_delete": len(self.would_delete),
                "blocked_on_shape": len(self.blocked_shape),
                "ambiguous_in_target": len(self.ambiguous),
                "target_rows_read": self.target_count,
            },
            "normalised_keys": [
                {"as_written": raw, "matched_as": norm} for raw, norm in self.normalised
            ],
            "creates": [c.key for c in self.creates],
            "updates": [
                {
                    "name": c.key,
                    "fields": {
                        col: {"target": tv, "source": sv, "why": why}
                        for col, (tv, sv, why) in c.fields.items()
                    },
                    "preserved": list(c.preserved),
                }
                for c in self.updates
            ],
            "blocked_on_shape": [
                {"name": c.key, "reasons": list(c.shape_changes)}
                for c in self.blocked_shape
            ],
            "ambiguous_in_target": [
                {"name": key, "ids": ids} for key, ids in self.ambiguous
            ],
            "would_delete": self.would_delete,
        }


def _get(row: dict, column: str, data_field: str | None) -> Any:
    if data_field is None:
        return row.get(column)
    blob = row.get(data_field)
    return blob.get(column) if isinstance(blob, dict) else None


def _reconcile(
    kind: str, read: int, unchanged: list, updates: list, blocked: list,
    would_delete: list,
) -> None:
    """Every target row must land in exactly one bucket, or nothing is emitted.

    A FUNCTION rather than four lines inline, because inline it could not be
    tested. It is a backstop: the guards above it — the locale-collision refusal in
    particular — mean nothing can currently reach it, so deleting the call changed
    no test and the check read as dead code. That is the layered-guard trap in
    GATE.md:170 exactly, and the answer there is to test the layer at its own
    level. This is that level.

    The arithmetic is the one `scripts/gate.sh` does over a pytest summary, for the
    same reason: a row read and then neither compared nor reported has gone missing
    silently, and the only visible symptom is a total slightly too small — which is
    precisely how the 986-vs-985 header defect presented.
    """
    accounted = len(unchanged) + len(updates) + len(blocked) + len(would_delete)
    if accounted != read:
        raise DiffError(
            f"internal: {read} {kind} rows were read but {accounted} were "
            "accounted for (unchanged + update + blocked + would-delete).\n"
            "A row that is read and then neither compared nor reported has gone "
            "missing, and the only symptom would have been a total slightly too "
            "small. Refusing to emit a payload computed from an incomplete pass."
        )


def build_diff(
    *,
    kind: str,
    key_field: str,
    fields: list[str] | None,
    source: Source,
    target_rows: list[dict],
    env: str,
    space: str,
    data_field: str | None,
    allow_shape_change: bool = False,
    extra_set: dict | None = None,
) -> DiffResult:
    """Compare, or refuse. Never guess which of two rows was meant."""
    # A name stored as an {en, ar} map indexes the SAME row under two keys — that
    # is `resolve.py`'s "match across shapes" rule and it is right. But it means
    # "which target rows were matched" is a question about ROWS, not about keys:
    # matching the English key leaves the Arabic one unmatched, and counting keys
    # would report a live row as a would-delete. Rows are tracked by identity.
    index: dict[str, list[dict]] = {}
    row_keys: dict[int, list[str]] = {}
    for row in target_rows:
        # dict.fromkeys, because a row whose {en, ar} halves hold the SAME text
        # yields that key twice and would be indexed against itself — measured on
        # live data on a real space, where 'Invest Africa' is stored identically in both
        # locales and was reported as ambiguous with its own id printed twice.
        # A row is never ambiguous with itself; only distinct rows can be.
        keys = list(dict.fromkeys(
            k for k in key_strings(_get(row, key_field, data_field)) if k
        ))
        row_keys[id(row)] = keys
        for key in keys:
            index.setdefault(key, []).append(row)

    ambiguous = {k: v for k, v in index.items() if len(v) > 1}

    # The SOURCE is normalised and validated before anything is compared, both so a
    # source defect is reported as a source defect, and because which ambiguities
    # actually MATTER is a question about what the source references.
    seen_source: dict[str, str] = {}
    normalised: list[tuple[str, str]] = []
    source_keys: list[str] = []
    for position, row in enumerate(source.rows):
        raw_key = row.get(key_field)
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise DiffError(
                f"source row {position} has no usable {key_field!r}: {raw_key!r}.\n"
                "Every row must be identifiable by the match key; a row that is not "
                "cannot be compared and must not be created blind."
            )
        key = normalise_key(raw_key)
        if not key:
            raise DiffError(f"source row {position}: {key_field!r} normalises to empty.")
        if key != raw_key:
            normalised.append((raw_key, key))
        if key in seen_source:
            raise DiffError(
                f"refusing to diff: source rows {seen_source[key]} and {position} "
                f"both normalise to {key!r}.\n"
                "Two source rows for one target row is a source-of-truth defect, and "
                "applying them in file order would silently let the last one win.\n"
                f"  as written: {raw_key!r}\n"
                "Nothing was compared."
            )
        seen_source[key] = str(position)
        source_keys.append(key)

    # Only an ambiguity the source REFERENCES can cause a wrong write, and that one
    # is fatal. An ambiguity elsewhere in the target is a data defect this tool did
    # not create and cannot resolve; refusing the whole run over it would make the
    # command unusable against the real space, which holds four such pairs and 986
    # rows. They are reported, loudly, and their rows are never written to.
    blocking = {k: v for k, v in ambiguous.items() if k in seen_source}
    if blocking:
        listing = "\n".join(
            f"    {key!r} → " + ", ".join(str(r.get("id")) for r in rows)
            for key, rows in sorted(blocking.items())
        )
        raise DiffError(
            f"refusing to diff: {len(blocking)} name(s) in the SOURCE identify more "
            f"than one {kind} row in {env}:\n{listing}\n"
            "\n"
            "A name that identifies two rows identifies neither, and picking one is "
            "how a wrong row gets written. Nothing was compared.\n"
            "→ de-duplicate the target, or address those rows by id in a payload "
            "you write yourself."
        )

    matched_rows: set[int] = set()
    creates: list[Change] = []
    updates: list[Change] = []
    blocked: list[Change] = []
    unchanged: list[str] = []
    matched: set[str] = set()

    # strict=True: source_keys is built from source.rows one-for-one, and if that
    # ever stops being true the pairs would silently shift and every row after
    # the gap would be compared against the wrong key.
    for row, key in zip(source.rows, source_keys, strict=True):
        columns = fields or [c for c in row if c != key_field]
        target = index.get(key, [None])[0]

        if target is None:
            payload = {c: row[c] for c in columns if c in row}
            # The NORMALISED key is what gets written, not the raw text. Writing
            # `'ODA '` back would create a row that this same diff would fail to
            # match on the next run and would create AGAIN.
            payload[key_field] = key
            if data_field is None:
                set_payload = dict(payload)
            else:
                set_payload = {data_field: dict(payload)}
            set_payload.update(extra_set or {})
            creates.append(
                Change(
                    action="add",
                    key=key,
                    fields={c: (None, v, "row absent from target") for c, v in payload.items()},
                    set_payload=set_payload,
                )
            )
            continue

        if id(target) in matched_rows:
            # The duplicate-source check above compares NORMALISED SOURCE KEYS and
            # is structurally unable to see this: 'FAO' and 'الفاو' are different
            # keys that resolve to one row, because a name map indexes its row under
            # every locale it holds. Two ops would be emitted for one row and the
            # later would silently win — the same last-one-wins failure the source
            # check exists to prevent, arriving by the one route that check cannot
            # cover. It is also what breaks the reconciliation below, which is how
            # this was found.
            other = next(k for k in row_keys[id(target)] if k in matched and k != key)
            raise DiffError(
                f"refusing to diff: source keys {other!r} and {key!r} are different "
                f"names for the SAME {kind} row in {env} ({target.get('id')}).\n"
                "A name stored as an {en, ar} map indexes its row under both, so two "
                "source rows can address one target row without ever colliding as "
                "text. Applying both would let the later one silently win.\n"
                "Nothing was compared."
            )
        matched.add(key)
        matched_rows.add(id(target))
        changed: dict[str, tuple[Any, Any, str]] = {}
        shape_changes: list[str] = []
        for column in columns:
            if column not in row:
                continue
            target_value = _get(target, column, data_field)
            source_value = row[column]
            equal, why = values_equal(target_value, source_value)
            if equal:
                continue
            if why.startswith("shape differs") and not allow_shape_change:
                shape_changes.append(f"{column}: {why}")
                continue
            changed[column] = (target_value, source_value, why)

        preserved = _preserved(target, columns, data_field)
        if shape_changes:
            blocked.append(
                Change(action="update", key=key, fields=changed,
                       preserved=preserved, shape_changes=tuple(shape_changes))
            )
        elif changed:
            if data_field is None:
                set_payload = {c: v[1] for c, v in changed.items()}
                expect_payload = {c: v[0] for c, v in changed.items()}
            else:
                # `$set` keys must be real COLUMNS, and a CustomEntity has exactly
                # one: `data` (models/CustomEntity.py:20). So the write is the whole
                # jsonb blob, merged over what the target already holds — a naive
                # whole-column write silently deletes every field the source did not
                # know about, which is the row rule applied one level down.
                blob = target.get(data_field)
                blob = dict(blob) if isinstance(blob, dict) else {}
                merged = {**blob, **{c: v[1] for c, v in changed.items()}}
                set_payload = {data_field: merged}
                # The whole blob, so a concurrent change to ANY field in it
                # invalidates this plan. That is the correct granularity: the write
                # is the whole column, so the precondition must be too.
                expect_payload = {data_field: target.get(data_field)}
            updates.append(
                Change(action="update", key=key, fields=changed, preserved=preserved,
                       set_payload=set_payload, expect_payload=expect_payload)
            )
        else:
            unchanged.append(key)

    # Every target row must land in exactly one bucket. This is the arithmetic
    # `scripts/gate.sh` does over a pytest summary, for the same reason: a row that
    # is read and then neither compared nor reported has gone missing silently, and
    # the only visible symptom is a total that is slightly too small.
    would_delete = sorted(
        (row_keys[id(row)] or [str(row.get("id"))])[0]
        for row in target_rows
        if id(row) not in matched_rows
    )

    _reconcile(kind, len(target_rows), unchanged, updates, blocked, would_delete)

    return DiffResult(
        kind=kind, env=env, space=space, key_field=key_field, source=source,
        creates=creates, updates=updates, unchanged=unchanged,
        would_delete=would_delete, normalised=normalised, blocked_shape=blocked,
        data_field=data_field,
        target_count=len(target_rows),
        ambiguous=sorted(
            (key, [str(r.get("id")) for r in rows]) for key, rows in ambiguous.items()
        ),
    )


def _preserved(target: dict, columns: list[str], data_field: str | None) -> tuple[str, ...]:
    """Target fields the source never mentioned.

    They are carried, not dropped — the same rule as rows, and it matters more for
    a jsonb column: ``$set`` keys must be real columns, so a CustomEntity update
    writes the WHOLE ``data`` blob and a naive write would silently delete every
    field the source did not know about.
    """
    if data_field is None:
        return ()
    blob = target.get(data_field)
    if not isinstance(blob, dict):
        return ()
    return tuple(sorted(k for k in blob if k not in columns))


def target_rows_for(
    *, kind: str, target: Target, space: str, token: str | None,
    entity_type: str | None, fetch=None,
) -> tuple[list[dict], str | None, dict]:
    """The target's rows, the jsonb column they live in, and any extra ``$set``.

    A CustomEntity is scoped by ``entity_type_id`` (models/CustomEntity.py:18) and
    has no name column at all, so ``--entity-type`` is required for that kind: the
    diff must compare within one type, and a create has to carry the FK.
    """
    spec = resolve_mod.KINDS.get(kind)
    if spec is None:
        raise DiffError(
            f"unknown kind {kind!r}. Valid kinds: {resolve_mod.kind_names()}."
        )
    if spec.no_name_reason:
        raise DiffError(f"{kind} cannot be diffed by name — {spec.no_name_reason}")

    rows = resolve_mod.list_rows(
        kind=kind, target=target, space=space, token=token, fetch=fetch,
        subject=f"{kind} in {target.name} cannot be diffed",
        remedy=_PARTIAL_REMEDY,
        paginate=True,
    )
    if spec.data_field is None:
        if entity_type:
            raise DiffError(
                f"--entity-type is only meaningful for a jsonb-backed kind; {kind} "
                "has real columns."
            )
        return rows, None, {}

    if not entity_type:
        raise DiffError(
            f"--entity-type is required for {kind}.\n"
            "A CustomEntity is scoped by entity_type_id and has no name column of "
            "its own, so a diff without a type would compare Organisations against "
            "Venues and propose creating every one of them.\n"
            "→ `--entity-type \"Organization\"` (the EntityType's single_name)."
        )
    match = resolve_mod.resolve_one(
        entity_type, kind="ENTITY_TYPE", target=target, space=space,
        token=token, fetch=fetch,
    )
    scoped = [r for r in rows if str(r.get("entity_type_id")) == str(match.id)]
    return scoped, spec.data_field, {"entity_type_id": match.id}


# ===========================================================================
# The CLI surface. Kept here so adding this command costs cli.py two lines.
# ===========================================================================
def add_parser(sub) -> None:

    parser = sub.add_parser(
        "diff",
        help="compare a source of truth against an environment; emit fm update ops",
        description=(
            "`fm update` answers HOW to write. This answers WHAT. It compares a "
            "source of truth against a target environment and writes a payload "
            "`fm update` consumes, so the two compose:\n"
            "    fm diff stakeholder --source rows.json --out ops.json\n"
            "      →  read it  →  fm update … ops.json\n\n"
            "Ops name rows by $name, never by id — ids differ between every "
            "environment, and $name is re-resolved by `fm update` against the "
            "target seconds before the write, with the resolved value copied into "
            "$expect so the row itself refuses a mis-resolution.\n\n"
            "THE EQUALITY RULES ARE PRINTED ON EVERY RUN, because two hand "
            "reconciliations of one delta agreed on the creates and differed 4 vs "
            "11 on the updates — 'already equal' meant something different on each "
            "side and nothing said so.\n\n"
            f"  match key: {KEY_RULE}\n"
            f"  values:    {VALUE_RULE}\n\n"
            "A row present in the target and absent from the source is NEVER a "
            "delete. It is reported and nothing else. Sources: .json (array of row "
            "objects, or {\"rows\": [...]}), .csv (header row), or --from-host to "
            "use another environment as the source of truth."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=argparse.SUPPRESS, metavar="HOST",
                        help="deployment to compare AGAINST (default: formist)")
    parser.add_argument("--space", help="space UUID (never a name)")
    parser.add_argument(
        "kind",
        help=f"one of: {resolve_mod.kind_names()}  (case-insensitive; - and _ alike)",
    )
    parser.add_argument("--entity-type", dest="entity_type",
                        help="EntityType single_name; required for the `custom` kind")
    parser.add_argument("--source", help="path to a .json or .csv source of truth")
    parser.add_argument("--from-host", dest="from_env", metavar="HOST",
                        help="use another deployment as the source of truth")
    parser.add_argument("--key", default="name",
                        help="the column rows are matched by (default: name)")
    parser.add_argument("--fields",
                        help="comma-separated columns to compare (default: every "
                             "column the source mentions)")
    parser.add_argument("--allow-shape-change", dest="allow_shape", action="store_true",
                        help="emit ops that change a value's SHAPE (string ↔ map). "
                             "Off by default: coalescing the two nearly converted "
                             "14 production rows")
    parser.add_argument("--out", help="write the ops payload here")
    parser.add_argument("--report", help="write the full report (rules, deletions, "
                                         "normalisations) here")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def cmd_diff(args) -> int:
    import sys

    # Imported inside the function because `cli` imports this module. Reusing its
    # `_effective`/`_token_for` rather than copying ten lines keeps ONE provenance
    # banner in the tool; a second one would drift from the rules in prefs.py that
    # make it worth printing.
    from formist_cli.cli import _effective, _token_for

    eff = _effective(args)
    token = _token_for(eff)
    kind = resolve_mod.require_kind(args.kind)

    if bool(args.source) == bool(args.from_env):
        raise DiffError(
            "give exactly one of --source (a file) or --from-host (another "
            "deployment).\n"
            "Two sources of truth is not a source of truth."
        )

    # The LOCAL source is read first, before any network call. A mistyped --source
    # is the most likely error in this command and the cheapest to detect, and
    # fetching the target first reports it only after a round trip and — against
    # prod — after an admin auth check, so the operator is shown a 403 for a
    # command that was never going to run. Cheap, local, certain checks go first.
    source = load_source(args.source) if args.source else None

    target_rows, data_field, extra_set = target_rows_for(
        kind=kind, target=eff.target, space=eff.space, token=token,
        entity_type=args.entity_type,
    )

    if source is None:
        from formist_cli import prefs as prefs_mod
        from formist_cli.targets import resolve as resolve_target

        src_env = prefs_mod.canonical_env(args.from_env)
        if src_env == eff.target.name:
            raise DiffError(
                f"--from-host {src_env!r} is the deployment being compared against. "
                "A diff of an environment with itself is empty by construction."
            )
        src_target = resolve_target(src_env)
        from formist_cli import tokens as tokens_mod

        src_token, _ = tokens_mod.load_for(src_target.name)
        print(f"  source of truth: {src_target.name} ({src_target.api_base})",
              file=sys.stderr)
        source = source_from_env(
            kind=kind, target=src_target, space=eff.space, token=src_token
        )

    result = build_diff(
        kind=kind,
        key_field=args.key,
        fields=[f.strip() for f in args.fields.split(",")] if args.fields else None,
        source=source,
        target_rows=target_rows,
        env=eff.target.name,
        space=eff.space,
        data_field=data_field,
        allow_shape_change=args.allow_shape,
        extra_set=extra_set,
    )

    report = result.report()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        _print_report(result, report)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
        print(f"\nreport → {args.report}", file=sys.stderr)

    ops = result.ops()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"ops": ops}, handle, indent=2, ensure_ascii=False, default=str)
        print(
            f"ops    → {args.out}  ({len(ops)} op(s))\n"
            f"\nRead it, then:\n"
            f"  fm update --host {result.env} --space {result.space} {args.out}",
            file=sys.stderr,
        )
    elif ops:
        print(
            f"\n{len(ops)} op(s) computed but not written — pass --out FILE.",
            file=sys.stderr,
        )
    return 0


def _print_report(result: DiffResult, report: dict) -> None:
    counts = report["counts"]
    print(f"{result.kind} in {result.env} / {result.space}")
    print(f"  source {result.source.origin}  [{result.source.fmt}] "
          f"sha256:{result.source.digest[:12]}  {len(result.source.rows)} rows")
    print(f"  target {result.target_count} rows  [reconciled: unchanged + update "
          "+ blocked + would-delete]")
    print()
    print(f"  match key  {result.key_field}: {KEY_RULE}")
    print(f"  values     {VALUE_RULE}")
    print(f"  writes     {report['equality_rules']['written_column']}")
    print()
    print(f"  create {counts['create']}   update {counts['update']}   "
          f"unchanged {counts['unchanged']}   "
          f"blocked-on-shape {counts['blocked_on_shape']}   "
          f"would-delete {counts['would_delete']}")

    if result.normalised:
        print("\n  keys that CHANGED under normalisation "
              "(an untrimmed name creates a phantom row):")
        for raw, norm in result.normalised:
            print(f"    {raw!r} → {norm!r}")

    for change in result.creates:
        print(f"\n  + {change.key}")
    for change in result.updates:
        print(f"\n  ~ {change.key}")
        for column, (target_value, source_value, why) in change.fields.items():
            print(f"      {column}: {_show(target_value)} → {_show(source_value)}   [{why}]")
        if change.preserved:
            print(f"      (preserved, not in source: {', '.join(change.preserved)})")
    for change in result.blocked_shape:
        print(f"\n  ! {change.key}  BLOCKED — shape change, no op emitted")
        for reason in change.shape_changes:
            print(f"      {reason}")
        print("      A bare string and an {en,…} map are different values; "
              "coalescing them nearly converted 14 production rows.")
        print("      → --allow-shape-change if that is genuinely what you mean.")
    if result.ambiguous:
        print(f"\n  ambiguous in {result.env} ({len(result.ambiguous)}) — NOT "
              "referenced by the source, so nothing here was compared or written:")
        for key, ids in result.ambiguous:
            print(f"    ? {key}  →  {', '.join(ids)}")
        print("    A name identifying two rows identifies neither. These are "
              "pre-existing duplicates in the target, not something this diff "
              "introduced — but a later source that DOES name one of them will be "
              "refused outright.")

    if result.would_delete:
        print(f"\n  would-delete ({len(result.would_delete)}) — REPORTED ONLY, never "
              "emitted as an op:")
        for key in result.would_delete:
            print(f"    - {key}")
        print("    A row missing from the source is not authority to delete it. The "
              "source may simply be partial.")


def _show(value: Any) -> str:
    """A value as JSON, never through str().

    `str({"en": "FAO"})` is a dict repr — it matches nothing, and printing one
    beside a real value invites comparing them by eye and concluding they agree.
    """
    return json.dumps(value, ensure_ascii=False, default=str)
