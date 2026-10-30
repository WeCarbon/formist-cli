"""Name to id resolution, the substrate under ``fm get`` and ``fm update``'s
``$name``.

The bulk endpoint's ``$ref`` only resolves within the same batch, backward
to a row the same request just created, so every ``update`` and ``delete``
op still has to carry a literal UUID that was typed or pasted by hand. This
module is the thing that stops the operator from ever having to type one.

Every test below is a refusal, because the refusals are where this module's
value is. A resolver that returns a plausible row when it should have
refused is worse than no resolver: the operator acts on it, and the id
lands in a jsonb column with no foreign key, where nothing errors.
"""

from __future__ import annotations

import pytest

from formist_cli.resolve import (
    KINDS,
    Match,
    ResolutionError,
    find,
    require_kind,
    resolve_one,
)
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


class TestExactlyOneOrRefuse:
    """A name must resolve to exactly one row, or the resolver refuses
    rather than guessing from a truncated id prefix or the first match."""

    def test_single_exact_match_resolves(self):
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}])
        match = resolve_one(
            "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        )
        assert isinstance(match, Match)
        assert match.id == "a1"

    def test_zero_matches_refuses(self):
        fetch = _fetcher([])
        with pytest.raises(ResolutionError, match="matched no STAKEHOLDER"):
            resolve_one(
                "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
            )

    def test_two_exact_matches_refuse_and_never_pick_one(self):
        """A name that identifies two rows identifies neither."""
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}, {"id": "a2", "name": "ADAFSA"}])
        with pytest.raises(ResolutionError) as exc:
            resolve_one(
                "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
            )
        # The operator has to be able to disambiguate, so both ids are named.
        assert "a1" in str(exc.value) and "a2" in str(exc.value)

    def test_substring_hit_is_not_an_identity_match(self):
        """`?q=` is a substring search, so the server returns candidates, not
        answers. Accepting a substring hit as identity would resolve
        'Dr Haddad' onto 'Dr Haddad Jr'."""
        fetch = _fetcher([{"id": "a1", "name": "Dr Haddad Jr"}])
        with pytest.raises(ResolutionError, match="matched no STAKEHOLDER"):
            resolve_one(
                "Dr Haddad", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
            )

    def test_case_difference_is_not_an_exact_match_but_is_reported(self):
        """Folding case silently is how the wrong row gets picked. Refuse, but say
        what was nearly matched: otherwise the operator cannot act on it."""
        fetch = _fetcher([{"id": "a1", "name": "adafsa"}])
        with pytest.raises(ResolutionError) as exc:
            resolve_one(
                "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
            )
        assert "adafsa" in str(exc.value)
        assert "case" in str(exc.value).lower()


class TestTruncatedPageCannotProveUniqueness:
    """Listing is capped at a page size, so a resolver that checks only the
    page it was handed and declares "exactly one match" has measured the
    page, not the table.
    """

    def test_more_rows_than_returned_refuses_even_with_one_exact_match(self):
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}], total=205)
        with pytest.raises(ResolutionError, match="could not be proved unique"):
            resolve_one(
                "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
            )

    def test_untruncated_page_is_fine(self):
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}], total=1)
        assert resolve_one(
            "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        ).id == "a1"


class TestI18nShapeSplit:
    """Production stores `name` as an {en, ar} map; development stores a
    plain string. Same column, same code, different shape, so the resolver
    must match across both shapes while carrying the raw value for
    `$expect`.
    """

    def test_matches_a_value_inside_an_i18n_map(self):
        fetch = _fetcher([{"id": "a1", "name": {"en": "ADAFSA", "ar": "أدافسا"}}])
        assert resolve_one(
            "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        ).id == "a1"

    def test_matches_a_non_english_value_in_the_map(self):
        fetch = _fetcher([{"id": "a1", "name": {"en": "ADAFSA", "ar": "أدافسا"}}])
        assert resolve_one(
            "أدافسا", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        ).id == "a1"

    def test_matches_a_plain_string(self):
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}])
        assert resolve_one(
            "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        ).id == "a1"

    def test_match_value_is_the_RAW_server_value_not_the_matched_string(self):
        """THE test in this file.

        `Match.value` becomes the `$expect` the server re-checks against
        `column_snapshot`, which is `_jsonify(getattr(entity, field))`: the raw
        column. Flattening the map to "ADAFSA" here would send an `$expect` that
        can never match a prod row, turning every prod update into an
        unexplainable `expect-mismatch`. Worse, the reverse (sending a map where a
        string lives) would compare equal to nothing and refuse just as opaquely.

        The value carried forward must be read from the thing being
        verified, never rebuilt locally.
        """
        raw = {"en": "ADAFSA", "ar": "أدافسا"}
        fetch = _fetcher([{"id": "a1", "name": raw}])
        match = resolve_one(
            "ADAFSA", kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch
        )
        assert match.value == raw, "must carry the whole map, not the matched string"
        assert match.field == "name"


class TestKindRegistry:
    def test_every_open_kind_the_bulk_endpoint_writes_is_resolvable(self):
        """A kind that can be written but not resolved forces the operator
        back to a literal UUID, which is the whole failure this module
        exists to remove."""
        writable = {
            "STAKEHOLDER", "SESSION", "TASK", "CUSTOM", "ENTITY_TYPE",
            "SKILL", "SAVED_VIEW", "FILE_LINK", "SESSION_PARTICIPANT",
        }
        assert writable - set(KINDS) == set(), (
            f"writable but not resolvable: {sorted(writable - set(KINDS))}"
        )

    def test_unknown_kind_is_refused_with_the_valid_list(self):
        with pytest.raises(ResolutionError, match="unknown kind"):
            resolve_one(
                "x", kind="WIDGET", target=LOCAL, space=SPACE, fetch=_fetcher([])
            )

    def test_task_resolves_on_subject_not_name(self):
        """TASK has no `name` column: TASK_SPEC searches `subject` instead."""
        fetch = _fetcher([{"id": "t1", "subject": "Draft agenda"}])
        match = resolve_one(
            "Draft agenda", kind="TASK", target=LOCAL, space=SPACE, fetch=fetch
        )
        assert match.id == "t1" and match.field == "subject"

    def test_custom_resolves_through_its_data_map(self):
        """CustomEntity has no name column at all; identity lives in `data`."""
        fetch = _fetcher([{"id": "c1", "data": {"name": "Initiative A"}}])
        match = resolve_one(
            "Initiative A", kind="CUSTOM", target=LOCAL, space=SPACE, fetch=fetch
        )
        assert match.id == "c1"
        assert match.field == "data"
        assert match.value == {"name": "Initiative A"}, "raw column, for $expect"


class TestFindIsTheListingForm:
    def test_find_returns_every_candidate_without_refusing(self):
        """`fm get` lists; `resolve_one` decides. Listing must not refuse on
        ambiguity: showing the operator both rows is the point."""
        fetch = _fetcher([{"id": "a1", "name": "ADAFSA"}, {"id": "a2", "name": "ADNEC"}])
        matches = find(None, kind="STAKEHOLDER", target=LOCAL, space=SPACE, fetch=fetch)
        assert [m.id for m in matches] == ["a1", "a2"]


class TestKindsWithNoNameRefuseWithTheirReason:
    """SESSION_PARTICIPANT and FILE_LINK are join rows. They are writable by
    the bulk endpoint but have no name of their own, so they are present in
    the registry and refusing rather than absent: a kind that is silently
    missing reads as an oversight and gets "helpfully" added.
    """

    @pytest.mark.parametrize("kind", ["SESSION_PARTICIPANT", "FILE_LINK"])
    def test_refuses_by_name_and_says_why(self, kind):
        with pytest.raises(ResolutionError, match="cannot be resolved by name"):
            resolve_one(
                "anything", kind=kind, target=LOCAL, space=SPACE, fetch=_fetcher([])
            )

    @pytest.mark.parametrize("kind", ["SESSION_PARTICIPANT", "FILE_LINK"])
    def test_the_reason_names_what_to_do_instead(self, kind):
        with pytest.raises(ResolutionError) as exc:
            resolve_one(
                "anything", kind=kind, target=LOCAL, space=SPACE, fetch=_fetcher([])
            )
        assert "separately" in str(exc.value), "a refusal must offer a way forward"

    def test_a_no_name_kind_is_still_in_the_registry(self):
        """Totality: absent would read as an oversight, refusing is a decision."""
        assert KINDS["SESSION_PARTICIPANT"].no_name_reason
        assert KINDS["FILE_LINK"].no_name_reason


class TestOneKindNormaliser:
    """Kinds are typed lowercase and positional, and exactly one function
    turns what was typed into a canonical kind.

    It was two: `kind = args.kind.upper().replace("-", "_")` appeared
    verbatim in both `cli.cmd_get` and `diff.cmd_diff`, so a future change
    to kind normalisation could land in one copy and not the other.
    """

    def test_lowercase_is_what_an_operator_types(self):
        assert require_kind("stakeholder") == "STAKEHOLDER"
        assert require_kind("custom") == "CUSTOM"

    def test_case_and_separator_are_both_forgiving(self):
        for spelling in ("saved_view", "saved-view", "SAVED-VIEW", "Saved_View", " saved-view "):
            assert require_kind(spelling) == "SAVED_VIEW", spelling

    def test_an_unknown_kind_names_what_IS_valid(self):
        """"invalid kind" tells an operator nothing they can act on. A misspelling
        needs the spelling."""
        with pytest.raises(ResolutionError) as exc:
            require_kind("stakeholdr")
        message = str(exc.value)
        assert "stakeholdr" in message
        assert "stakeholder" in message          # the thing they meant, listed
        assert "session_participant" in message  # and the rest of the vocabulary
        assert message.count(",") >= 8           # all nine kinds, not a sample

    def test_the_listed_kinds_are_lowercase_like_the_input(self):
        """A message that LISTS `SAVED_VIEW` after accepting `saved-view` teaches
        the operator the wrong thing to type next: and the list is the part they
        copy from.

        Scoped to the first line on purpose. The second line deliberately shows
        `SAVED_VIEW` while explaining that spelling is accepted too, which is worth
        saying to anyone with the old habit; a first draft of this test forbade the
        string anywhere and failed on that sentence. The rule is about what is
        offered, not about never naming the alternative.
        """
        with pytest.raises(ResolutionError) as exc:
            require_kind("nope")
        listing = str(exc.value).splitlines()[0]
        assert "saved_view" in listing
        assert "SAVED_VIEW" not in listing
        # every kind in the list, specifically: not `listing.lower() == listing`,
        # which a second draft tried and which fails on the capital in "Valid".
        # Prose is not a kind.
        offered = listing.split("Valid kinds:")[1]
        assert offered.strip() == offered.strip().lower()

    def test_a_positional_that_swallows_anything_is_the_risk_being_guarded(self):
        """A flag could be omitted; a positional takes whatever the shell hands it.
        Empty, whitespace and None are all refusals rather than a kind of `''`."""
        for junk in (None, "", "   "):
            with pytest.raises(ResolutionError) as exc:
                require_kind(junk)
            assert "kind is required" in str(exc.value) or "unknown kind" in str(exc.value)

    def test_no_command_normalises_a_kind_by_itself(self):
        """The property, structurally: a second copy would not be named the same
        thing, so this looks for the OPERATION."""
        import pathlib

        from formist_cli import resolve as resolve_mod

        package = pathlib.Path(resolve_mod.__file__).parent
        expression = '.upper().replace("-"' + ', "_")'
        offenders = sorted(
            path.name for path in package.glob("*.py")
            if expression in path.read_text() and path.name != "resolve.py"
        )
        assert offenders == [], (
            f"{offenders} normalise a kind inline instead of calling require_kind()"
        )
