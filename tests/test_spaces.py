"""Tests for ``fm spaces``: the CLI front end for ``/admin/spaces*`` (list,
read sub-surfaces, and the six irreversible writes).

Two shapes covered:

  1. The render functions' "never overstate what was fetched" contract
     (``pagination_footer``'s three cases, applied at four call sites) and
     the "no members / no rows" empty-state messages.
  2. The pre-network refusals on the six write subjects: `delete` without
     `--confirm`, `rename` without `--name`, `transfer-ownership` without
     `--new-owner`, a delete-child without a child id, and any write with
     no `space_id` at all. Each is asserted to refuse without calling the
     API at all: a write that "fails safe" by silently not sending the
     request it should have refused earlier is the wrong kind of safe.
"""

from __future__ import annotations

import argparse
import types

import pytest

from formist_cli import spaces
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
OTHER_USER = "11111111-1111-1111-1111-111111111111"


# ===========================================================================
# canonical_subject
# ===========================================================================
def test_default_subject_is_list():
    assert spaces.canonical_subject(None) == "list"


def test_an_unknown_subject_names_every_valid_one():
    with pytest.raises(spaces.SpacesError) as exc:
        spaces.canonical_subject("archive")
    msg = str(exc.value)
    for subj in spaces.SUBJECTS:
        assert subj in msg, f"{subj} missing from the refusal message"


# ===========================================================================
# render_space_list / render_members: the SHOWN-vs-TOTAL contract
# ===========================================================================
def test_render_space_list_reports_region_and_member_count():
    out = spaces.render_space_list(
        {"items": [{"region": "ae", "member_count": 4, "name": "Acme Summit", "id": SPACE}],
         "total": 1, "offset": 0}
    )
    assert "ae" in out and "4" in out and "Acme Summit" in out and "1 of 1" in out


def test_render_space_list_more_exist_names_the_next_offset():
    items = [{"region": "global", "member_count": 1, "name": f"s{i}", "id": SPACE} for i in range(5)]
    out = spaces.render_space_list({"items": items, "total": 50, "offset": 0})
    assert "MORE EXIST" in out


def test_render_members_empty_says_so():
    out = spaces.render_members({"items": []})
    assert out == "(this space has no members)"


def test_render_members_lists_email_and_role():
    out = spaces.render_members(
        {"items": [{"email": "a@x.com", "name": "A", "role": "OWNER", "user_id": OTHER_USER}]}
    )
    assert "a@x.com" in out and "OWNER" in out


# ===========================================================================
# the shared _paginated body: conversations / skills / invitations
# ===========================================================================
def test_render_conversations_empty_names_the_noun():
    assert spaces.render_conversations({"items": []}) == "(this space has no conversations)"


def test_render_conversations_marks_archived():
    out = spaces.render_conversations(
        {"items": [{"title": "T", "model": "gpt-4o", "archived": True, "id": "c-1"}],
         "total": 1, "offset": 0}
    )
    assert "archived" in out


def test_render_skills_shows_enabled_vs_disabled():
    out = spaces.render_skills(
        {"items": [
            {"name": "writer", "enabled": True, "id": "sk-1"},
            {"name": "reviewer", "enabled": False, "id": "sk-2"},
        ], "total": 2, "offset": 0}
    )
    enabled_line = next(ln for ln in out.splitlines() if "writer" in ln)
    disabled_line = next(ln for ln in out.splitlines() if "reviewer" in ln)
    assert "enabled" in enabled_line
    assert "disabled" in disabled_line


def test_render_invitations_empty_names_the_noun():
    assert spaces.render_invitations({"items": []}) == "(this space has no invitations)"


def test_paginated_more_exist_notice_is_consistent_across_all_three_listings():
    for renderer, rows_key in (
        (spaces.render_conversations, "title"),
        (spaces.render_skills, "name"),
        (spaces.render_invitations, "email"),
    ):
        out = renderer({"items": [{rows_key: "v", "id": "x"}], "total": 99, "offset": 0})
        assert "MORE EXIST" in out, renderer.__name__


# ===========================================================================
# render_usage: the two DIFFERENT numbers, both shown, never conflated
# ===========================================================================
def test_render_usage_shows_invoiced_and_current_cycle_side_by_side():
    out = spaces.render_usage(
        {
            "plan": "Evane Max", "status": "active", "seats": 5,
            "current_period_end": "2026-10-01",
            "tokens_used_this_period": 1000, "included_tokens": 5000, "overage_tokens": 0,
            "current_cycle": {"standard_tokens": 1200, "calls": 30},
        }
    )
    assert "invoiced" in out and "1000" in out and "5000" in out
    assert "current_cycle" in out and "1200" in out


def test_render_usage_with_no_plan_says_none_rather_than_nothing():
    out = spaces.render_usage({"plan": None, "status": None, "seats": 0})
    assert "(none)" in out


# ===========================================================================
# render_rename / render_delete / render_transfer / render_child_delete
# ===========================================================================
def test_render_rename():
    assert spaces.render_rename({"id": SPACE, "name": "New Name"}) == f"renamed  {SPACE} -> 'New Name'"


def test_render_delete_shows_row_and_blob_counts():
    out = spaces.render_delete({"deleted": SPACE, "rows": 4021, "blobs": 17})
    assert "DELETED" in out and "4021" in out and "17" in out


def test_render_transfer_shows_old_and_new_owner():
    out = spaces.render_transfer(
        {"space_id": SPACE, "old_owner_user_id": "old-1", "new_owner_user_id": "new-1",
         "old_owner_demoted_to": "ADMIN"}
    )
    assert "old-1" in out and "new-1" in out and "ADMIN" in out


def test_render_transfer_with_no_prior_owner_says_unchanged():
    out = spaces.render_transfer(
        {"space_id": SPACE, "old_owner_user_id": None, "new_owner_user_id": "new-1",
         "old_owner_demoted_to": None}
    )
    assert "unchanged" in out


def test_render_child_delete_names_the_noun():
    assert "conversation" in spaces.render_child_delete({"deleted": "c-1"}, noun="conversation")


# ===========================================================================
# _require_space
# ===========================================================================
def test_require_space_refuses_a_missing_id_pointing_at_the_listing():
    with pytest.raises(spaces.SpacesError) as exc:
        spaces._require_space(argparse.Namespace(space_id=None))
    assert "fm spaces" in str(exc.value)


def test_require_space_accepts_a_real_uuid():
    assert spaces._require_space(argparse.Namespace(space_id=SPACE)) == SPACE


# ===========================================================================
# run/_run_write: every write REFUSES before touching the network
# ===========================================================================
def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": None, "space_id": SPACE, "child_id": None, "host": "evane",
        "limit": None, "offset": None, "json": False, "name": None,
        "new_owner": None, "confirm": False,
        "region": None, "country": None, "space": None, "user": None, "role": "USER",
    }
    return argparse.Namespace(**(base | overrides))


class _RefusingApi:
    """Every method blows up: a fixture that PROVES the refusal happened
    before any network call, not merely that the test never checked."""

    def _boom(self, *a, **k):
        raise AssertionError("the API must not be called when the guard should refuse first")

    admin_get = admin_post = admin_patch = admin_delete = post = _boom


@pytest.fixture
def guarded(monkeypatch):
    """`run`/`_run_write` wired to an API that raises on ANY call, plus a
    resolved context: isolates the pure pre-network guard logic."""
    monkeypatch.setattr(spaces, "api", _RefusingApi())
    monkeypatch.setattr(
        spaces.context, "resolve",
        lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
    )
    return spaces.run


def test_delete_without_confirm_refuses_and_never_calls_the_api(guarded):
    with pytest.raises(spaces.SpacesError, match="--confirm"):
        guarded(_args(subject="delete"))


def test_rename_without_name_refuses_and_never_calls_the_api(guarded):
    with pytest.raises(spaces.SpacesError, match="--name"):
        guarded(_args(subject="rename"))


def test_rename_with_only_whitespace_name_is_treated_as_missing(guarded):
    """Shape 1: a specific wrong value: a `--name '   '` must not rename the
    space to whitespace; it is the same refusal as no name at all."""
    with pytest.raises(spaces.SpacesError, match="--name"):
        guarded(_args(subject="rename", name="   "))


def test_transfer_ownership_without_new_owner_refuses(guarded):
    with pytest.raises(spaces.SpacesError, match="--new-owner"):
        guarded(_args(subject="transfer-ownership"))


@pytest.mark.parametrize("subject", ["delete-conversation", "delete-skill", "delete-invitation"])
def test_delete_child_without_a_child_id_refuses(guarded, subject):
    with pytest.raises(spaces.SpacesError, match="third argument"):
        guarded(_args(subject=subject, child_id=None))


def test_any_write_without_a_space_id_refuses_before_the_subject_specific_guard(guarded):
    with pytest.raises(spaces.SpacesError, match="space id"):
        guarded(_args(subject="delete", space_id=None))


def test_create_without_name_refuses_and_never_calls_the_api(guarded):
    with pytest.raises(spaces.SpacesError, match="--name"):
        guarded(_args(subject="create", space_id=None, name=None, country="ae"))


def test_create_without_country_refuses_and_never_calls_the_api(guarded):
    """`--country` is required, so `fm spaces create --region ae` with no
    country must refuse rather than create a space with no country at
    all."""
    with pytest.raises(spaces.SpacesError, match="--country"):
        guarded(_args(subject="create", space_id=None, name="New WS", country=None))


def test_add_member_without_a_space_refuses(guarded):
    with pytest.raises(spaces.SpacesError, match="space id"):
        guarded(_args(subject="add-member", space_id=None, space=None, user="a@x.com"))


def test_add_member_without_user_refuses(guarded):
    with pytest.raises(spaces.SpacesError, match="--user"):
        guarded(_args(subject="add-member", user=None))


def test_add_member_with_an_unknown_role_refuses_before_resolving_the_email(guarded):
    """The role is checked BEFORE the email lookup (which would otherwise be a
    wasted walk of every platform user for a request that was always going to
    be refused)."""
    with pytest.raises(spaces.SpacesError, match="OWNER, ADMIN, USER, VIEWER"):
        guarded(_args(subject="add-member", user="a@x.com", role="superadmin"))


# ===========================================================================
# run/_run_write: a well-formed write reaches exactly one API call
# ===========================================================================
@pytest.fixture
def driven(monkeypatch):
    def _drive(method: str, payload, **overrides):
        calls: list[tuple] = []

        def _record(target, path, *rest, token, **kw):
            calls.append((path, rest[0] if rest else kw.get("params")))
            return payload

        # A plain namespace, NOT a class: an instance-dict function attribute is
        # returned as-is on lookup (no descriptor binding), so `_record` is called
        # with exactly the arguments `spaces.py` passes, with no implicit `self`.
        stub = types.SimpleNamespace(
            admin_get=_record, admin_post=_record, admin_patch=_record, admin_delete=_record,
            post=_record,
        )
        monkeypatch.setattr(spaces, "api", stub)
        monkeypatch.setattr(
            spaces.context, "resolve",
            lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
        )
        rc = spaces.run(_args(**overrides))
        return rc, calls

    return _drive


def test_delete_with_confirm_reaches_the_api_exactly_once(driven):
    rc, calls = driven(
        "admin_delete", {"deleted": SPACE, "rows": 1, "blobs": 0},
        subject="delete", confirm=True,
    )
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == f"/admin/spaces/{SPACE}"


def test_rename_sends_exactly_the_stripped_name(driven):
    rc, calls = driven("admin_patch", {"id": SPACE, "name": "Trimmed"}, subject="rename", name="  Trimmed  ")
    assert rc == 0
    assert calls[0][1] == {"name": "Trimmed"}


def test_transfer_ownership_sends_the_new_owner_id(driven):
    rc, calls = driven(
        "admin_post", {"space_id": SPACE, "old_owner_user_id": "o", "new_owner_user_id": OTHER_USER,
                       "old_owner_demoted_to": None},
        subject="transfer-ownership", new_owner=OTHER_USER,
    )
    assert rc == 0
    assert calls[0][1] == {"new_owner_user_id": OTHER_USER}


@pytest.mark.parametrize("subject", ["delete-conversation", "delete-skill", "delete-invitation"])
def test_a_well_formed_delete_child_reaches_the_right_endpoint(driven, subject):
    child_id = "22222222-2222-2222-2222-222222222222"
    rc, calls = driven(
        "admin_delete", {"deleted": child_id}, subject=subject, child_id=child_id,
    )
    assert rc == 0
    child_path = {
        "delete-conversation": "conversations",
        "delete-skill": "skills",
        "delete-invitation": "invitations",
    }[subject]
    assert calls[0][0] == f"/admin/spaces/{SPACE}/{child_path}/{child_id}"


# ===========================================================================
# run/_run_write: `create` (POST /projects, the caller's OWN token, not /admin)
# ===========================================================================
def test_create_sends_only_the_name_and_country_when_no_region_is_given(driven):
    rc, calls = driven(
        "post", {"id": SPACE, "name": "New WS", "region": "global", "role": "OWNER"},
        subject="create", space_id=None, name="New WS", country="us",
    )
    assert rc == 0
    assert calls[0][0] == "/projects"
    assert calls[0][1] == {"name": "New WS", "country": "us"}


def test_create_includes_region_when_given(driven):
    rc, calls = driven(
        "post", {"id": SPACE, "name": "New WS", "region": "ae", "role": "OWNER"},
        subject="create", space_id=None, name="New WS", country="ae", region="ae",
    )
    assert calls[0][1] == {"name": "New WS", "country": "ae", "region": "ae"}


def test_create_strips_the_name(driven):
    rc, calls = driven(
        "post", {"id": SPACE, "name": "New WS", "region": "global", "role": "OWNER"},
        subject="create", space_id=None, name="  New WS  ", country="us",
    )
    assert calls[0][1] == {"name": "New WS", "country": "us"}


def test_render_created_shows_id_name_region_and_role():
    out = spaces.render_created({"id": SPACE, "name": "New WS", "region": "ae", "role": "OWNER"})
    assert SPACE in out and "New WS" in out and "ae" in out and "OWNER" in out


# ===========================================================================
# run/_run_write: `add-member` (email -> user id via GET /admin/users, then
# the SAME POST /admin/users/{id}/spaces `fm users add-space` calls)
# ===========================================================================
def test_add_member_resolves_the_email_case_insensitively_then_assigns(monkeypatch):
    calls: list[tuple] = []

    def _admin_get(target, path, *, token, params=None):
        calls.append(("GET", path, params))
        assert path == "/admin/users"
        return {
            "items": [
                {"id": "11111111-1111-1111-1111-111111111112", "email": "someone-else@x.com"},
                {"id": OTHER_USER, "email": "Match@X.com"},
            ],
            "total": 2, "limit": 200, "offset": 0,
        }

    def _admin_post(target, path, body, *, token):
        calls.append(("POST", path, body))
        return {"user_id": OTHER_USER, "space_id": SPACE, "role": body["role"]}

    stub = types.SimpleNamespace(admin_get=_admin_get, admin_post=_admin_post)
    monkeypatch.setattr(spaces, "api", stub)
    monkeypatch.setattr(
        spaces.context, "resolve",
        lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
    )

    rc = spaces.run(_args(
        subject="add-member", space_id=None, space=SPACE, user="match@x.com", role="admin",
    ))

    assert rc == 0
    assert calls[0] == ("GET", "/admin/users", {"limit": "200", "offset": "0"})
    assert calls[1] == (
        "POST", f"/admin/users/{OTHER_USER}/spaces", {"space_id": SPACE, "role": "ADMIN"},
    )


def test_add_member_falls_back_to_the_positional_space_id_when_dash_dash_space_is_absent(monkeypatch):
    """`--space` is documented as "an alternative to the positional space-id
    argument": this pins that the fallback actually runs, not just that the
    flag works when given."""
    posted: list[dict] = []

    def _admin_get(target, path, *, token, params=None):
        return {"items": [{"id": OTHER_USER, "email": "a@x.com"}], "total": 1, "limit": 200, "offset": 0}

    def _admin_post(target, path, body, *, token):
        posted.append(body)
        return {"user_id": OTHER_USER, "space_id": body["space_id"], "role": body["role"]}

    stub = types.SimpleNamespace(admin_get=_admin_get, admin_post=_admin_post)
    monkeypatch.setattr(spaces, "api", stub)
    monkeypatch.setattr(
        spaces.context, "resolve",
        lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
    )

    # `space` is left at its default (None); `space_id` (the positional) is SPACE.
    rc = spaces.run(_args(subject="add-member", user="a@x.com"))
    assert rc == 0
    assert posted == [{"space_id": SPACE, "role": "USER"}]


def test_add_member_walks_every_page_looking_for_the_email(monkeypatch):
    """No server-side email filter exists (formist_cli/users.py's own module doc),
    so a match on page 2 must still be found."""
    calls: list[tuple] = []

    def _admin_get(target, path, *, token, params=None):
        calls.append(params)
        offset = int(params["offset"])
        if offset == 0:
            return {"items": [{"id": "x", "email": "nope@x.com"}], "total": 2, "limit": 1, "offset": 0}
        return {"items": [{"id": OTHER_USER, "email": "found@x.com"}], "total": 2, "limit": 1, "offset": 1}

    def _admin_post(target, path, body, *, token):
        return {"user_id": OTHER_USER, "space_id": SPACE, "role": body["role"]}

    stub = types.SimpleNamespace(admin_get=_admin_get, admin_post=_admin_post)
    monkeypatch.setattr(spaces, "api", stub)
    monkeypatch.setattr(spaces, "_USER_SEARCH_PAGE_SIZE", 1)
    monkeypatch.setattr(
        spaces.context, "resolve",
        lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
    )

    rc = spaces.run(_args(subject="add-member", user="found@x.com"))
    assert rc == 0
    assert len(calls) == 2


def test_add_member_refuses_when_the_email_is_not_found_and_never_calls_admin_post(monkeypatch):
    def _admin_get(target, path, *, token, params=None):
        return {"items": [], "total": 0, "limit": 200, "offset": 0}

    def _boom_post(*a, **k):
        raise AssertionError("must not assign a space when the email was never resolved")

    stub = types.SimpleNamespace(admin_get=_admin_get, admin_post=_boom_post)
    monkeypatch.setattr(spaces, "api", stub)
    monkeypatch.setattr(
        spaces.context, "resolve",
        lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
    )

    with pytest.raises(spaces.SpacesError, match="no platform user"):
        spaces.run(_args(subject="add-member", user="ghost@x.com"))


# ===========================================================================
# run: every READ subject dispatches to the right endpoint
# ===========================================================================
@pytest.fixture
def driven_read(monkeypatch):
    """Same wiring as `driven`, for the READ half of `run` (no `_MUTATING_SUBJECTS`
    guard to route around)."""

    def _drive(payload, **overrides):
        calls: list[tuple] = []

        def _record(target, path, *, token, params=None):
            calls.append((path, params))
            return payload

        stub = types.SimpleNamespace(admin_get=_record)
        monkeypatch.setattr(spaces, "api", stub)
        monkeypatch.setattr(
            spaces.context, "resolve",
            lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
        )
        rc = spaces.run(_args(**overrides))
        return rc, calls

    return _drive


def test_list_is_the_default_subject_and_needs_no_space_id(driven_read):
    rc, calls = driven_read({"items": [], "total": 0, "offset": 0}, subject=None, space_id=None)
    assert rc == 0
    assert calls == [("/admin/spaces", {})]


def test_members_reaches_the_members_endpoint(driven_read):
    rc, calls = driven_read({"items": []}, subject="members")
    assert rc == 0
    assert calls == [(f"/admin/spaces/{SPACE}/members", None)]


def test_usage_reaches_the_usage_endpoint(driven_read):
    rc, calls = driven_read({"plan": None, "status": None, "seats": 0}, subject="usage")
    assert rc == 0
    assert calls == [(f"/admin/spaces/{SPACE}/usage", None)]


@pytest.mark.parametrize("subject,noun", [
    ("conversations", "conversations"),
    ("skills", "skills"),
    ("invitations", "invitations"),
])
def test_the_three_paginated_reads_reach_their_own_endpoint(driven_read, subject, noun):
    rc, calls = driven_read({"items": []}, subject=subject, limit=10)
    assert rc == 0
    assert calls == [(f"/admin/spaces/{SPACE}/{noun}", {"limit": "10"})]
