"""Name → id resolution: the reason an operator never types a UUID.

WHY THIS EXISTS
---------------
The bulk endpoint's ``$ref`` looks like it solves this and does not. It is a
strictly BACKWARD, WITHIN-BATCH reference to a row the same request just created
(``services/bulk_data.py:201-227``), filled only from an ``add``'s result. So every
``update`` and ``delete`` op still has to carry a literal ``$id`` UUID — the
endpoint hands the transcribed-id failure straight back to the operator, which is
precisely the failure that must not happen: every organisation id differs between
dev and prod, and a wrong id written into a jsonb column with no FK raises nothing.
A handover document whose id shared its first eight characters with the right one
nearly did exactly that.

So resolution happens HERE, in the client, seconds before the write, against the
environment being written. The wire format the endpoint sees is unchanged.

THE THREE RULES THIS MODULE KEEPS
---------------------------------
**1. Exactly one row, or refuse.** Not "the best match", not "the first". Of the
~20 operator scripts written on 2026-08-26/27, exactly one got this right
(``scratchpad/apply_ia_dry.py:9-13``: ``if len(r)!=1: raise SystemExit``). Every
other one hardcoded an 8-character id prefix and demoted the name to a post-hoc
guard — which is how the prefix became a primary key over a table where a prefix
collision has already been observed.

**2. A page is not a table.** ``parse_pagination`` caps ``limit`` at
``MAX_LIMIT = 200`` (``utils/list_query.py:60``), so a resolver that counts matches
in the page it was handed has measured the page. When ``total`` exceeds what came
back, uniqueness is UNPROVEN and this module refuses rather than reporting the
count it happens to be able to see.

**3. Match across shapes; carry the raw value.** Production stores ``name`` as an
``{en, ar}`` map and development stores a plain string — same column, same code
(``services/bulk_data.py:260-272``, which names the 14 prod rows a coalescing probe
would have silently converted from string to map). Matching therefore looks inside
both shapes, but :attr:`Match.value` is the RAW column value exactly as the server
returned it, because it becomes the ``$expect`` the server re-checks against
``column_snapshot`` — an exact, un-coalesced comparison. Flattening a map to the
matched string here would send an ``$expect`` that can never match, turning every
prod update into an unexplainable ``expect-mismatch``.

Rule 3 is also what makes the resolution self-checking: the id this module picks is
handed back to the server together with the value it was picked by, so a
mis-resolution is refused by the row itself rather than written to confidently.

Stdlib only — ``ev`` must add no runtime dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from formist_cli.cli_support import default_fetch as _default_fetch
from formist_cli.targets import Target, require_space_id

#: What the list endpoints cap a page at (``utils/list_query.py:60``). Asking for
#: more is silently clamped, so this is the largest page uniqueness can be proved
#: within — beyond it, :func:`find` refuses instead of guessing.
MAX_PAGE = 200

#: How many pages a paginated walk will take before it gives up and refuses. 50 ×
#: 200 = 10,000 rows, comfortably above the largest real space (986) and low enough
#: that a server answering `items: []` beside a non-zero `total` is reported rather
#: than looped on forever.
MAX_PAGES = 50


class ResolutionError(Exception):
    """A name that does not identify exactly one row. Written for an operator."""


@dataclass(frozen=True)
class KindSpec:
    """How one writable kind is listed and identified.

    ``name_fields`` empty means the kind has no name to resolve BY — see
    :attr:`no_name_reason`. The registry stays TOTAL over what the bulk endpoint
    will write (``services/bulk_data.py`` ``OPEN_KINDS``) rather than omitting the
    awkward ones, for the same reason that module enumerates its closed kinds with
    reasons: a kind that is silently absent reads as an oversight and gets
    "helpfully" added by the next person, while a kind that refuses with a reason
    is a decision someone can argue with.
    """

    path: str
    name_fields: tuple[str, ...] = ()
    #: For kinds whose identity lives inside a jsonb column (CustomEntity has no
    #: name column at all — ``routes/entities.py::_custom_entity_label``). The
    #: column is searched by its inner keys but carried whole for ``$expect``.
    data_field: str | None = None
    data_keys: tuple[str, ...] = ("name", "title", "label", "subject")
    no_name_reason: str | None = None


#: Every kind ``services/bulk_data.py`` will write, and how to find one by name.
#: Two of them are join rows with no name of their own; they are present and
#: refusing rather than absent.
KINDS: dict[str, KindSpec] = {
    # ⚠️ `org` HERE IS THE WIRE KEY, AND IT IS ON BORROWED TIME (D423). The
    # server stopped WRITING that column in phase (c) — a stakeholder's
    # organisation is `parent_id` — but `participant_dict` still SERIALISES the
    # legacy text for rows the prod backfill has not reached, so resolving an
    # operator's `$name` against it still works and still helps. When phase (d)
    # drops the column the key disappears from the payload; `_candidates_for`
    # filters on `if field in item`, so this entry then matches zero keys and
    # silently stops contributing rather than erroring — the same trap the
    # SESSION comment below records. Remove it in the same change as the column,
    # and if resolving by organisation is still wanted, resolve through
    # `parent_id` instead. See scratchpad/0910/org-reference/PHASE-D.md.
    "STAKEHOLDER": KindSpec("/projects/{space}/stakeholders", ("name", "email", "org")),
    # NOT "title" — the DB column is `Session.title`, but the list endpoint's
    # serialiser renames it on the wire: `session_dict` returns `"name": s.title`
    # (routes/sessions.py:93), and the legacy-input mirror does the same rename
    # in the other direction (routes/sessions.py:60). `_candidates_for` below
    # filters on `if field in item`, so a spec naming "title" here would match
    # ZERO keys of every session dict the server ever sends and every session
    # would silently resolve to nothing. Resolve by the wire key, not the column.
    "SESSION": KindSpec("/projects/{space}/sessions", ("name",)),
    "TASK": KindSpec("/projects/{space}/tasks", ("subject",)),
    "ENTITY_TYPE": KindSpec(
        "/projects/{space}/entity-types", ("single_name", "plural_name")
    ),
    "SKILL": KindSpec("/projects/{space}/skills", ("name",)),
    "SAVED_VIEW": KindSpec("/projects/{space}/views", ("name",)),
    # A Section resolves by its own name, which is unique per space
    # (uq_section_project_name), so a name is an unambiguous handle here.
    "SECTION": KindSpec("/projects/{space}/sections", ("name",)),
    # SECTION_LINK is deliberately ABSENT. This dict must equal `OPEN_KINDS`
    # (tests/test_evtool_resolve.py asserts set equality), and SECTION_LINK is
    # CLOSED for the bulk endpoint on purpose — see services/bulk_data.py. Adding it
    # here would red that test, which is the guard working: a kind ev can resolve is
    # a kind bulk can write.
    "CUSTOM": KindSpec("/projects/{space}/custom-entities", data_field="data"),
    "SESSION_PARTICIPANT": KindSpec(
        "/projects/{space}/session-participants",
        no_name_reason=(
            "a SESSION_PARTICIPANT is the join row between a session and a "
            "stakeholder and has no name of its own (SESSION_PARTICIPANT_SPEC "
            "declares no searchable field — routes/session_participants.py:37). "
            "Resolve the session and the stakeholder by name separately, then "
            "address this row by the pair."
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

    :attr:`value` is the raw column value as the server returned it — never a
    rendering of it. It travels into ``$expect`` unchanged.
    """

    id: str
    #: The column that matched — the one whose value goes into ``$expect``.
    field: str
    value: Any
    #: Display text for a human. NEVER used as data.
    label: str


Fetcher = Callable[..., Any]


def _strings_in(value: Any) -> list[str]:
    """Every string a column value could be identified by, across both shapes.

    A plain string yields itself; an ``{en, ar}`` I18nString map yields its values.
    This is for MATCHING only — the raw value is what gets carried forward.
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
    """The ONE place a typed kind becomes a canonical one, or is refused.

    Every command that takes a kind calls this. It was two copies —
    `kind = args.kind.upper().replace("-", "_")` appeared verbatim in `cli.cmd_get`
    and `diff.cmd_diff` — which is three commands' worth of places to disagree
    about what `Stakeholder`, `saved-view` or a typo should do. The owner's ruling
    that kinds are typed in LOWERCASE is exactly the kind of change that would have
    landed in one copy and not the other.

    Deliberately NOT named `canonical_kind`: `evtool/chats.py` already has a
    function by that name for an unrelated vocabulary (`background-agent` /
    `user-chat`). Two same-named normalisers over different vocabularies is the
    `MAX_PAGES` collision this package already carries once, and once is enough.

    Refuses rather than passes an unknown kind through, because a positional
    argument accepts anything the shell hands it. The refusal LISTS the valid
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
            "`saved-view`, `saved_view` and `SAVED_VIEW` are the same kind — "
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

    Extracted from :func:`find` rather than copied, because it carries RULE 2 and
    a second copy of that rule is a second place for it to rot: a page is not a
    table, so when the server says there are more rows than it sent, no statement
    about the set is available — including a negative one. ``fm diff`` needs whole
    rows (``find`` returns only the matched column), and a diff that measured the
    page it was handed would report every unseen row as a would-delete.

    ``subject`` and ``remedy`` exist because the RULE is shared and the MESSAGE
    cannot be. The first extraction of this function let one refusal serve both
    callers, and `tests/test_evtool_resolve.py:112` caught it: a resolver asked
    "is this name unique?" and was answered "the table could not be listed
    completely" — true of the table, not of the question, and it dropped the name
    the operator had typed. That is the same defect as a report about the wrong
    object, one layer down. The remedies differ too: a resolver can narrow its
    search, and a diff cannot — it needs every row or none.

    ``paginate`` walks ``offset`` until the rows in hand equal the ``total`` the
    server reported, and is how a caller that needs the TABLE gets one. It does not
    weaken RULE 2, it satisfies it: the refusal below still fires whenever the walk
    ends holding fewer rows than the server claims exist. Measured 2026-08-27 —
    a real local space held **986** stakeholders against a 200-row cap, so a single-page
    ``fm diff`` could not run against the real dataset at all, and every one of the
    786 rows it never saw would have been reported as a would-delete.
    """
    spec = _spec(kind)
    space = require_space_id(space)
    fetch = fetch or _default_fetch
    path = spec.path.format(space=space)

    items: list[dict] = []
    total: Any = None
    offset = 0
    # A page that returns nothing, or a total that moves under the walk, ends it —
    # and the reconciliation below turns that into a refusal. Without the cap a
    # server that always answers `items: []` with a non-zero `total` would spin
    # forever; with it, the same condition is reported as what it is.
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
                "→ Retry when the space is not being written to."
            )
        items.extend(page)
        offset += MAX_PAGE
        if not paginate or not page or not isinstance(total, int) or len(items) >= total:
            break

    if isinstance(total, int) and total > len(items):
        capped = (
            f"the walk stopped after {MAX_PAGES} pages"
            if paginate
            else f"the list endpoint caps a page at {MAX_PAGE} — utils/list_query.py:60"
        )
        raise ResolutionError(
            (subject or f"{kind} could not be listed completely")
            + f": the server reports {total} matching rows but returned "
            f"{len(items)} ({capped}).\n"
            "Refusing rather than counting the page: a caller that measures the "
            "page it was handed has not measured the table.\n"
            "→ " + (remedy or "Narrow the search, or address these rows by id.")
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

    ``name`` of None lists the kind (this is ``fm get``'s listing form). Listing
    deliberately does NOT refuse on ambiguity — showing the operator both rows is
    the entire point of the command.

    ``?q=`` narrows server-side but only ilike-style
    (``utils/list_query.py:220-223``), so it selects CANDIDATES; identity is decided
    here, exactly. Accepting the server's substring hit as an answer would resolve
    "Dr Haddad" onto "Dr Haddad Jr".
    """
    spec = _spec(kind)
    # The two forms of this call are two different questions and must not share a
    # refusal — `fm get STAKEHOLDER` with no --name printed "STAKEHOLDER None could
    # not be proved unique", which names no row, asks about no name, and answers a
    # question the operator did not put. Uniqueness is only at stake when a name is.
    if name is None:
        subject = f"{kind} could not be listed completely"
        remedy = (
            "Narrow with --name, or read the rows through the API directly — a "
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
            f"{kind} cannot be resolved by name — {spec.no_name_reason}"
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
                "\nA row differs from it only by CASE: "
                + ", ".join(f"{m.label!r} ({m.id})" for m in folded)
                + "\nev does not fold case when resolving — matching "
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
        "→ Address the one you mean by its id, or give a name that is unique."
    )
