"""Name -> id resolution: the reason an operator never types a UUID.

The bulk endpoint's ``$ref`` only resolves a backward, within-batch
reference to a row the same request just created. Every ``update`` and
``delete`` op still has to carry a literal ``$id`` UUID, so a wrong id
typed or copied by hand is exactly the failure this module exists to
prevent: every organisation's ids differ between environments, and a
wrong id written into a column with no foreign key raises nothing.

So resolution happens here, in the client, seconds before the write,
against the environment being written. The wire format the endpoint sees
is unchanged.

Three rules this module keeps:

1. Exactly one row, or refuse. Not "the best match", not "the first".
   Resolving by a shortened id prefix is exactly how a prefix collision
   becomes a correctness bug.

2. A page is not a table. The list endpoints cap a page, so a resolver
   that counts matches in the page it was handed has measured the page.
   When the server's reported total exceeds what came back, uniqueness is
   unproven and this module refuses rather than reporting the count it
   happens to be able to see.

3. Match across shapes; carry the raw value. A name column can hold
   either a plain string or an ``{en, ar}`` map depending on environment,
   so matching looks inside both shapes, but :attr:`Match.value` is the
   raw column value exactly as the server returned it, because it becomes
   the ``$expect`` the server re-checks, an exact, un-coalesced
   comparison. Flattening a map to the matched string here would send an
   ``$expect`` that can never match.

Rule 3 is also what makes the resolution self-checking: the id this
module picks is handed back to the server together with the value it was
picked by, so a mis-resolution is refused by the row itself rather than
written to confidently.

Stdlib only: `fm` must add no runtime dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from formist_cli.cli_support import default_fetch as _default_fetch
from formist_cli.targets import Target, require_space_id

#: What the list endpoints cap a page at. Asking for more is silently
#: clamped, so this is the largest page uniqueness can be proved within;
#: beyond it, :func:`find` refuses instead of guessing.
MAX_PAGE = 200

#: How many pages a paginated walk will take before it gives up and
#: refuses. 50 x 200 = 10,000 rows, comfortably above any real space and
#: low enough that a server answering `items: []` beside a non-zero
#: `total` is reported rather than looped on forever.
MAX_PAGES = 50


class ResolutionError(Exception):
    """A name that does not identify exactly one row. Written for an operator."""


@dataclass(frozen=True)
class KindSpec:
    """How one writable kind is listed and identified.

    ``name_fields`` empty means the kind has no name to resolve by, see
    :attr:`no_name_reason`. The registry stays total over what the bulk
    endpoint will write rather than omitting the awkward ones: a kind that
    is silently absent reads as an oversight and gets "helpfully" added by
    the next person, while a kind that refuses with a reason is a decision
    someone can argue with.
    """

    path: str
    name_fields: tuple[str, ...] = ()
    #: For kinds whose identity lives inside a jsonb column (a custom entity
    #: has no name column at all). The column is searched by its inner keys
    #: but carried whole for ``$expect``.
    data_field: str | None = None
    data_keys: tuple[str, ...] = ("name", "title", "label", "subject")
    no_name_reason: str | None = None


#: Every kind the bulk endpoint will write, and how to find one by name.
#: Two of them are join rows with no name of their own; they are present and
#: refusing rather than absent.
KINDS: dict[str, KindSpec] = {
    # `org` is a legacy wire key kept for rows an old backfill has not yet
    # reached; a stakeholder's organisation now lives at `parent_id`.
    # `_candidates_for` filters on `if field in item`, so once a row no
    # longer carries `org` this entry simply stops contributing to it
    # rather than erroring. Remove it in the same change that drops the
    # column, and resolve by `parent_id` instead if still needed.
    "STAKEHOLDER": KindSpec("/projects/{space}/stakeholders", ("name", "email", "org")),
    # Not "title": the DB column is `Session.title`, but the list endpoint's
    # serialiser renames it on the wire to `name`, and the legacy-input
    # mirror does the same rename in the other direction. `_candidates_for`
    # below filters on `if field in item`, so a spec naming "title" here
    # would match zero keys of every session dict the server ever sends.
    # Resolve by the wire key, not the column.
    "SESSION": KindSpec("/projects/{space}/sessions", ("name",)),
    "TASK": KindSpec("/projects/{space}/tasks", ("subject",)),
    "ENTITY_TYPE": KindSpec(
        "/projects/{space}/entity-types", ("single_name", "plural_name")
    ),
    "SKILL": KindSpec("/projects/{space}/skills", ("name",)),
    "SAVED_VIEW": KindSpec("/projects/{space}/views", ("name",)),
    # A Section resolves by its own name, which is unique per space, so a
    # name is an unambiguous handle here.
    "SECTION": KindSpec("/projects/{space}/sections", ("name",)),
    # SECTION_LINK is deliberately absent: this dict must equal the bulk
    # endpoint's writable kinds, and SECTION_LINK is closed there on
    # purpose. A kind `fm` can resolve is a kind the bulk endpoint can write.
    "CUSTOM": KindSpec("/projects/{space}/custom-entities", data_field="data"),
    "SESSION_PARTICIPANT": KindSpec(
        "/projects/{space}/session-participants",
        no_name_reason=(
            "a SESSION_PARTICIPANT is the join row between a session and a "
            "stakeholder and has no name of its own. Resolve the session and "
            "the stakeholder by name separately, then address this row by "
            "the pair."
        ),
    ),
    "FILE_LINK": KindSpec(
        "/projects/{space}/files",
        no_name_reason=(
            "a FILE_LINK is the join row attaching a file to an entity and has no "
            "name of its own. Resolve the file and the entity separately."
        ),
    ),
}


@dataclass(frozen=True)
class Match:
    """One resolved row.

    :attr:`value` is the raw column value as the server returned it, never
    a rendering of it. It travels into ``$expect`` unchanged.
    """

    id: str
    #: The column that matched, the one whose value goes into ``$expect``.
    field: str
    value: Any
    #: Display text for a human. NEVER used as data.
    label: str


Fetcher = Callable[..., Any]


def _strings_in(value: Any) -> list[str]:
    """Every string a column value could be identified by, across both shapes.

    A plain string yields itself; an ``{en, ar}`` I18nString map yields its values.
    This is for matching only; the raw value is what gets carried forward.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    return []


def _candidates_for(item: dict, spec: KindSpec) -> list[tuple[str, Any, list[str]]]:
    """``(field, raw_value, matchable_strings)`` for one row."""
    if spec.data_field:
        raw = item.get(spec.data_field) or {}
        strings: list[str] = []
        if isinstance(raw, dict):
            for key in spec.data_keys:
                strings.extend(_strings_in(raw.get(key)))
        return [(spec.data_field, raw, strings)]
    return [
        (field, item.get(field), _strings_in(item.get(field)))
        for field in spec.name_fields
        if field in item
    ]


def _label(item: dict, spec: KindSpec) -> str:
    for _field, _raw, strings in _candidates_for(item, spec):
        if strings:
            return strings[0]
    return str(item.get("id", "?"))


def kind_names() -> str:
    """The valid kinds, lowercase, for a message an operator can retype."""
    return ", ".join(sorted(k.lower() for k in KINDS))


def require_kind(raw: str | None) -> str:
    """The one place a typed kind becomes a canonical one, or is refused.

    Every command that takes a kind calls this, rather than each
    normalising its own copy, so a rule like "kinds are typed in
    lowercase" lives in one place instead of several that can disagree
    about what `Stakeholder`, `saved-view` or a typo should do.

    Deliberately not named `canonical_kind`: `chats.py` already has a
    function by that name for an unrelated vocabulary (`background-agent` /
    `user-chat`), and two same-named normalisers over different
    vocabularies invite exactly the kind of collision this module's rules
    exist to prevent.

    Refuses rather than passes an unknown kind through, because a positional
    argument accepts anything the shell hands it. The refusal lists the valid
    kinds: an operator who typed `stakeholdr` needs the spelling, not the word
    "invalid".
    """
    if raw is None or not raw.strip():
        raise ResolutionError(
            f"a kind is required. One of: {kind_names()}."
        )
    canonical = raw.strip().upper().replace("-", "_")
    if canonical not in KINDS:
        raise ResolutionError(
            f"unknown kind {raw!r}. Valid kinds: {kind_names()}.\n"
            "Kinds are case-insensitive and `-` and `_` are interchangeable, so "
            "`saved-view`, `saved_view` and `SAVED_VIEW` are the same kind, "
            f"but {raw!r} is not one of them under any spelling."
        )
    return canonical


def _spec(kind: str) -> KindSpec:
    spec = KINDS.get(kind)
    if spec is None:
        raise ResolutionError(
            f"unknown kind {kind!r}. Valid kinds: {kind_names()}."
        )
    return spec


def list_rows(
    *,
    kind: str,
    target: Target,
    space: str,
    token: str | None = None,
    fetch: Fetcher | None = None,
    q: str | None = None,
    subject: str | None = None,
    remedy: str | None = None,
    paginate: bool = False,
) -> list[dict]:
    """Every row of ``kind`` in ``space``, as the server sent them.

    Extracted from :func:`find` rather than copied, because it carries rule 2
    and a second copy of that rule is a second place for it to rot: a page
    is not a table, so when the server says there are more rows than it
    sent, no statement about the set is available, including a negative
    one. ``fm diff`` needs whole rows (``find`` returns only the matched
    column), and a diff that measured the page it was handed would report
    every unseen row as a would-delete.

    ``subject`` and ``remedy`` exist because the rule is shared and the
    message cannot be: a resolver asked "is this name unique?" must not be
    answered "the table could not be listed completely", which is true of
    the table, not of the question, and drops the name the operator typed.
    The remedies differ too: a resolver can narrow its search, and a diff
    cannot, it needs every row or none.

    ``paginate`` walks ``offset`` until the rows in hand equal the ``total``
    the server reported, and is how a caller that needs the whole table
    gets one. It does not weaken rule 2, it satisfies it: the refusal below
    still fires whenever the walk ends holding fewer rows than the server
    claims exist.
    """
    spec = _spec(kind)
    space = require_space_id(space)
    fetch = fetch or _default_fetch
    path = spec.path.format(space=space)

    items: list[dict] = []
    total: Any = None
    offset = 0
    # A page that returns nothing, or a total that moves under the walk, ends
    # it, and the reconciliation below turns that into a refusal. Without the
    # cap a server that always answers `items: []` with a non-zero `total`
    # would spin forever; with it, the same condition is reported as what it is.
    for _ in range(MAX_PAGES):
        params = {"limit": str(MAX_PAGE), "offset": str(offset)}
        if q:
            params["q"] = q
        payload = fetch(target, path, token=token, params=params)
        if not isinstance(payload, dict):
            raise ResolutionError(
                f"listing {kind} returned {type(payload).__name__}, not a list envelope."
            )
        page = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
        page_total = payload.get("total")
        if total is None:
            total = page_total
        elif isinstance(total, int) and page_total != total:
            # The table changed while it was being read. A plan built from an
            # inconsistent scan is exactly the thing `$expect` exists to catch one
            # step later, and catching it there means the operator has already read
            # and approved a report describing a set that never existed.
            raise ResolutionError(
                f"{kind} changed while it was being listed: the server reported "
                f"{total} rows and then {page_total} mid-scan.\n"
                "The rows in hand are a mixture of two states of the table, and no "
                "statement about the set is available.\n"
                "-> Retry when the space is not being written to."
            )
        items.extend(page)
        offset += MAX_PAGE
        if not paginate or not page or not isinstance(total, int) or len(items) >= total:
            break

    if isinstance(total, int) and total > len(items):
        capped = (
            f"the walk stopped after {MAX_PAGES} pages"
            if paginate
            else f"the list endpoint caps a page at {MAX_PAGE}"
        )
        raise ResolutionError(
            (subject or f"{kind} could not be listed completely")
            + f": the server reports {total} matching rows but returned "
            f"{len(items)} ({capped}).\n"
            "Refusing rather than counting the page: a caller that measures the "
            "page it was handed has not measured the table.\n"
            "-> " + (remedy or "Narrow the search, or address these rows by id.")
        )
    return items


def find(
    name: str | None,
    *,
    kind: str,
    target: Target,
    space: str,
    token: str | None = None,
    fetch: Fetcher | None = None,
) -> list[Match]:
    """Every row of ``kind`` whose name matches ``name`` exactly, as candidates.

    ``name`` of None lists the kind (this is ``fm get``'s listing form).
    Listing deliberately does not refuse on ambiguity: showing the operator
    both rows is the entire point of the command.

    ``?q=`` narrows server-side but only ilike-style, so it selects
    candidates; identity is decided here, exactly. Accepting the server's
    substring hit as an answer would resolve "Dr Haddad" onto "Dr Haddad Jr".
    """
    spec = _spec(kind)
    # The two forms of this call are two different questions and must not share a
    # refusal: `fm get STAKEHOLDER` with no --name printed "STAKEHOLDER None could
    # not be proved unique", which names no row, asks about no name, and answers a
    # question the operator did not put. Uniqueness is only at stake when a name is.
    if name is None:
        subject = f"{kind} could not be listed completely"
        remedy = (
            "Narrow with --name, or read the rows through the API directly: a "
            "listing that shows some of the table looks exactly like one that shows "
            "all of it."
        )
    else:
        subject = f"{kind} {name!r} could not be proved unique"
        remedy = "Narrow the search, or address this row by its id."
    items = list_rows(
        kind=kind, target=target, space=space, token=token, fetch=fetch, q=name,
        subject=subject, remedy=remedy,
    )

    matches: list[Match] = []
    for item in items:
        for field, raw, strings in _candidates_for(item, spec):
            if name is None or name in strings:
                matches.append(
                    Match(
                        id=str(item.get("id")),
                        field=field,
                        value=raw,
                        label=_label(item, spec),
                    )
                )
                break
    return matches


def resolve_one(
    name: str,
    *,
    kind: str,
    target: Target,
    space: str,
    token: str | None = None,
    fetch: Fetcher | None = None,
) -> Match:
    """The single row named ``name``, or a refusal explaining what was found.

    Rule 1. There is no "closest match" branch and there must never be one.
    """
    spec = _spec(kind)
    if spec.no_name_reason:
        raise ResolutionError(
            f"{kind} cannot be resolved by name: {spec.no_name_reason}"
        )

    matches = find(
        name, kind=kind, target=target, space=space, token=token, fetch=fetch
    )

    if len(matches) == 1:
        return matches[0]

    if not matches:
        # Say what was NEARLY matched, or the operator cannot act on the refusal.
        # Case is never folded silently: folding it is how the wrong row is picked.
        near = find(
            None, kind=kind, target=target, space=space, token=token, fetch=fetch
        )
        folded = [m for m in near if m.label.casefold() == name.casefold()]
        hint = ""
        if folded:
            hint = (
                "\nA row differs from it only by case: "
                + ", ".join(f"{m.label!r} ({m.id})" for m in folded)
                + "\n`fm` does not fold case when resolving: matching "
                "case-insensitively is how a resolver picks the wrong row. Use the "
                "name exactly as stored."
            )
        raise ResolutionError(
            f"{name!r} matched no {kind} in this space ({target.name}).{hint}"
        )

    listed = "\n".join(f"    {m.id}  {m.label!r}" for m in matches)
    raise ResolutionError(
        f"{name!r} matched {len(matches)} {kind} rows in this space ({target.name}); "
        "a name that identifies two rows identifies neither:\n"
        f"{listed}\n"
        "-> Address the one you mean by its id, or give a name that is unique."
    )
