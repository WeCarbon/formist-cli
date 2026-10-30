"""Tests for ``fm diff``.

Covers key normalisation (whitespace, case, typography), shape-sensitive value
comparison (a bare string vs an ``{"en": ...}`` locale map), ambiguous-row
refusal, and the create/update/delete report contract that ``fm update``
consumes.
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
    """A stand-in that honours ``offset``, unlike ``_fetcher`` above.

    ``_fetcher`` returns the same page regardless of offset, which would hide a
    pagination bug: the walk would "work" because every page happens to contain
    the answer.
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
# 1. The match key: the trailing-whitespace family
# ---------------------------------------------------------------------------
def test_the_two_characters_a_bare_strip_cannot_reach():
    """The normalisation table exists for U+200B and U+FEFF specifically.

    U+00A0 (NBSP) is already handled by ``.strip()``, ``\\s`` and NFKC alone.
    U+200B (ZERO WIDTH SPACE) and U+FEFF (ZERO WIDTH NO-BREAK SPACE) survive all
    three, so the table is the only thing that normalises them. Most other tests
    in this file use U+00A0, which would stay green even if the table dropped
    the zero-width entries; the assertions below are the ones that would catch
    that regression.
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
    """``resolve`` refuses to fold case, because folding is how a resolver picks
    the wrong row. The diff's own key matching must agree, or it could emit a
    ``$name`` that ``fm update`` then refuses to resolve.
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
    """Same property as above, for the character class generic stripping misses.

    U+200B is invisible in every terminal, diff view and spreadsheet cell, so an
    operator cannot see or report a duplicate it causes: a second row would be
    created under a name that renders identically to the first.
    """
    result = _build(
        [{"name": "ODA​", "org": "UN"}],
        [{"id": "t-1", "name": "ODA", "org": "UN"}],
    )
    assert result.creates == []
    assert result.unchanged == ["ODA"]
    assert result.normalised == [("ODA​", "ODA")]


def test_a_create_writes_the_normalised_key_not_the_raw_text():
    """Writing the raw, untrimmed text back would create a row this same diff
    fails to match on the next run, and creates again.
    """
    result = _build([{"name": "New Org\xa0", "org": "X"}], [])
    assert result.creates[0].set_payload["name"] == "New Org"


# ---------------------------------------------------------------------------
# 2. Shape is part of a value
# ---------------------------------------------------------------------------
def test_a_bare_string_never_equals_an_en_map():
    """A `notes` column can hold a bare string in one environment and an
    ``{"en": ...}`` locale map in another, for the same field. The server
    refuses to coalesce the two shapes silently, so the diff must not treat
    them as equal either.
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
    """`str({"en": "FAO"})` is a Python dict repr (single quotes), which a reader
    comparing two printed values by eye could mistake for equivalent JSON. Every
    printed value must go through ``json.dumps`` instead.
    """
    shown = diff._show({"en": "FAO"})
    assert shown == '{"en": "FAO"}'
    assert "'" not in shown


def test_the_DECISION_path_never_compares_through_str_either():
    """``_show`` is the display path; ``values_equal`` is the decision path that
    decides whether to write. Both must avoid comparing via ``str()``.

    Two dicts that are equal but built in a different key order compare equal
    with ``==`` but print differently with ``str()`` (a jsonb column
    round-tripped with ``sort_keys`` on one side and not the other is exactly
    this). A ``str()``-based comparison would call them different and propose
    writing a value the target already holds.
    """
    left = {"en": "FAO", "ar": "الفاو"}
    right = {"ar": "الفاو", "en": "FAO"}
    assert left == right
    assert str(left) != str(right)  # the trap this test guards against
    assert diff.values_equal(left, right) == (True, "identical")

    # the same trap one level down, inside a list
    assert diff.values_equal([{"a": 1, "b": 2}], [{"b": 2, "a": 1}]) == (True, "identical")

    # end to end: a key-order difference must not become an update op
    result = _build(
        [{"name": "FAO", "notes": {"en": "x", "ar": "y"}}],
        [{"id": "t-1", "name": "FAO", "notes": {"ar": "y", "en": "x"}}],
    )
    assert result.updates == []
    assert result.unchanged == ["FAO"]


def test_a_whitespace_only_value_difference_is_reported_as_such():
    """Not equal, so the value is written exactly, but the reason distinguishes
    it from a real edit, which is what a reviewer needs to decide whether to
    skip it.
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
    """NULL is absence, not a shape, and the two directions are not symmetric.

    A source null over a populated column would be a field-level delete, and
    this module's rule is that the source is never authority to delete; that
    direction stays blocked behind ``--allow-shape-change``.
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
    """A proper noun can be stored identically in both halves of an `{en, ar}`
    name map. `key_strings` would then yield the same key twice, indexing the
    row against itself and refusing the diff with the same id printed as both
    of the "conflicting" rows, a message with no possible remedy.
    """
    target = [{"id": "t-1", "name": {"en": "Acme Holdings", "ar": "Acme Holdings"}}]
    result = _build([{"name": "Acme Holdings", "org": "PRIVATE"}], target)
    assert result.updates[0].key == "Acme Holdings"
    assert result.would_delete == []
    assert result.ambiguous == []


def test_an_ambiguity_the_source_never_names_is_reported_not_fatal():
    """A duplicate name the source never mentions is reported, not fatal: a row
    no source key resolves to is a row nothing was ever going to write, so
    refusing the whole run over it would make the command unusable. The
    refusal stays reserved for the case that is actually dangerous: a source
    key that resolves to two target rows.
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
    """The other side of the split above: the case where a wrong row could be
    written is refused outright, and nothing is compared.
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
    """The duplicate-source check compares normalised source keys, which cannot
    see this case: 'FAO' and its Arabic translation are different text that
    resolve to the same row, because a name map indexes its row under every
    locale it holds. Two ops for one row would mean the later one silently
    wins, the exact failure the key check exists to prevent, arriving by a
    route the key check alone cannot cover.
    """
    target = [{"id": "t-1", "name": {"en": "FAO", "ar": "الفاو"}}]
    with pytest.raises(DiffError) as exc:
        _build([{"name": "FAO", "org": "UN"}, {"name": "الفاو", "org": "UN-AR"}], target)
    assert "different names for the SAME STAKEHOLDER row" in str(exc.value)
    assert "t-1" in str(exc.value)
    assert "Nothing was compared" in str(exc.value)


def test_every_target_row_lands_in_exactly_one_bucket():
    """The reported target count must be read independently from the rows, not
    derived from the same buckets it is meant to check (summing only some of
    the buckets would silently under-report the total).
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
    """Tested at its own level because the locale-collision guard one layer up
    in `build_diff` rejects the only input that would reach this path there, so
    the reconciliation arithmetic and its wiring into `build_diff` (asserted
    below) must each be tested directly; neither alone would be enough.
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
    """Row ids differ between environments, so no id crosses this boundary at
    all: `$name` is re-resolved by `fm update` against the environment being
    written.
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
    """`$expect` must come from a read that is a separate command, with a human
    review between that read and the eventual write, not from a read done
    moments earlier in the same command (which could never fail as a
    staleness check).

    The key field is deliberately absent, so `fm update`'s own copy-back fills
    it from its own resolution rather than from this file.
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
    """The bulk-write endpoint forbids extra keys, so the deletion list and the
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
# 6. A jsonb-backed kind writes a whole column
# ---------------------------------------------------------------------------
def test_a_custom_entity_update_merges_rather_than_replacing_the_blob():
    """A CustomEntity has one writable column, `data`, so `$set` writes the
    whole blob. A naive whole-column write would silently delete every field
    the source did not know about, so the diff must merge first.
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
    """The resolver shares its truncated-page rule with the diff, but not its
    message. A resolver told a truncated page means "narrow the search", but
    for a diff, narrowing is exactly what causes the damage: every row the
    page did not carry reads as absent, which this tool reports as a
    would-delete, so a diff refusal must not suggest narrowing the search.
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
    # the shared half, the rule itself, is still the resolver's own wording
    assert "has not measured the table" in message


def test_the_diff_walks_every_page_because_the_real_space_does_not_fit_one():
    """A single-page `fm diff` could not run against a dataset of this size at
    all. Without pagination the rows past the first page would never be seen
    and would all be reported under "would delete".
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
    would catch it one step later, but by then the operator has already read
    and approved a report describing a set that never existed.
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
    """`fm get STAKEHOLDER` with no `--name` must not reuse the "could not be
    proved unique" message: it names no row and asks about no name, so it
    needs its own refusal rather than answering a question that was never
    asked.
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
    """Asserted from this side too: the shared extraction must not reword the
    resolver's own message.
    """
    fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}], total=205)
    with pytest.raises(ResolutionError) as exc:
        resolve_mod.find("ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE,
                         fetch=fetch)
    message = str(exc.value)
    assert "could not be proved unique" in message
    assert "'ADAFSA'" in message  # the operator's own search term is still in the answer


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
    """`resolve._strings_in` yields a plain string as itself and an {en, ar}
    map as its values. If this module had a different idea of "the name of
    this row", it would emit a `$name` that `fm update` cannot resolve.
    """
    from formist_cli.resolve import _strings_in

    for value in ("FAO", {"en": "FAO", "ar": "الفاو"}, None, 7):
        mine = diff.key_strings(value)
        theirs = [diff.normalise_key(s) for s in _strings_in(value)]
        assert mine == theirs, value


def test_an_i18n_named_row_that_matched_is_not_also_a_would_delete():
    """A name stored as `{"en": ..., "ar": ...}` indexes the same row under two
    keys, which is correct for matching across shapes. Counting *keys* rather
    than *rows* would then wrongly report the Arabic key of a matched row as a
    deletion candidate.
    """
    result = _build(
        [{"name": "FAO", "org": "UN"}],
        [{"id": "t-1", "name": {"en": "FAO", "ar": "الفاو"}, "org": "UN"}],
    )
    assert result.would_delete == []
    assert result.unchanged == ["FAO"]
