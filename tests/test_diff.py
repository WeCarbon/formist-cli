"""``fm diff`` — the comparison, which is where every measured defect lived.

Each test names the real data that produced it. The values here (``'ODA '``,
``'FAO '``, a ``notes`` column holding a string in one environment and an
``{"en": …}`` map in the other) are the shapes actually found in this project's
production and development databases on 2026-08-26/27.
"""

from __future__ import annotations

import json
import re
import unicodedata

import pytest

from formist_cli import diff
from formist_cli import resolve as resolve_mod
from formist_cli.diff import DiffError
from formist_cli.resolve import ResolutionError
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


def _fetcher(items, total=None):
    """A stand-in for ``api.get`` that returns one canned list envelope."""

    def fetch(target, path, *, token=None, params=None, **kwargs):
        return {
            "items": items,
            "total": len(items) if total is None else total,
            "limit": 200,
            "offset": 0,
        }

    return fetch


def _paging_fetcher(rows, total=None, calls=None):
    """A stand-in that honours ``offset`` — i.e. one that behaves like the server.

    ``_fetcher`` above returns the same page whatever offset it is asked for, which
    is the shape of fake that makes a pagination bug invisible: the walk "works"
    because every page happens to contain the answer.
    """
    reported = len(rows) if total is None else total

    def fetch(target, path, *, token=None, params=None, **kwargs):
        offset = int((params or {}).get("offset", 0))
        limit = int((params or {}).get("limit", 200))
        if calls is not None:
            calls.append(offset)
        return {
            "items": rows[offset : offset + limit],
            "total": reported,
            "limit": limit,
            "offset": offset,
        }

    return fetch


def _source(rows, origin="test.json", fmt="json"):
    return diff.Source(rows=rows, origin=origin, digest="0" * 64, fmt=fmt)


def _build(source_rows, target_rows, **kw):
    return diff.build_diff(
        kind="CUSTOM" if kw.get("data_field") else "STAKEHOLDER",
        key_field=kw.pop("key_field", "name"),
        fields=kw.pop("fields", None),
        source=_source(source_rows),
        target_rows=target_rows,
        env="local",
        space="452093ea-7b91-491e-88b6-3285edd48a31",
        data_field=kw.pop("data_field", None),
        allow_shape_change=kw.pop("allow_shape_change", False),
        extra_set=kw.pop("extra_set", None),
    )


# ---------------------------------------------------------------------------
# 1. The match key — the trailing-whitespace family
# ---------------------------------------------------------------------------
def test_the_two_characters_a_bare_strip_cannot_reach():
    """MEASURED, because the plausible reason for the ``_SPACES`` table is false.

    The reason first written here — "``.strip()`` does not remove U+00A0" — is not
    true of Python: U+00A0 is ``isspace()``, ``.strip()`` removes it, ``\\s`` matches
    it, and NFKC maps it to U+0020 as well. Of the 18 characters in the table, **16
    are handled three ways over**.

    Exactly two are not: U+200B ZERO WIDTH SPACE and U+FEFF ZERO WIDTH NO-BREAK
    SPACE survive NFKC, ``\\s`` and ``.strip()`` alike. They are the whole reason the
    table exists, and they were the two characters the reason did not name.

    That gap is not cosmetic. Every other test in this file exercises U+00A0, so a
    reader who deletes the table as redundant — which for U+00A0 it *is* — keeps the
    entire suite green and starts creating phantom rows on zero-width input. The
    assertions below are the ones that go red for that edit.
    """
    # NBSP: strip reaches it. NFKC reaches it. `\s` reaches it.
    assert "ODA\xa0".strip() == "ODA"
    assert unicodedata.normalize("NFKC", "\xa0") == " "
    # Zero-width: nothing generic reaches these two.
    assert "ODA​".strip() == "ODA​"
    assert "ODA﻿".strip() == "ODA﻿"
    assert unicodedata.normalize("NFKC", "​") == "​"
    assert re.sub(r"\s+", " ", "ODA​").strip() == "ODA​"

    # The rule handles all of them, and the table is what carries the last two.
    assert diff.normalise_key("ODA​") == "ODA"
    assert diff.normalise_key("ODA﻿") == "ODA"
    assert diff.normalise_key("ODA\xa0") == "ODA"
    assert diff.normalise_key("FAO ") == "FAO"
    assert diff.normalise_key("  Food  and\xa0Agriculture  ") == "Food and Agriculture"


def test_curly_quotes_and_dashes_normalise():
    assert diff.normalise_key("Farmers’ Union") == "Farmers' Union"
    assert diff.normalise_key("Africa–GCC Corridor") == "Africa-GCC Corridor"
    assert diff.normalise_key("“Green Silk Road”") == '"Green Silk Road"'


def test_case_is_never_folded():
    """formist_cli/resolve.py:283 refuses to fold case when resolving, because folding is
    how a resolver picks the wrong row. This runs seconds before that resolver, so
    the two must agree — a diff that matched case-insensitively would emit a $name
    `fm update` then refuses to resolve.
    """
    assert diff.normalise_key("adafsa") != diff.normalise_key("ADAFSA")


def test_a_trailing_space_does_not_produce_a_phantom_create():
    result = _build(
        [{"name": "ODA\xa0", "org": "UN"}],
        [{"id": "t-1", "name": "ODA", "org": "UN"}],
    )
    assert result.creates == []
    assert result.unchanged == ["ODA"]
    # And the normalisation is NAMED, because a silent trim is unauditable.
    assert result.normalised == [("ODA\xa0", "ODA")]


def test_a_zero_width_trailing_character_does_not_produce_a_phantom_create():
    """The same property as above, on the input that the generic machinery misses.

    U+200B is invisible in every terminal, every diff view and every spreadsheet
    cell, so this is the case an operator cannot see and cannot report: the row
    exists, the diff proposes creating it, and the second copy is created under a
    name that renders identically to the first.
    """
    result = _build(
        [{"name": "ODA​", "org": "UN"}],
        [{"id": "t-1", "name": "ODA", "org": "UN"}],
    )
    assert result.creates == []
    assert result.unchanged == ["ODA"]
    assert result.normalised == [("ODA​", "ODA")]


def test_a_create_writes_the_normalised_key_not_the_raw_text():
    """Writing `'ODA '` back would create a row this same diff fails to match on
    the next run — and creates again. The phantom becomes recurrent.
    """
    result = _build([{"name": "New Org\xa0", "org": "X"}], [])
    assert result.creates[0].set_payload["name"] == "New Org"


# ---------------------------------------------------------------------------
# 2. Shape is part of a value
# ---------------------------------------------------------------------------
def test_a_bare_string_never_equals_an_en_map():
    """`notes` holds a bare JSON string in production and an `{"en": …}` map in
    development: same column, same code. The frontend writes a string while English
    is the only locale and a map once a second exists; importers always write maps.

    services/bulk_data.py:290-300 refuses to coalesce them, naming the 14 prod rows
    a coalescing probe would have silently converted. So this refuses too.
    """
    equal, why = diff.values_equal("FAO", {"en": "FAO"})
    assert equal is False
    assert "shape differs" in why


def test_a_shape_change_is_blocked_by_default_and_emits_no_op():
    result = _build(
        [{"name": "FAO", "notes": {"en": "Rome"}}],
        [{"id": "t-1", "name": "FAO", "notes": "Rome"}],
    )
    assert result.updates == []
    assert len(result.blocked_shape) == 1
    assert "notes" in result.blocked_shape[0].shape_changes[0]
    assert result.ops() == []


def test_a_shape_change_is_emitted_only_when_asked_for():
    result = _build(
        [{"name": "FAO", "notes": {"en": "Rome"}}],
        [{"id": "t-1", "name": "FAO", "notes": "Rome"}],
        allow_shape_change=True,
    )
    assert len(result.updates) == 1
    assert result.ops()[0]["$set"] == {"notes": {"en": "Rome"}}


def test_no_value_is_ever_rendered_through_str():
    """`str({"en": "FAO"})` is `"{'en': 'FAO'}"` — a Python dict repr, which matches
    nothing and which a reader comparing two printed values by eye will read as the
    same thing as the JSON. Every printed value goes through json.dumps.
    """
    shown = diff._show({"en": "FAO"})
    assert shown == '{"en": "FAO"}'
    assert "'" not in shown


def test_the_DECISION_path_never_compares_through_str_either():
    """The test above covers ``_show`` — the DISPLAY path. This covers
    ``values_equal`` — the DECISION path, and it exists because replacing that
    comparison with ``str(left) == str(right)`` SURVIVED this entire file.

    The test named "no value is ever rendered through str()" asserted the property
    of the half that only prints. The half that decides whether to WRITE was
    unguarded, and the two are one line apart.

    Same-shape values are where ``==`` and ``str()`` part company: two maps that are
    equal but were built in a different key order compare equal and print
    differently — a jsonb column round-tripped with ``sort_keys`` on one side and
    not the other is exactly that. A ``str()`` comparison would call them different
    and propose writing a value the target already holds, which is the "already
    equal meant different things on each side" disagreement this module exists to
    end, reintroduced one layer below where it was fixed.
    """
    left = {"en": "FAO", "ar": "الفاو"}
    right = {"ar": "الفاو", "en": "FAO"}
    assert left == right
    assert str(left) != str(right)  # the trap, asserted so it cannot be argued
    assert diff.values_equal(left, right) == (True, "identical")

    # the same trap one level down, inside a list
    assert diff.values_equal([{"a": 1, "b": 2}], [{"b": 2, "a": 1}]) == (True, "identical")

    # and end to end: a key-order difference must not become an update op
    result = _build(
        [{"name": "FAO", "notes": {"en": "x", "ar": "y"}}],
        [{"id": "t-1", "name": "FAO", "notes": {"ar": "y", "en": "x"}}],
    )
    assert result.updates == []
    assert result.unchanged == ["FAO"]


def test_a_whitespace_only_value_difference_is_reported_as_such():
    """Not equal — the value is written exactly — but the REASON distinguishes it
    from a real edit, which is what a reviewer needs in order to skip it or not.
    """
    equal, why = diff.values_equal("Abu Dhabi ", "Abu Dhabi")
    assert equal is False
    assert "normalisation" in why


# ---------------------------------------------------------------------------
# 3. Never guess which row was meant
# ---------------------------------------------------------------------------
def test_two_target_rows_with_one_name_refuse_the_whole_diff():
    with pytest.raises(DiffError) as exc:
        _build(
            [{"name": "ADAFSA", "org": "X"}],
            [
                {"id": "t-1", "name": "ADAFSA"},
                {"id": "t-2", "name": "ADAFSA\xa0"},
            ],
        )
    message = str(exc.value)
    assert "t-1" in message and "t-2" in message
    assert "Nothing was compared" in message


def test_filling_an_empty_column_is_an_update_and_clearing_one_is_not():
    """NULL is absence, not a shape — and the two directions are not symmetric.

    The first live run against GFSS classified every fill-in as a "shape change"
    (null vs string) and emitted no op for any of them, which is the whole of the
    ordinary work refused on a technicality. But a source null over a POPULATED
    column is a field-level delete, and this module's rule is that the source is
    never authority to delete; that direction stays blocked behind
    --allow-shape-change.
    """
    filled = _build(
        [{"name": "Wanted", "org": "UN"}], [{"id": "t-1", "name": "Wanted", "org": None}]
    )
    assert filled.updates[0].fields["org"] == (None, "UN", "target holds no value")
    assert filled.blocked_shape == []

    cleared = _build(
        [{"name": "Wanted", "org": None}], [{"id": "t-1", "name": "Wanted", "org": "UN"}]
    )
    assert cleared.updates == []
    assert cleared.blocked_shape[0].shape_changes == ("org: shape differs: string vs null",)


def test_a_row_is_never_ambiguous_with_ITSELF():
    """MEASURED on live GFSS data. 'Invest Africa' is stored identically in both
    halves of its `{en, ar}` name map — normal for a proper noun — so `key_strings`
    yielded the same key twice, the row was indexed against itself, and the diff
    refused with the SAME id printed twice as the two conflicting rows.

    The message was self-refuting and still stopped the whole command: the operator
    is told two rows collide and shown one id, with no possible remedy, because
    there is no second row to de-duplicate.
    """
    target = [{"id": "t-1", "name": {"en": "Invest Africa", "ar": "Invest Africa"}}]
    result = _build([{"name": "Invest Africa", "org": "PRIVATE"}], target)
    assert result.updates[0].key == "Invest Africa"
    assert result.would_delete == []
    assert result.ambiguous == []


def test_an_ambiguity_the_source_never_names_is_reported_not_fatal():
    """The real space holds 986 rows and FOUR pre-existing duplicate names. Refusing
    the whole run over a duplicate the source does not mention makes the command
    unusable against the only data it was built for — and buys nothing, because a
    row no source key resolves to is a row nothing was ever going to write.

    So the refusal is kept exactly where the danger is (a source key that resolves
    to two rows) and the rest is reported. Loudly: a later source that DOES name one
    of them is refused outright, and the operator should learn that now.
    """
    target = [
        {"id": "t-1", "name": "Wanted"},
        {"id": "dup-a", "name": "Untouched Duplicate"},
        {"id": "dup-b", "name": "Untouched Duplicate"},
    ]
    result = _build([{"name": "Wanted", "org": "UN"}], target)

    assert result.updates[0].key == "Wanted"          # the run completed
    assert result.ambiguous == [("Untouched Duplicate", ["dup-a", "dup-b"])]
    assert result.report()["counts"]["ambiguous_in_target"] == 1
    # and no op names the ambiguous row
    assert all(op.get("$name") != "Untouched Duplicate" for op in result.ops())


def test_an_ambiguity_the_source_DOES_name_still_refuses_everything():
    """The other side of the split above — the case where a wrong row could be
    written is unchanged, and nothing is compared.
    """
    target = [
        {"id": "dup-a", "name": "Contested"},
        {"id": "dup-b", "name": "Contested"},
    ]
    with pytest.raises(DiffError) as exc:
        _build([{"name": "Contested", "org": "UN"}], target)
    assert "in the SOURCE identify more than one" in str(exc.value)
    assert "dup-a" in str(exc.value) and "dup-b" in str(exc.value)
    assert "Nothing was compared" in str(exc.value)


def test_two_source_rows_normalising_to_one_key_refuse_the_whole_diff():
    with pytest.raises(DiffError) as exc:
        _build(
            [{"name": "FAO", "org": "A"}, {"name": "FAO ", "org": "B"}],
            [{"id": "t-1", "name": "FAO"}],
        )
    assert "both normalise to" in str(exc.value)


def test_a_source_row_with_no_key_is_refused_not_created_blind():
    with pytest.raises(DiffError) as exc:
        _build([{"org": "X"}], [])
    assert "no usable" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Absence is never authority to delete
# ---------------------------------------------------------------------------
def test_two_source_rows_that_are_two_LOCALES_of_one_target_row_refuse():
    """The duplicate-source check compares normalised source KEYS, and is
    structurally unable to see this one: 'FAO' and 'الفاو' are different text that
    resolve to the same row, because a name map indexes its row under every locale
    it holds. Two ops for one row, later one silently wins — the exact failure the
    text check exists to prevent, arriving by the route it cannot cover.

    Found by the reconciliation, not by reading the code: the buckets summed to one
    more than the number of rows read.
    """
    target = [{"id": "t-1", "name": {"en": "FAO", "ar": "الفاو"}}]
    with pytest.raises(DiffError) as exc:
        _build([{"name": "FAO", "org": "UN"}, {"name": "الفاو", "org": "UN-AR"}], target)
    assert "different names for the SAME STAKEHOLDER row" in str(exc.value)
    assert "t-1" in str(exc.value)
    assert "Nothing was compared" in str(exc.value)


def test_every_target_row_lands_in_exactly_one_bucket():
    """The printed header used to sum three of the four buckets and omit
    `blocked_shape`, so a live run over 986 rows reported "target 985 rows" and
    nothing said otherwise. A count derived from the same numbers it is meant to
    check cannot check them — so the count is now read from the rows, and the
    buckets are reconciled against it.
    """
    target = [
        {"id": "t-1", "name": "Same", "org": "UN"},        # unchanged
        {"id": "t-2", "name": "Changed", "org": "UN"},     # update
        {"id": "t-3", "name": "Shaped", "org": "UN"},      # blocked on shape
        {"id": "t-4", "name": "Absent From Source"},       # would-delete
    ]
    result = _build(
        [
            {"name": "Same", "org": "UN"},
            {"name": "Changed", "org": "WFP"},
            {"name": "Shaped", "org": {"en": "UN"}},
        ],
        target,
    )
    assert result.target_count == 4
    assert (len(result.unchanged) + len(result.updates)
            + len(result.blocked_shape) + len(result.would_delete)) == 4
    assert result.report()["counts"]["target_rows_read"] == 4


def test_the_reconciliation_refuses_when_a_row_goes_missing():
    """Tested at its OWN level, because nothing can currently reach it through
    `build_diff` — the locale-collision guard one layer up rejects the only input
    that produced an unaccounted row. Deleting the call site therefore reddened
    nothing, and the check read as dead code: GATE.md:170's layered-guard trap,
    where "this guard is covered" and "this guard is dead" are the same signal.

    So the arithmetic is exercised directly, and the wiring is asserted separately
    below. Neither test alone is enough.
    """
    with pytest.raises(DiffError) as exc:
        diff._reconcile("STAKEHOLDER", 986, ["a"], ["b"], [], ["c"])
    assert "986 STAKEHOLDER rows were read but 3 were accounted for" in str(exc.value)
    assert "Refusing to emit a payload" in str(exc.value)

    # and it stays quiet when the books balance
    diff._reconcile("STAKEHOLDER", 3, ["a"], ["b"], [], ["c"])


def test_build_diff_actually_calls_the_reconciliation(monkeypatch):
    """The other half: a correct check that is never invoked is not a check."""
    seen: list[tuple] = []
    monkeypatch.setattr(diff, "_reconcile", lambda *a: seen.append(a))
    _build([{"name": "Same", "org": "UN"}], [{"id": "t-1", "name": "Same", "org": "UN"}])
    assert seen and seen[0][0] == "STAKEHOLDER" and seen[0][1] == 1


def test_a_row_missing_from_the_source_is_reported_never_deleted():
    result = _build(
        [{"name": "FAO", "org": "UN"}],
        [
            {"id": "t-1", "name": "FAO", "org": "UN"},
            {"id": "t-2", "name": "IFAD", "org": "UN"},
        ],
    )
    assert result.would_delete == ["IFAD"]
    assert all(op["$action"] != "delete" for op in result.ops())
    assert result.report()["counts"]["would_delete"] == 1


# ---------------------------------------------------------------------------
# 5. The output contract with `fm update`
# ---------------------------------------------------------------------------
def test_ops_name_rows_by_name_and_carry_no_target_id():
    """Every organisation id differs between environments, and an 8-character
    prefix was used as a primary key over a table where a prefix collision has been
    measured. So no id crosses this boundary at all: `$name` is re-resolved by
    `fm update` against the environment being written.
    """
    result = _build(
        [{"name": "FAO", "org": "Rome"}],
        [{"id": "8c4018c2-0000-0000-0000-000000000000", "name": "FAO", "org": "UN"}],
    )
    blob = json.dumps(result.ops())
    assert "$name" in blob
    assert "$id" not in blob
    assert "8c4018c2" not in blob


def test_an_update_carries_expect_from_the_state_it_was_generated_against():
    """GATE.md:239 warns against auto-filling `$expect` from a read you just did —
    that converts a staleness check into one that cannot fail. It does not apply
    here, and the difference is the point: that read is milliseconds earlier IN THE
    SAME COMMAND, while this one is a separate command with a human review between
    it and the write. That gap is exactly what `$expect` covers.

    The KEY field is deliberately absent, so `fm update`'s own copy-back fills it
    from its own resolution rather than from this file.
    """
    result = _build(
        [{"name": "FAO", "org": "Rome"}],
        [{"id": "t-1", "name": "FAO", "org": "UN"}],
    )
    op = result.ops()[0]
    assert op["$expect"] == {"org": "UN"}
    assert "name" not in op["$expect"]
    assert op["$set"] == {"org": "Rome"}


def test_ops_are_ops_only_so_the_endpoint_accepts_them():
    """services/bulk_data.py forbids extra keys, so the deletion list and the
    equality rules go in the report sidecar and never into the payload.
    """
    result = _build(
        [{"name": "New", "org": "X"}],
        [{"id": "t-2", "name": "Gone"}],
    )
    allowed = {"$action", "$kind", "$id", "$set", "$expect", "$ref", "$name"}
    for op in result.ops():
        assert set(op) <= allowed, set(op) - allowed


# ---------------------------------------------------------------------------
# 6. A jsonb-backed kind writes a WHOLE column
# ---------------------------------------------------------------------------
def test_a_custom_entity_update_merges_rather_than_replacing_the_blob():
    """A CustomEntity has one writable column, `data` (models/CustomEntity.py:20),
    so `$set` writes the whole blob. A naive whole-column write silently deletes
    every field the source did not know about — the row rule one level down.
    """
    result = _build(
        [{"name": "FAO", "status": "Confirmed"}],
        [
            {
                "id": "t-1",
                "entity_type_id": "et-1",
                "data": {
                    "name": "FAO",
                    "status": "Tentative",
                    "relevance_to_summit": "written by someone else",
                },
            }
        ],
        data_field="data",
    )
    op = result.ops()[0]
    assert op["$set"] == {
        "data": {
            "name": "FAO",
            "status": "Confirmed",
            "relevance_to_summit": "written by someone else",
        }
    }
    assert "relevance_to_summit" in result.updates[0].preserved


def test_a_custom_entity_expect_pins_the_whole_blob():
    """The write is the whole column, so the precondition must be too: a concurrent
    change to ANY field in the blob has to invalidate this plan, because applying it
    would overwrite that change.
    """
    target = {
        "id": "t-1",
        "entity_type_id": "et-1",
        "data": {"name": "FAO", "status": "Tentative", "country": "IT"},
    }
    result = _build(
        [{"name": "FAO", "status": "Confirmed"}], [target], data_field="data"
    )
    assert result.ops()[0]["$expect"] == {"data": target["data"]}


def test_a_custom_entity_create_carries_the_entity_type_fk():
    result = _build(
        [{"name": "Brand New", "status": "Tentative"}],
        [],
        data_field="data",
        extra_set={"entity_type_id": "et-1"},
    )
    op = result.ops()[0]
    assert op["$set"]["entity_type_id"] == "et-1"
    assert op["$set"]["data"]["name"] == "Brand New"


# ---------------------------------------------------------------------------
# 7. The report is the falsifiable artifact
# ---------------------------------------------------------------------------
def test_the_report_prints_the_rule_it_used_for_equality():
    """Two hand reconciliations of one delta agreed on the creates and differed
    4 vs 11 on the updates, because "already equal" meant different things on each
    side and neither said which. A disagreement now has somewhere to land.
    """
    result = _build([{"name": "FAO", "org": "UN"}], [{"id": "t-1", "name": "FAO", "org": "UN"}])
    rules = result.report()["equality_rules"]
    assert "U+00A0" in rules["match_key"]["rule"]
    # The printed rule names the zero-width pair too. An operator reading a
    # would-create for a row they can see in the target needs the one explanation
    # they cannot reach by looking at the screen.
    assert "U+200B" in rules["match_key"]["rule"]
    assert "CASE-SENSITIVE" in rules["match_key"]["rule"]
    assert "SHAPE-SENSITIVE" in rules["values"]["rule"]
    assert rules["match_key"]["field"] == "name"


def test_the_report_ties_itself_to_the_bytes_it_compared():
    result = _build([{"name": "FAO"}], [{"id": "t-1", "name": "FAO"}])
    source = result.report()["source"]
    assert len(source["sha256"]) == 64
    assert source["rows"] == 1


# ---------------------------------------------------------------------------
# 7b. The shared rule, and the message that cannot be shared with it
# ---------------------------------------------------------------------------
def test_a_truncated_page_refuses_the_diff_without_offering_the_resolver_remedy():
    """`resolve.list_rows` carries RULE 2 for both callers. The MESSAGE cannot be
    shared, and the first extraction of that function shared it anyway.

    A resolver told a truncated page means "I cannot prove this NAME is unique →
    narrow the search". For a diff, narrowing is the thing that causes the damage:
    every row the page did not carry reads as absent, and absent is what this tool
    reports as a would-delete. Handing a diff operator "narrow the search" is a
    refusal message instructing them to manufacture the failure it just prevented
    — GATE.md:240, a guard's remediation line is part of the guard.
    """
    fetch = _fetcher([{"id": "a1", "name": "ODA"}], total=205)
    with pytest.raises(ResolutionError) as exc:
        diff.target_rows_for(
            kind="STAKEHOLDER", target=LOCAL, space=SPACE, token=None,
            entity_type=None, fetch=fetch,
        )
    message = str(exc.value)
    assert "cannot be diffed" in message
    assert "would-delete" in message
    assert "Narrow the search" not in message
    # and the shared half — the rule itself — is still the resolver's own wording
    assert "has not measured the table" in message


def test_the_diff_walks_every_page_because_the_real_space_does_not_fit_one():
    """MEASURED 2026-08-27: local GFSS holds 986 stakeholders against a 200-row cap.

    A single-page `fm diff` could not run against the real dataset at all — and the
    failure mode if RULE 2 had not been there to refuse is the bad one: 786 rows the
    tool never saw, every one of them printed under "would delete".
    """
    rows = [{"id": f"t-{i}", "name": f"Org {i:04d}"} for i in range(986)]
    calls: list[int] = []
    got, data_field, extra = diff.target_rows_for(
        kind="STAKEHOLDER", target=LOCAL, space=SPACE, token=None,
        entity_type=None, fetch=_paging_fetcher(rows, calls=calls),
    )
    assert len(got) == 986
    assert calls == [0, 200, 400, 600, 800]  # five pages, then the total is met
    assert [r["id"] for r in got] == [r["id"] for r in rows]
    assert data_field is None and extra == {}


def test_a_table_that_changes_mid_walk_refuses_rather_than_mixing_two_states():
    """The rows in hand would be a mixture of two states of the table. `$expect`
    would catch it one step later — but by then the operator has already read and
    approved a report describing a set that never existed.
    """
    rows = [{"id": f"t-{i}", "name": f"Org {i:04d}"} for i in range(400)]
    seen: list[int] = []

    def fetch(target, path, *, token=None, params=None, **kwargs):
        offset = int((params or {}).get("offset", 0))
        seen.append(offset)
        return {"items": rows[offset : offset + 200], "total": 400 + len(seen) - 1,
                "limit": 200, "offset": offset}

    with pytest.raises(ResolutionError, match="changed while it was being listed"):
        diff.target_rows_for(
            kind="STAKEHOLDER", target=LOCAL, space=SPACE, token=None,
            entity_type=None, fetch=fetch,
        )


def test_a_server_that_never_finishes_the_walk_is_refused_not_looped_on():
    """`items: []` beside a non-zero `total` is a server contradiction. Without the
    page ceiling it is an infinite loop; with it, it is a refusal that says so.
    """
    def fetch(target, path, *, token=None, params=None, **kwargs):
        return {"items": [], "total": 5000, "limit": 200,
                "offset": int((params or {}).get("offset", 0))}

    with pytest.raises(ResolutionError) as exc:
        diff.target_rows_for(
            kind="STAKEHOLDER", target=LOCAL, space=SPACE, token=None,
            entity_type=None, fetch=fetch,
        )
    assert "cannot be diffed" in str(exc.value)
    assert "would-delete" in str(exc.value)


def test_the_listing_form_of_find_does_not_claim_a_name_it_was_never_given():
    """`fm get STAKEHOLDER` with no --name printed "STAKEHOLDER None could not be
    proved unique" against the real 986-row space. It names no row, asks about no
    name, and answers a question the operator did not put — the third instance of
    one refusal serving two questions in this module in one evening.
    """
    fetch = _fetcher([{"id": "a1", "name": "ODA"}], total=986)
    with pytest.raises(ResolutionError) as exc:
        resolve_mod.find(None, kind="STAKEHOLDER", target=LOCAL, space=SPACE,
                         fetch=fetch)
    message = str(exc.value)
    assert "None" not in message
    assert "proved unique" not in message
    assert "could not be listed completely" in message


def test_the_resolver_keeps_its_own_refusal_after_the_extraction():
    """The other half of the pair, asserted from this side too: the extraction must
    not quietly reword the resolver, which is the regression that happened.
    """
    fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}], total=205)
    with pytest.raises(ResolutionError) as exc:
        resolve_mod.find("ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE,
                         fetch=fetch)
    message = str(exc.value)
    assert "could not be proved unique" in message
    assert "'ADAFSA'" in message  # the operator's own word is still in the answer


# ---------------------------------------------------------------------------
# 8. Sources
# ---------------------------------------------------------------------------
def test_a_spreadsheet_is_refused_with_the_conversion_rather_than_parsed(tmp_path):
    book = tmp_path / "tracker.xlsx"
    book.write_bytes(b"PK\x03\x04not really")
    with pytest.raises(DiffError) as exc:
        diff.load_source(str(book))
    message = str(exc.value)
    assert "stdlib-only" in message
    assert "openpyxl" in message


def test_csv_and_json_load_to_the_same_rows(tmp_path):
    as_json = tmp_path / "rows.json"
    as_json.write_text(json.dumps([{"name": "FAO", "org": "UN"}]), encoding="utf-8")
    as_csv = tmp_path / "rows.csv"
    as_csv.write_text("name,org\nFAO,UN\n", encoding="utf-8")
    assert diff.load_source(str(as_json)).rows == diff.load_source(str(as_csv)).rows


def test_an_empty_source_is_refused_rather_than_treated_as_delete_everything(tmp_path):
    """The worst possible reading of an empty source is "the target should be
    empty". It is refused before it can be read that way at all.
    """
    empty = tmp_path / "rows.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(DiffError) as exc:
        diff.load_source(str(empty))
    assert "no rows" in str(exc.value)


# ---------------------------------------------------------------------------
# 9. The match must agree with the resolver that will run seconds later
# ---------------------------------------------------------------------------
def test_key_strings_looks_inside_both_shapes_like_the_resolver_does():
    """formist_cli/resolve.py's `_strings_in` yields a plain string as itself and an
    {en, ar} map as its values. If this module had a different idea of "the name of
    this row", it would emit a `$name` that `fm update` cannot resolve.
    """
    from formist_cli.resolve import _strings_in

    for value in ("FAO", {"en": "FAO", "ar": "الفاو"}, None, 7):
        mine = diff.key_strings(value)
        theirs = [diff.normalise_key(s) for s in _strings_in(value)]
        assert mine == theirs, value


def test_an_i18n_named_row_that_matched_is_not_also_a_would_delete():
    """A name stored as `{"en": …, "ar": …}` indexes the same row under two keys —
    resolve.py's match-across-shapes rule, which is right. Counting *keys* rather
    than *rows* then reports the Arabic key of a matched row as a deletion
    candidate, which is a live row on a list headed "would delete".

    Production stores these as maps and development as plain strings, so this shows
    up against prod and not against dev — the shape of bug that reaches the owner.
    """
    result = _build(
        [{"name": "FAO", "org": "UN"}],
        [{"id": "t-1", "name": {"en": "FAO", "ar": "الفاو"}, "org": "UN"}],
    )
    assert result.would_delete == []
    assert result.unchanged == ["FAO"]
