"""``fm users``: the four mutating subjects: create / set / add-space / remove-space.

``tests/test_formist_cli_users.py`` already covers the READ subjects (list / show /
spaces / grants) and --help. This file is the write half, added for D254-style
platform-admin grants: ``fm users set --platform-admin on|off`` is the highest-
blast-radius flag on this whole surface (see the module docstring), so what
matters here is precise:

  * every write is resolved ``for_write=True``: it NEVER inherits ``--env``
    from ``fm use``, so a remembered environment cannot turn into a prod
    write by accident;
  * ``set``'s PATCH body carries ONLY the fields the operator actually named
    (``--name`` and/or ``--platform-admin``): never a field nobody asked to
    change;
  * ``--platform-admin on``/``off`` map onto the correct boolean, precisely
    the two directions a copy-paste flip would silently swap;
  * every subject needing a user id or a space UUID refuses BEFORE any
    request reaches the network: this file's ONE-ID-AT-A-TIME half of the
    owner rule "dedupe is manual" that ``tests/test_formist_cli_participants.py``
    covers for the merge surface: there is no bulk write shape here either;
  * a server refusal (403 not-platform-admin, or the documented
    cannot-self-demote / last-platform-admin errors) propagates verbatim —
    this CLI layer must never catch and reword it, since the operator-facing
    error text IS the audit trail an operator reads at the terminal. The
    server-side audit ROW (AuditedAdminRouter, `platform_audit_log`) is a
    property of the ROUTE regardless of caller and is out of reach from a
    stubbed-transport CLI test: see routes/admin.py's own test coverage for
    that half.

HTTP transport stubbed at the module level (``users.api``), the same pattern
``tests/test_formist_cli_admin_ops.py`` uses for ``formist_cli.admin_ops``.

Run: uv run pytest tests/test_formist_cli_users_writes.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import users
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")
UID = "22222222-2222-2222-2222-222222222222"
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


class RecordingApi:
    def __init__(self, *, response=None, raises: Exception | None = None):
        self.calls: list[tuple[str, str, object]] = []
        self._response = response if response is not None else {}
        self._raises = raises

    def _record(self, method, path, body):
        self.calls.append((method, path, body))
        if self._raises is not None:
            raise self._raises
        return self._response

    def admin_post(self, target, path, body, *, token=None, **kw):
        return self._record("POST", path, body)

    def admin_patch(self, target, path, body, *, token=None, **kw):
        return self._record("PATCH", path, body)

    def admin_delete(self, target, path, *, token=None, params=None, **kw):
        return self._record("DELETE", path, params)


def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": "create", "user_id": None, "env": "global", "limit": None,
        "offset": None, "json": False, "email": None, "name": None,
        "platform_admin": None, "space": None, "role": "USER",
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    captured_resolve_kwargs: dict = {}

    def _drive(api_stub, **overrides):
        def fake_resolve(args, **kw):
            captured_resolve_kwargs.update(kw)
            return Context(target=GLOBAL, space=None, token="t", space_source="flag")

        monkeypatch.setattr(users, "api", api_stub)
        monkeypatch.setattr(users.context, "resolve", fake_resolve)
        rc = users.run(_args(**overrides))
        return rc, captured_resolve_kwargs

    return _drive


# ===========================================================================
# create
# ===========================================================================
class TestCreate:
    def test_requires_email_and_sends_nothing_without_it(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="--email"):
            driven(stub, subject="create", email=None)
        assert stub.calls == []

    def test_sends_email_only_when_no_name_given(self, driven):
        stub = RecordingApi(response={"id": UID, "email": "a@b.com"})
        rc, kwargs = driven(stub, subject="create", email="a@b.com")
        assert rc == 0
        method, path, body = stub.calls[0]
        assert method == "POST"
        assert path == "/admin/users"
        assert body == {"email": "a@b.com"}

    def test_sends_name_when_given(self, driven):
        stub = RecordingApi(response={"id": UID})
        driven(stub, subject="create", email="a@b.com", name="A B")
        assert stub.calls[0][2] == {"email": "a@b.com", "name": "A B"}

    def test_resolves_for_write_never_inherits_env(self, driven):
        stub = RecordingApi(response={"id": UID})
        _, kwargs = driven(stub, subject="create", email="a@b.com")
        assert kwargs == {"for_write": True, "need_token": True}


# ===========================================================================
# set: the platform-admin grant surface
# ===========================================================================
class TestSetRequiresAUserIdAndAtLeastOneField:
    def test_missing_user_id_refuses_before_any_request(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="needs a user id"):
            driven(stub, subject="set", user_id=None, name="X")
        assert stub.calls == []

    def test_no_fields_named_refuses_before_any_request(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="nothing to set"):
            driven(stub, subject="set", user_id=UID, name=None, platform_admin=None)
        assert stub.calls == []


class TestSetBodyCarriesOnlyWhatWasNamed:
    def test_name_only(self, driven):
        stub = RecordingApi(response={"id": UID, "name": "New Name"})
        rc, _ = driven(stub, subject="set", user_id=UID, name="New Name")
        assert rc == 0
        method, path, body = stub.calls[0]
        assert method == "PATCH"
        assert path == f"/admin/users/{UID}"
        assert body == {"name": "New Name"}

    def test_platform_admin_on_maps_to_true(self, driven):
        stub = RecordingApi(response={"id": UID, "is_platform_admin": True})
        driven(stub, subject="set", user_id=UID, platform_admin="on")
        assert stub.calls[0][2] == {"is_platform_admin": True}

    def test_platform_admin_off_maps_to_false(self, driven):
        """The direction most likely to be silently swapped by a copy-paste
        edit: pinned as its own case rather than folded into the 'on' test."""
        stub = RecordingApi(response={"id": UID, "is_platform_admin": False})
        driven(stub, subject="set", user_id=UID, platform_admin="off")
        assert stub.calls[0][2] == {"is_platform_admin": False}

    def test_name_and_platform_admin_together(self, driven):
        stub = RecordingApi(response={"id": UID})
        driven(stub, subject="set", user_id=UID, name="New Name", platform_admin="on")
        assert stub.calls[0][2] == {"name": "New Name", "is_platform_admin": True}

    def test_resolves_for_write_never_inherits_env(self, driven):
        stub = RecordingApi(response={"id": UID})
        _, kwargs = driven(stub, subject="set", user_id=UID, name="X")
        assert kwargs == {"for_write": True, "need_token": True}

    def test_json_flag_prints_the_response_verbatim(self, driven, capsys):
        stub = RecordingApi(response={"id": UID, "is_platform_admin": True})
        driven(stub, subject="set", user_id=UID, platform_admin="on", **{"json": True})
        assert '"is_platform_admin": true' in capsys.readouterr().out

    def test_without_json_flag_prints_the_rendered_user(self, driven, capsys):
        stub = RecordingApi(response={"id": UID, "email": "a@b.com"})
        driven(stub, subject="set", user_id=UID, name="X")
        out = capsys.readouterr().out
        assert '"email": "a@b.com"' in out  # render_user is pretty-printed JSON


class TestServerRefusalsPropagateVerbatim:
    """cannot-self-demote / last-platform-admin / a plain 403 must reach the
    operator's terminal unchanged: this layer must not catch and reword a
    server refusal about the platform's highest-blast-radius flag."""

    def test_a_403_from_a_non_admin_caller_is_not_swallowed(self, driven):
        from formist_cli import api

        stub = RecordingApi(raises=api.ApiError("403: platform admin required"))
        with pytest.raises(api.ApiError, match="platform admin required"):
            driven(stub, subject="set", user_id=UID, platform_admin="on")

    def test_cannot_self_demote_is_not_swallowed(self, driven):
        from formist_cli import api

        stub = RecordingApi(raises=api.ApiError("cannot-self-demote"))
        with pytest.raises(api.ApiError, match="cannot-self-demote"):
            driven(stub, subject="set", user_id=UID, platform_admin="off")

    def test_last_platform_admin_is_not_swallowed(self, driven):
        from formist_cli import api

        stub = RecordingApi(raises=api.ApiError("last-platform-admin"))
        with pytest.raises(api.ApiError, match="last-platform-admin"):
            driven(stub, subject="set", user_id=UID, platform_admin="off")


# ===========================================================================
# add-space / remove-space
# ===========================================================================
class TestAddRemoveSpaceRequireAUserIdAndASpace:
    def test_add_space_missing_user_id_refuses(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="needs a user id"):
            driven(stub, subject="add-space", user_id=None, space=SPACE)
        assert stub.calls == []

    def test_add_space_missing_space_refuses_before_any_request(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="--space"):
            driven(stub, subject="add-space", user_id=UID, space=None)
        assert stub.calls == []

    def test_add_space_a_name_instead_of_a_uuid_is_refused(self, driven):
        from formist_cli.targets import TargetError

        stub = RecordingApi()
        with pytest.raises(TargetError):
            driven(stub, subject="add-space", user_id=UID, space="GFSS 2026")
        assert stub.calls == []

    def test_remove_space_missing_space_refuses(self, driven):
        stub = RecordingApi()
        with pytest.raises(users.UsersError, match="--space"):
            driven(stub, subject="remove-space", user_id=UID, space=None)
        assert stub.calls == []


class TestAddSpace:
    def test_sends_space_id_and_role_defaulting_to_USER(self, driven):
        stub = RecordingApi(response={"user_id": UID, "space_id": SPACE, "role": "USER"})
        rc, kwargs = driven(stub, subject="add-space", user_id=UID, space=SPACE)
        assert rc == 0
        method, path, body = stub.calls[0]
        assert method == "POST"
        assert path == f"/admin/users/{UID}/spaces"
        assert body == {"space_id": SPACE, "role": "USER"}
        assert kwargs == {"for_write": True, "need_token": True}

    def test_a_non_default_role_is_forwarded(self, driven):
        stub = RecordingApi(response={"user_id": UID, "space_id": SPACE, "role": "OWNER"})
        driven(stub, subject="add-space", user_id=UID, space=SPACE, role="OWNER")
        assert stub.calls[0][2] == {"space_id": SPACE, "role": "OWNER"}

    def test_renders_the_assignment_not_a_raw_dict(self, driven, capsys):
        stub = RecordingApi(response={"user_id": UID, "space_id": SPACE, "role": "OWNER"})
        driven(stub, subject="add-space", user_id=UID, space=SPACE, role="OWNER")
        out = capsys.readouterr().out
        assert f"assigned user={UID}  space={SPACE}  role=OWNER" in out


class TestRemoveSpace:
    def test_sends_a_DELETE_to_the_membership_path(self, driven):
        stub = RecordingApi(response={"user_id": UID, "space_id": SPACE})
        rc, kwargs = driven(stub, subject="remove-space", user_id=UID, space=SPACE)
        assert rc == 0
        method, path, params = stub.calls[0]
        assert method == "DELETE"
        assert path == f"/admin/users/{UID}/spaces/{SPACE}"
        assert kwargs == {"for_write": True, "need_token": True}

    def test_renders_the_removal_not_a_raw_dict(self, driven, capsys):
        stub = RecordingApi(response={"user_id": UID, "space_id": SPACE})
        driven(stub, subject="remove-space", user_id=UID, space=SPACE)
        out = capsys.readouterr().out
        assert f"removed  user={UID}  space={SPACE}" in out
        assert "role=" not in out  # removal never claims a role


# ===========================================================================
# render_membership_change: the pure renderer
# ===========================================================================
class TestRenderMembershipChange:
    def test_assigned(self):
        out = users.render_membership_change({"user_id": UID, "space_id": SPACE, "role": "ADMIN"}, removed=False)
        assert out == f"assigned user={UID}  space={SPACE}  role=ADMIN"

    def test_removed(self):
        out = users.render_membership_change({"user_id": UID, "space_id": SPACE}, removed=True)
        assert out == f"removed  user={UID}  space={SPACE}"


# ===========================================================================
# render_grants: untested by tests/test_formist_cli_users.py; same module, same
# "coverage" scope for this lane. Pure function, no transport needed.
# ===========================================================================
class TestRenderGrants:
    def test_regions_scanned_is_shown_even_when_there_are_no_grants(self):
        out = users.render_grants({"items": [], "regions_scanned": ["global", "ae"]})
        assert "regions scanned: global, ae" in out

    def test_no_regions_scanned_says_none_rather_than_a_blank(self):
        out = users.render_grants({"items": [], "regions_scanned": []})
        assert "regions scanned: (none)" in out


# ===========================================================================
# users.run(): the READ subjects' dispatch. render_list/render_user/
# render_spaces/render_grants are already pin-tested directly by
# tests/test_formist_cli_users.py; what was NOT covered anywhere is run() actually
# reaching api.admin_get with the right path for each subject, and the
# "needs a user id" refusal shared by show/grants/spaces before any request.
# ===========================================================================
class TestReadDispatch:
    def test_list_hits_admin_users_with_limit_and_offset(self, driven):
        stub = _WithAdminGet(response={"items": [], "total": 0, "limit": 50, "offset": 0})
        rc, kwargs = driven(stub, subject="list", limit=10, offset=20)
        assert rc == 0
        assert stub.calls == [("GET", "/admin/users", {"limit": "10", "offset": "20"})]

    def test_show_needs_a_user_id_before_any_request(self, driven):
        stub = _WithAdminGet()
        with pytest.raises(users.UsersError, match="needs a user id"):
            driven(stub, subject="show", user_id=None)
        assert stub.calls == []

    def test_show_hits_the_users_id_path(self, driven):
        stub = _WithAdminGet(response={"id": UID, "email": "a@b.com"})
        rc, _ = driven(stub, subject="show", user_id=UID)
        assert rc == 0
        assert stub.calls == [("GET", f"/admin/users/{UID}", None)]

    def test_grants_needs_a_user_id_before_any_request(self, driven):
        stub = _WithAdminGet()
        with pytest.raises(users.UsersError, match="needs a user id"):
            driven(stub, subject="grants", user_id=None)
        assert stub.calls == []

    def test_grants_hits_the_grants_path_and_renders_via_render_grants(self, driven, capsys):
        stub = _WithAdminGet(response={"items": [], "regions_scanned": ["ae"]})
        rc, _ = driven(stub, subject="grants", user_id=UID)
        assert rc == 0
        assert stub.calls == [("GET", f"/admin/users/{UID}/grants", None)]
        assert "regions scanned: ae" in capsys.readouterr().out

    def test_grants_json_flag_prints_raw_json(self, driven, capsys):
        stub = _WithAdminGet(response={"items": [], "regions_scanned": []})
        driven(stub, subject="grants", user_id=UID, **{"json": True})
        assert '"regions_scanned": []' in capsys.readouterr().out

    def test_spaces_needs_a_user_id_before_any_request(self, driven):
        stub = _WithAdminGet()
        with pytest.raises(users.UsersError, match="needs a user id"):
            driven(stub, subject="spaces", user_id=None)
        assert stub.calls == []

    def test_spaces_hits_the_spaces_path(self, driven, capsys):
        stub = _WithAdminGet(response={"items": []})
        rc, _ = driven(stub, subject="spaces", user_id=UID)
        assert rc == 0
        assert stub.calls == [("GET", f"/admin/users/{UID}/spaces", None)]
        assert "no space" in capsys.readouterr().out.lower()

    def test_list_reads_never_resolve_for_write(self, driven):
        stub = _WithAdminGet(response={"items": [], "total": 0, "limit": 50, "offset": 0})
        _, kwargs = driven(stub, subject="list")
        assert kwargs == {"need_token": True}


class _WithAdminGet(RecordingApi):
    """RecordingApi plus the admin_get a read dispatches through."""

    def admin_get(self, target, path, *, token=None, params=None, **kw):
        return self._record("GET", path, params)


# ===========================================================================
# add_parser: the CLI wiring (the `driven` fixture builds argparse.Namespace
# by hand and never exercises argparse's own defaults, so --role's actual
# default lives ONLY here).
# ===========================================================================
class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        top = argparse.ArgumentParser(prog="ev")
        users.add_parser(top.add_subparsers(dest="command"))
        return top

    def test_role_defaults_to_USER(self):
        args = self._parser().parse_args(["users", "add-space", "--space", SPACE])
        assert args.role == "USER"

    def test_platform_admin_only_accepts_on_or_off(self):
        args = self._parser().parse_args(["users", "set", "--platform-admin", "on"])
        assert args.platform_admin == "on"
        with pytest.raises(SystemExit):
            self._parser().parse_args(["users", "set", "--platform-admin", "maybe"])
