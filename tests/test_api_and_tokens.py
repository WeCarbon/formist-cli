"""Error translation and credential storage.

The API tests are about *messages*, not transport: on this surface 410, 404 and 401
are all routinely misread, and the whole value of the client is saying what they
actually mean. The token tests are about a credential never reaching a terminal.
"""

from __future__ import annotations

import base64
import io
import json
import stat
import urllib.error

import pytest

from formist_cli import api, tokens
from formist_cli.api import ApiError, NotAuthenticated, _translate, _translate_body, admin_get
from formist_cli.targets import resolve


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    """Every test below must see file-only behaviour unless it opts into
    $FORMIST_TOKEN explicitly — regardless of what the runner's own shell exports."""
    monkeypatch.delenv(tokens.ENV_TOKEN_VAR, raising=False)


def _http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode()
    return urllib.error.HTTPError(
        "https://x/y", code, "err", {}, io.BytesIO(body)  # type: ignore[arg-type]
    )


class TestErrorTranslation:
    def test_410_says_admin_is_global_only(self):
        """The gate runs on non-Global instances, so 410 means 'you asked ae'."""
        err = _translate(
            _http_error(410, {"error": "auth-not-served-in-region"}),
            resolve("evane-ae"),
            "/admin/events/jobs",
        )
        assert "GLOBAL region only" in str(err)
        assert "--host evane" in str(err)

    def test_410_reassures_that_ae_data_is_still_visible(self):
        """Otherwise an operator assumes Global cannot see AE spaces. It can."""
        err = _translate(
            _http_error(410, {"error": "auth-not-served-in-region"}),
            resolve("evane-ae"),
            "/admin/spaces",
        )
        assert "cross-region" in str(err)

    def test_404_never_predicts_that_a_path_will_start_working(self):
        """⚠️ The replaced test asserted the OPPOSITE, and that is why this exists.

        `ev` used to hold a map of eight admin paths "on main but not deployed" and
        answer a 404 on one of them with: *this is deploy lag, it will start working
        after the next release.* Measured 2026-08-27, all eight answered 401 in
        Global prod — every entry was wrong.

        The defect was never the data. That sentence is a PREDICTION ABOUT A
        DEPLOYMENT'S FUTURE made from a hardcoded list inside a client that has never
        spoken to that deployment, and it was confident, wrong and actionable —
        strictly worse than an unhandled error, because an operator acts on it and
        stops looking. The old test was what held it in place.
        """
        err = str(_translate(_http_error(404, {}), resolve("evane"), "/admin/usage/tokens"))
        assert "NOT DEPLOYED" not in err
        assert "deploy lag" not in err
        assert "next release" not in err

    def test_404_states_the_ambiguity_and_names_the_instrument(self):
        """Being told a tool cannot know something is only useful with a way to find
        out. `fm instance` reads the deployed build identity — it MEASURES what the
        map used to guess."""
        err = str(_translate(_http_error(404, {}), resolve("evane"), "/admin/nope"))
        assert "fm instance" in err
        assert "not served" in err

    def test_both_404s_read_the_same_because_ev_genuinely_cannot_tell_them_apart(self):
        """A wrong path and a right-but-undeployed path are indistinguishable from
        the client. Rendering them differently claimed a distinction ev cannot make,
        which is precisely how the wrong sentence became believable."""
        known = str(_translate(_http_error(404, {}), resolve("evane"), "/admin/usage/tokens"))
        unknown = str(_translate(_http_error(404, {}), resolve("evane"), "/admin/nope"))
        assert known.replace("/admin/usage/tokens", "X") == unknown.replace("/admin/nope", "X")

    def test_a_domain_404_is_told_apart_from_an_undeployed_route(self):
        """The one case the two tests above do NOT cover: a 404 that carries a
        structured `{"error": ..., "message": ...}` body — every ``ErrorResult``
        this codebase raises for "that row does not exist" (``space-not-found``,
        ``user-not-found``, ...). That is NOT the same ambiguity as a route
        FastAPI never registered (which answers ``{"detail": "Not Found"}``, no
        `error` key) — it is a normal, expected outcome of a typo'd id, and
        showing "may not be deployed yet" for it sends an operator to compare
        build revisions for something that was never a deploy gap.

        Found 2026-09-06 running `fm users add-space` against a space id that
        was real but drifted (`routes/admin.py::assign_user_to_space` 404'd with
        `space-not-found`) and getting exactly that wrong message.
        """
        err = str(_translate(
            _http_error(404, {"error": "space-not-found", "message": "Space not found"}),
            resolve("evane"),
            "/admin/users/00000000-0000-0000-0000-000000000000/spaces",
        ))
        assert "space-not-found" in err or "Space not found" in err
        assert "may not be deployed" not in err.lower()
        assert "fm instance" not in err

    def test_the_write_path_sibling_tells_a_domain_404_apart_too(self):
        """`_translate_body` (POST/PUT/PATCH/DELETE) is `_translate`'s (GET)
        sibling for this exact ambiguity — the file's own comment on this branch
        already warned "IT WAS MISSED WHEN THE GET PATH WAS FIXED" once. Same
        fix, same test, the other transport."""
        payload = {"error": "user-not-found", "message": "User not found"}
        err = str(_translate_body(
            _http_error(404, payload), payload, resolve("evane"),
            "/admin/users/00000000-0000-0000-0000-000000000000",
        ))
        assert "user-not-found" in err or "User not found" in err
        assert "may not be deployed" not in err.lower()

    def test_401_points_at_login(self):
        err = _translate(
            _http_error(401, {"error": "not-authenticated"}), resolve("evane"), "/admin/spaces"
        )
        assert isinstance(err, NotAuthenticated)
        assert "fm login --host evane" in str(err)

    def test_403_distinguishes_session_from_admin_flag(self):
        err = _translate(
            _http_error(403, {"error": "platform-admin-required"}),
            resolve("evane"),
            "/admin/spaces",
        )
        assert "is_platform_admin" in str(err)
        assert "A valid session is not enough" in str(err)


class TestAdminRoutingGuard:
    def test_admin_call_to_ae_is_refused_before_the_network(self):
        """Saves a round trip and explains the topology instead of a status code."""
        with pytest.raises(ApiError, match="GLOBAL region only"):
            admin_get(resolve("evane-ae"), "/admin/spaces", token="t")

    def test_missing_token_is_named_as_such(self):
        with pytest.raises(NotAuthenticated, match="fm login"):
            admin_get(resolve("evane"), "/admin/spaces", token=None)

    def test_admin_call_to_formist_is_not_refused_by_region(self):
        """Formist is its own single-region deployment, not a region of EVANE's
        Global/AE split — its one host serves /admin locally, so the region gate
        must let it through to the (separate) token check rather than reporting
        the GLOBAL-only 410 AE gets."""
        with pytest.raises(NotAuthenticated, match="fm login"):
            admin_get(resolve("formist"), "/admin/spaces", token=None)

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(
                lambda: admin_get(resolve("evane"), "/admin/spaces", token=None),
                id="admin_get",
            ),
            pytest.param(
                lambda: api.admin_post(
                    resolve("evane"), "/admin/spaces", {}, token=None
                ),
                id="admin_post",
            ),
            pytest.param(
                lambda: api.admin_put(
                    resolve("evane"), "/admin/spaces/x/plan", {}, token=None
                ),
                id="admin_put",
            ),
            pytest.param(
                lambda: api.admin_patch(
                    resolve("evane"), "/admin/users/x", {}, token=None
                ),
                id="admin_patch",
            ),
            pytest.param(
                lambda: api.admin_delete(
                    resolve("evane"), "/admin/spaces/x", token=None
                ),
                id="admin_delete",
            ),
            pytest.param(
                lambda: api.admin_post_multipart(
                    resolve("evane"),
                    "/admin/spaces/x/roster/institutions/sync",
                    fields={},
                    files={},
                    token=None,
                ),
                id="admin_post_multipart",
            ),
        ],
    )
    def test_every_admin_helper_reaches_the_shared_guard(self, call):
        """EVERY ``admin_*`` entry point, not the one someone happened to look at.

        RED-FIRST on the 2026-09-14 main/prod merge, on ``admin_post_multipart``
        alone: the prod line gave ``_check_admin_target`` a third parameter
        (``path``, for the region-local admin carve-out) and updated the five call
        sites IT could see; the main line had meanwhile added a SIXTH —
        ``admin_post_multipart``, the ``ev roster`` uploads, which are the only
        ``/admin`` writes that send a file rather than JSON. Neither branch was
        wrong and the two texts did not overlap, so git merged them cleanly into
        ``_check_admin_target(target, token)`` against a three-parameter function:
        every ``ev roster ... import`` would have died with ``TypeError: missing
        required positional argument: 'token'`` — a stack trace, not a refusal,
        and not on any path a unit test was walking.

        Parametrised over the whole family on purpose (shape 2). A single case
        added for ``admin_post_multipart`` would pin the bug that was found and
        say nothing about the next helper added to this module, which is exactly
        how this one got in.
        """
        with pytest.raises(NotAuthenticated, match="fm login"):
            call()


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.signature"


class TestTokenStore:
    def test_stored_file_is_0600(self, tmp_path, monkeypatch):
        """A credential must never be readable by another account."""
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        path = tokens.store("local", _jwt({"sub": "u1"}))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_directory_is_0700(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        path = tokens.store("local", _jwt({"sub": "u1"}))
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        token = _jwt({"sub": "u1"})
        tokens.store("ae", token)
        assert tokens.load("ae") == token

    def test_environments_are_isolated(self, tmp_path, monkeypatch):
        """A local token must never be reachable as a prod one."""
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", _jwt({"sub": "local-user"}))
        assert tokens.load("ae") is None

    def test_loose_permissions_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        path = tokens.store("local", _jwt({"sub": "u1"}))
        path.chmod(0o644)
        with pytest.raises(tokens.TokenError, match="readable by others"):
            tokens.load("local")

    def test_logout_removes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", _jwt({"sub": "u1"}))
        assert tokens.clear("local") is True
        assert tokens.load("local") is None
        assert tokens.clear("local") is False

    def test_logout_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", _jwt({"sub": "a"}))
        tokens.store("ae", _jwt({"sub": "b"}))
        assert sorted(tokens.clear_all()) == ["ae", "local"]


class TestDescribeMeta:
    """``describe_meta`` replaced ``describe`` (removed): an ``fm login`` access
    token is opaque (``crst_at_…``, no dots), so there is nothing in it to decode
    any more — everything reportable was already written to the metadata file
    beside it. These are the unit-level checks on the reader itself; the
    regression this replaced is covered end-to-end in
    tests/test_formist_cli_doctor.py (``fm doctor`` against a real stored credential)."""

    def test_reads_expiry_from_metadata_not_the_token(self):
        info = tokens.describe_meta({"expires_at": "2099-01-01T00:00:00+00:00"})
        assert info.expired is False
        assert "2099-01-01" in info.describe_expiry()

    def test_expired_is_flagged(self):
        info = tokens.describe_meta({"expires_at": "2000-01-01T00:00:00+00:00"})
        assert info.expired is True
        assert "EXPIRED" in info.describe_expiry()

    def test_missing_expiry_is_not_an_error(self):
        """A meta dict with no ``expires_at`` (or none at all) must describe, not
        raise — unlike the old JWT decoder, a missing claim here is a normal,
        expected shape (D301's env-sourced credential has no file at all)."""
        assert tokens.describe_meta({}).describe_expiry() == "no expiry recorded"

    def test_never_decodes_the_opaque_token_shape(self):
        """The value that broke the old JWT-decoding ``describe``: an opaque
        ``crst_at_…`` access token has no dots and is not valid JWT input. It must
        never even be looked at — ``describe_meta`` takes the metadata dict, not
        the token, so there is no token argument to mis-decode."""
        info = tokens.describe_meta(
            {"expires_at": "2099-01-01T00:00:00+00:00", "obtained_at": None}
        )
        assert info.expired is False


class TestEnvTokenVar:
    """``$FORMIST_TOKEN`` — D301 (二 = C, 2026-09-09). Unit-level checks on the
    function itself; formist_cli/tests/test_formist_cli_login.py covers the same property
    through the CLI/resolver call sites that consume it."""

    def test_unset_is_blank(self, monkeypatch):
        monkeypatch.delenv(tokens.ENV_TOKEN_VAR, raising=False)
        assert tokens.env_token() == ""

    def test_set_is_stripped(self, monkeypatch):
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "  crst_at_abc  ")
        assert tokens.env_token() == "crst_at_abc"

    def test_whitespace_only_is_blank(self, monkeypatch):
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "   ")
        assert tokens.env_token() == ""

    def test_an_opaque_ev_login_token_passes(self, monkeypatch):
        """The real shape (mcp_app/oauth/provider.py::ACCESS_PREFIX): a prefix plus
        url-safe random text, never a dot."""
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_" + "x" * 40)
        assert tokens.env_token() == "crst_at_" + "x" * 40

    def test_a_jwt_shaped_value_is_refused(self, monkeypatch):
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, _jwt({"sub": "u1"}))
        with pytest.raises(tokens.TokenError, match="browser session token"):
            tokens.env_token()

    def test_a_value_with_two_dots_but_an_empty_segment_is_not_treated_as_a_jwt(
        self, monkeypatch
    ):
        """The shape check is `3 non-empty segments`, not `2 dots` — a stray
        double-dot in an opaque value must not misfire."""
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_ab..cd")
        assert tokens.env_token() == "crst_at_ab..cd"

    def test_load_for_prefers_the_env_token_over_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", _jwt({"sub": "file-user"}))
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        token, origin = tokens.load_for("local")
        assert token == "crst_at_fromenv"
        assert origin == tokens.ENV_TOKEN_ORIGIN
        assert origin != "local", "the sentinel must never collide with a real env name"

    def test_load_for_falls_back_to_the_file_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        file_token = _jwt({"sub": "file-user"})
        tokens.store("local", file_token)
        token, origin = tokens.load_for("local")
        assert token == file_token
        assert origin == "local"

    def test_local_scopes_is_none_when_env_token_set_even_with_a_recorded_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", "crst_at_x", {"scopes": ["evane:kb", "evane:cli"]})
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        assert tokens.local_scopes("local") is None

    def test_local_scopes_reads_the_file_when_no_env_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        tokens.store("local", "crst_at_x", {"scopes": ["evane:kb", "evane:admin"]})
        assert tokens.local_scopes("local") == ["evane:kb", "evane:admin"]
