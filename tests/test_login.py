"""Tests for `fm login` / `fm logout` / `fm whoami`: the client half of the
OAuth flow.

The server's own guarantees are tested elsewhere. These are the ones no
server can make, because they are properties of the process on the
operator's laptop:

  * the PKCE verifier is generated here and only its challenge is sent, so a holder
    of the code alone cannot exchange it;
  * the loopback listener is one-shot, on 127.0.0.1, and CLOSED before the exchange;
  * ``state`` is checked in BOTH modes: against the callback in the loopback flow,
    against the pasted value in the manual one;
  * ``fm logout`` revokes SERVER-SIDE and says so when it could not;
  * the discovery step turns "this deployment has no authorization server" into a
    sentence naming the env var, instead of a 404 on a URL this code built right.

Plus the tool's promise: there is no ``--token`` flag and no credential env var.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

import pytest

from formist_cli import api, cli, login, privileges, tokens
from formist_cli.targets import Target


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never the operator's real ``~/.config/evane``, and never a real
    operator's $FORMIST_TOKEN: every test in this file that wants one sets
    it explicitly, so the rest see file-only behaviour regardless of what
    the runner's own shell happens to export.
    """
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "fm-config"))
    monkeypatch.delenv(tokens.ENV_TOKEN_VAR, raising=False)
    return tmp_path / "fm-config"


def _jwt(claims: dict) -> str:
    """A 3-segment dot-separated value: the shape of the old pasted session
    credential (and what ``$FORMIST_TOKEN`` must refuse)."""
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.signature"


LOCAL = Target(env="local", api_base="http://127.0.0.1:8177")
META = {
    "authorization_endpoint": "http://127.0.0.1:8177/authorize",
    "token_endpoint": "http://127.0.0.1:8177/token",
    "registration_endpoint": "http://127.0.0.1:8177/register",
}


# --- PKCE ---------------------------------------------------------------------------
def test_the_challenge_is_s256_of_the_verifier_and_the_verifier_stays_here():
    verifier, challenge = login.generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert "=" not in challenge  # unpadded base64url, per RFC 7636
    assert 43 <= len(verifier) <= 128
    assert verifier != challenge  # the wire value is not the secret


def test_two_logins_do_not_share_a_verifier():
    assert login.generate_pkce()[0] != login.generate_pkce()[0]


# --- the loopback listener ------------------------------------------------------------
def test_the_callback_listener_is_loopback_one_shot_and_closes():
    cb = login._Callback()
    try:
        assert cb.redirect_uri.startswith("http://127.0.0.1:")
        assert cb.port >= 1024
        with urllib.request.urlopen(f"{cb.redirect_uri}?code=abc&state=xyz", timeout=5) as r:
            assert r.status == 200
        assert cb.wait(2) is True
        assert (cb.code, cb.state) == ("abc", "xyz")
    finally:
        cb.close()
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(f"{cb.redirect_uri}?code=again", timeout=2)


def test_a_callback_without_a_code_does_not_satisfy_the_wait():
    cb = login._Callback()
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{cb.redirect_uri}?error=denied", timeout=5)
        assert cb.wait(0.5) is False
        assert cb.code is None
    finally:
        cb.close()


# --- state ----------------------------------------------------------------------------
def test_a_pasted_code_from_another_login_is_refused():
    with pytest.raises(login.LoginError) as exc:
        login._check_state("someone-elses-state", "mine")
    assert "different authorization request" in str(exc.value)


def test_a_pasted_value_with_no_state_is_allowed_but_SAID(capsys):
    """A security control that silently did not run is worse than one that is
    absent: so a bare code is accepted (the user may have copied half) and the
    operator is told the check could not happen."""
    login._check_state("", "mine")
    assert "could not check" in capsys.readouterr().err


def test_the_manual_page_format_round_trips():
    assert login._split_pasted("crst_ac_abc:st1") == ("crst_ac_abc", "st1")
    assert login._split_pasted("crst_ac_abc") == ("crst_ac_abc", "")


# --- discovery -------------------------------------------------------------------------
def test_a_deployment_with_no_authorization_server_says_which_env_var(monkeypatch):
    """The likeliest failure in dev, and a bare 404 would send an operator looking
    for a typo in a URL this code built correctly. MCP_OAUTH_ENABLED is set in every
    deploy path and NOT in .env.local."""
    def boom(*a, **k):
        raise api.ApiError("404 from local", status=404)

    monkeypatch.setattr(api, "get", boom)
    with pytest.raises(login.LoginError) as exc:
        login.discover(LOCAL)
    assert "MCP_OAUTH_ENABLED" in str(exc.value)


def test_discovery_metadata_pointing_elsewhere_is_refused(monkeypatch):
    """A discovery document is attacker-influenceable in exactly the way the host
    allow-list exists for; following an endpoint on another host would send the
    PKCE verifier there."""
    monkeypatch.setattr(
        api, "get", lambda *a, **k: {**META, "token_endpoint": "https://evil.example/token"}
    )
    meta = login.discover(LOCAL)
    with pytest.raises(login.LoginError) as exc:
        login._path_of(LOCAL, meta["token_endpoint"])
    assert "Refusing to follow" in str(exc.value)


def test_discovery_without_registration_is_refused(monkeypatch):
    monkeypatch.setattr(
        api, "get", lambda *a, **k: {k2: v for k2, v in META.items() if k2 != "registration_endpoint"}
    )
    with pytest.raises(login.LoginError) as exc:
        login.discover(LOCAL)
    assert "registration_endpoint" in str(exc.value)


# --- registration ----------------------------------------------------------------------
def test_the_cli_registers_as_a_public_client_with_both_redirect_uris(monkeypatch):
    sent = {}

    def fake_post(target, path, body, **kw):
        sent.update(body)
        return {"client_id": "cid-1", "client_name": body["client_name"]}

    monkeypatch.setattr(api, "post", fake_post)
    login.register_client(
        LOCAL, META, ["http://127.0.0.1:5000/callback", f"{LOCAL.api_base}/oauth/cli/code"],
        [privileges.SCOPE_KB, privileges.SCOPE_CLI],
    )
    # A client that ships on PyPI has no secret it could keep.
    assert sent["token_endpoint_auth_method"] == "none"
    assert sent["software_id"] == privileges.CLI_SOFTWARE_ID
    assert sent["scope"] == "evane:kb evane:cli"
    assert len(sent["redirect_uris"]) == 2


def test_the_admin_scope_is_only_requested_when_asked_for(monkeypatch):
    """A PROPERTY of the request, asserted without a server: the default `fm login`
    must not even ASK for platform administration."""
    seen = []

    monkeypatch.setattr(api, "get", lambda *a, **k: META)
    monkeypatch.setattr(
        api, "post", lambda t, p, b, **k: (seen.append(b), {"client_id": "cid"})[1]
    )
    monkeypatch.setattr(login, "_Callback", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(login, "_prompt_code", lambda *a: (_ for _ in ()).throw(
        login.LoginError("stop here")))

    for admin, expected in ((False, "evane:kb evane:cli"),
                            (True, "evane:kb evane:cli evane:admin")):
        seen.clear()
        with pytest.raises(login.LoginError):
            login.perform_login("127.0.0.1:8177", admin=admin)
        assert seen[0]["scope"] == expected


# --- the tool's promise -----------------------------------------------------------------
def test_there_is_no_flag_and_exactly_one_sanctioned_env_var():
    """`fm login --token ...` was the old interface and stays gone. Exactly
    one non-interactive credential path is sanctioned: $FORMIST_TOKEN
    (formist_cli/tokens.py); every other name would be the old hole wearing
    a hat."""
    import pathlib

    parser = cli.build_parser()
    flags = {
        f
        for a in parser._subparsers._group_actions[0].choices["login"]._actions  # type: ignore[attr-defined]
        for f in a.option_strings
    }
    assert "--token" not in flags, "the pasted-credential path is back"
    assert "--no-browser" in flags and "--admin" in flags

    source = (
        pathlib.Path(login.__file__).read_text() + pathlib.Path(cli.__file__).read_text()
    )
    for name in ("EVANE_ADMIN_TOKEN", "EVANE_SESSION_TOKEN", "EV_TOKEN", "EVANE_CLI_TOKEN"):
        assert name not in source, f"{name} would be a non-interactive credential path"
    assert tokens.ENV_TOKEN_VAR == "FORMIST_TOKEN", "the sanctioned var must be this one, not another"


# --- storage + logout --------------------------------------------------------------------
class _Args:
    def __init__(self, **kw):
        self.host = kw.pop("host", "127.0.0.1:8160")
        self.all = kw.pop("all", False)
        self.local_only = kw.pop("local_only", False)
        self.remote = kw.pop("remote", False)
        for k, v in kw.items():
            setattr(self, k, v)


def test_store_payload_turns_a_duration_into_an_instant_and_hides_the_refresh_token():
    login.store_payload(
        "127.0.0.1:8160",
        {"access_token": "crst_at_x", "refresh_token": "crst_rt_y",
         "scope": "evane:kb evane:cli", "expires_in": 3600},
        client_id="cid-1", client_name="Evane CLI - box",
    )
    meta = tokens.load_meta("127.0.0.1:8160")
    assert meta["expires_at"], "a duration alone is only meaningful beside its instant"
    assert meta["scopes"] == ["evane:kb", "evane:cli"]
    assert "refresh_token" not in meta, "load_meta must never return a secret"
    assert tokens.load_refresh("127.0.0.1:8160") == "crst_rt_y"
    assert tokens.load("127.0.0.1:8160") == "crst_at_x"
    on_disk = json.loads((tokens.token_path("127.0.0.1:8160")).read_text())
    assert on_disk["client_id"] == "cid-1"


def _store_a_credential():
    login.store_payload(
        "127.0.0.1:8160",
        {"access_token": "crst_at_x", "refresh_token": "crst_rt_y",
         "scope": "evane:kb evane:cli", "expires_in": 3600},
        client_id="cid-1", client_name="Evane CLI",
    )


def test_logout_revokes_server_side_then_deletes(monkeypatch, capsys):
    _store_a_credential()
    seen = {}
    monkeypatch.setattr(login, "list_apps", lambda t, tok: [
        {"client_id": "cid-1", "grant_id": "g-1", "revoked": False}
    ])
    monkeypatch.setattr(
        login, "revoke",
        lambda target, token, gid: (seen.update({"gid": gid}), True)[1],
    )
    assert cli.cmd_logout(_Args()) == 0
    assert seen == {"gid": "g-1"}
    assert tokens.load("127.0.0.1:8160") is None
    assert "revoked" in capsys.readouterr().out


def test_logout_still_deletes_the_file_when_the_server_says_nothing_and_SAYS_SO(
    monkeypatch, capsys
):
    """An offline laptop must be able to forget its credential: and must not be
    told it was revoked when it was not."""
    _store_a_credential()
    monkeypatch.setattr(login, "list_apps", lambda *a, **k: [])
    monkeypatch.setattr(login, "revoke", lambda *a, **k: False)
    rc = cli.cmd_logout(_Args())
    captured = capsys.readouterr()
    assert rc == 1, "a failed revocation must not report success"
    assert tokens.load("127.0.0.1:8160") is None
    assert "did NOT confirm" in captured.err


def test_logout_local_only_never_calls_the_server(monkeypatch):
    _store_a_credential()

    def explode(*a, **k):
        raise AssertionError("--local-only must not call the server")

    monkeypatch.setattr(login, "revoke", explode)
    monkeypatch.setattr(login, "list_apps", explode)
    assert cli.cmd_logout(_Args(local_only=True)) == 0
    assert tokens.load("127.0.0.1:8160") is None


# --- the transparent refresh ---------------------------------------------------------------
def test_a_stale_access_token_is_refreshed_before_any_request(monkeypatch):
    """The AS issues a 1-hour access token. Without this a `fm` session would
    demand a browser every hour, which is exactly the pressure that makes
    people want a static token again."""
    from datetime import datetime, timedelta, timezone

    from formist_cli import context

    login.store_payload(
        "127.0.0.1:8160",
        {"access_token": "crst_at_old", "refresh_token": "crst_rt_y",
         "scope": "evane:kb", "expires_in": 1},
        client_id="cid-1",
    )
    # expire it for real
    path = tokens.token_path("127.0.0.1:8160")
    data = json.loads(path.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))

    monkeypatch.setattr(login, "discover", lambda t: META)
    monkeypatch.setattr(
        login, "refresh",
        lambda *a, **k: {"access_token": "crst_at_new", "refresh_token": "crst_rt_z",
                         "scope": "evane:kb", "expires_in": 3600},
    )
    assert context._refresh_if_stale("127.0.0.1:8160", "crst_at_old") == "crst_at_new"
    assert tokens.load("127.0.0.1:8160") == "crst_at_new"
    assert tokens.load_refresh("127.0.0.1:8160") == "crst_rt_z"


def test_a_fresh_access_token_is_not_refreshed(monkeypatch):
    from formist_cli import context

    _store_a_credential()

    def explode(*a, **k):
        raise AssertionError("a valid token must not cost a refresh round trip")

    monkeypatch.setattr(login, "refresh", explode)
    assert context._refresh_if_stale("127.0.0.1:8160", "crst_at_x") == "crst_at_x"


def test_a_failed_refresh_leaves_the_old_token_and_explains(monkeypatch, capsys):
    """Raising here would break `fm health`, which is public and must work when
    nothing else does."""
    from datetime import datetime, timedelta, timezone

    from formist_cli import context

    _store_a_credential()
    path = tokens.token_path("127.0.0.1:8160")
    data = json.loads(path.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))

    monkeypatch.setattr(login, "discover", lambda t: META)
    monkeypatch.setattr(
        login, "refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert context._refresh_if_stale("127.0.0.1:8160", "crst_at_x") == "crst_at_x"
    assert "could not refresh" in capsys.readouterr().err


# --- the pre-flight privilege check ---------------------------------------------------------
def test_an_admin_command_is_refused_before_any_socket(monkeypatch):
    _store_a_credential()

    def explode(*a, **k):
        raise AssertionError("a socket was opened for a command that cannot succeed")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    # "users" moved to curist-server's `fmdev` (C1105/C1114) and is now a
    # LEVEL_LOCAL redirect stub; "participants" is still LEVEL_ADMIN in `fm`.
    with pytest.raises(privileges.PrivilegeError):
        privileges.check("participants", tokens.load_meta("127.0.0.1:8160").get("scopes"))


# --- $FORMIST_TOKEN --------------------------------------------------------------------
class TestWhoamiNamesTheSource:
    def test_file_backed_credential_names_the_file(self, monkeypatch, capsys):
        _store_a_credential()
        assert cli.cmd_whoami(_Args()) == 0
        out = capsys.readouterr().out
        assert "token source:" in out
        assert str(tokens.token_path("127.0.0.1:8160")) in out

    def test_env_backed_credential_names_the_environment(self, monkeypatch, capsys):
        _store_a_credential()  # a stale file must not be what gets described
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        assert cli.cmd_whoami(_Args()) == 0
        out = capsys.readouterr().out
        assert f"environment ({tokens.ENV_TOKEN_VAR})" in out
        # The stale file's client_name / scopes describe a DIFFERENT credential
        # than the one actually in use and must not be printed here.
        assert "not written to disk" in out


class TestDoctorNamesTheSource:
    def test_env_backed_credential_names_the_environment(self, monkeypatch):
        from formist_cli.doctor import EnvReport

        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        report = EnvReport.from_health("127.0.0.1:8160", {"status": "ok", "checks": []})
        annotated = cli._with_credential(report, "127.0.0.1:8160")
        assert annotated.credential == f"environment ({tokens.ENV_TOKEN_VAR})"

    def test_no_credential_at_all_still_says_so(self, monkeypatch):
        from formist_cli.doctor import EnvReport

        report = EnvReport.from_health("127.0.0.1:8160", {"status": "ok", "checks": []})
        annotated = cli._with_credential(report, "127.0.0.1:8160")
        assert annotated.credential is not None
        assert "none stored" in annotated.credential


class TestEnvTokenPrecedence:
    def test_env_token_wins_over_the_stored_file(self, monkeypatch):
        _store_a_credential()  # tokens.load("127.0.0.1:8160") would be "crst_at_x"
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        token, origin = tokens.load_for("127.0.0.1:8160")
        assert token == "crst_at_fromenv"
        assert origin == tokens.ENV_TOKEN_ORIGIN

    def test_no_env_token_falls_back_to_the_file_as_before(self):
        _store_a_credential()
        token, origin = tokens.load_for("127.0.0.1:8160")
        assert token == "crst_at_x"
        assert origin == "127.0.0.1:8160"

    def test_blank_env_token_is_ignored(self, monkeypatch):
        _store_a_credential()
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "   ")
        token, origin = tokens.load_for("127.0.0.1:8160")
        assert token == "crst_at_x"
        assert origin == "127.0.0.1:8160"

    def test_a_jwt_shaped_env_token_is_refused_with_a_message(self, monkeypatch):
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, _jwt({"sub": "u1"}))
        with pytest.raises(tokens.TokenError, match="browser session token"):
            tokens.load_for("127.0.0.1:8160")

    def test_the_refusal_never_echoes_the_value(self, monkeypatch):
        secret = _jwt({"sub": "totally-secret-marker"})
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, secret)
        with pytest.raises(tokens.TokenError) as exc:
            tokens.env_token()
        assert secret not in str(exc.value)
        assert "totally-secret-marker" not in str(exc.value)


class TestEnvTokenIsNeverStoredOrRefreshed:
    def test_env_token_is_used_as_is_and_never_refreshed(self, monkeypatch, capsys):
        """The pre-fix resolver refreshed unconditionally: see
        formist_cli/context.py::resolve. A refresh would call `store_payload`, which is
        exactly the write-to-disk this env path promises never to do."""
        from formist_cli import context, prefs
        from formist_cli.targets import resolve as resolve_target

        class _Eff:
            def __init__(self, target, space):
                self.target, self.space, self.space_source = target, space, "flag"

            def banner(self):
                return "BANNER"

        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), "sp")
        )
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")

        def _must_not_refresh(*a, **k):
            raise AssertionError("an env-sourced token must never be refreshed")

        monkeypatch.setattr(context, "_refresh_if_stale", _must_not_refresh)

        class _Args2:
            env = "127.0.0.1:8160"
            space = None

        ctx = context.resolve(_Args2())
        assert ctx.token == "crst_at_fromenv"
        assert f"environment ({tokens.ENV_TOKEN_VAR})" in capsys.readouterr().err

    def test_env_token_never_reaches_the_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "never-written"))
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        assert tokens.load_for("127.0.0.1:8160") == ("crst_at_fromenv", tokens.ENV_TOKEN_ORIGIN)
        assert not (tmp_path / "never-written").exists(), (
            "reading the env token must never create or touch the config dir"
        )


class TestLocalScopesDeferToTheServerForAnEnvToken:
    """"instance" moved to curist-server's `fmdev` (C1105/C1114) and is now
    a LEVEL_LOCAL redirect stub, so it can no longer stand in for "a LEVEL_
    ADMIN command" here; "participants" is still LEVEL_ADMIN in `fm`.
    """

    def test_a_stale_files_non_admin_scopes_do_not_block_an_env_token(self, monkeypatch):
        """`fm participants` with FORMIST_TOKEN set must not be refused locally on
        the basis of a DIFFERENT credential's recorded scopes: the local pre-check
        cannot know what the env token was granted, so it must defer to the
        server (which will 403 with the real refusal if it truly lacks admin)."""
        login.store_payload(
            "127.0.0.1:8160",
            {"access_token": "crst_at_stale", "scope": "evane:kb evane:cli",
             "expires_in": 3600},
            client_id="cid-1",
        )
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
        assert tokens.local_scopes("127.0.0.1:8160") is None
        privileges.check("participants", tokens.local_scopes("127.0.0.1:8160"))  # must not raise

    def test_without_an_env_token_the_file_scopes_are_still_used(self, monkeypatch):
        login.store_payload(
            "127.0.0.1:8160",
            {"access_token": "crst_at_x", "scope": "evane:kb evane:cli",
             "expires_in": 3600},
            client_id="cid-1",
        )
        assert tokens.local_scopes("127.0.0.1:8160") == ["evane:kb", "evane:cli"]
        with pytest.raises(privileges.PrivilegeError):
            privileges.check("participants", tokens.local_scopes("127.0.0.1:8160"))


class TestEnvTokenRedaction:
    """The value must never reach stdout/stderr: whoami, doctor, and the
    login help text are the surfaces that matter here; there is no
    --verbose flag and no header-dumping path in formist_cli/api.py to
    check (the credential goes only into the Authorization header, never a
    printed one)."""

    SECRET = "crst_at_REDACTION_MARKER_f00dcafe"

    def test_whoami(self, monkeypatch, capsys):
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, self.SECRET)
        cli.cmd_whoami(_Args())
        captured = capsys.readouterr()
        assert self.SECRET not in captured.out
        assert self.SECRET not in captured.err

    def test_doctor_credential_annotation(self, monkeypatch):
        from formist_cli.doctor import EnvReport

        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, self.SECRET)
        report = EnvReport.from_health("127.0.0.1:8160", {"status": "ok", "checks": []})
        annotated = cli._with_credential(report, "127.0.0.1:8160")
        assert self.SECRET not in str(annotated)

    def test_context_banner(self, monkeypatch, capsys):
        from formist_cli import context, prefs
        from formist_cli.targets import resolve as resolve_target

        class _Eff:
            def __init__(self, target, space):
                self.target, self.space, self.space_source = target, space, "flag"

            def banner(self):
                return "BANNER"

        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), "sp")
        )
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, self.SECRET)

        class _Args2:
            env = "127.0.0.1:8160"
            space = None

        context.resolve(_Args2())
        captured = capsys.readouterr()
        assert self.SECRET not in captured.out
        assert self.SECRET not in captured.err

    def test_login_help_text_never_echoes_a_real_value(self, monkeypatch):
        """Sanity: the --help text NAMES the variable, never a value. Guards
        against a future edit that interpolates the current environment into the
        static description string."""
        monkeypatch.setenv(tokens.ENV_TOKEN_VAR, self.SECRET)
        parser = cli.build_parser()
        help_text = parser._subparsers._group_actions[0].choices["login"].format_help()  # type: ignore[attr-defined]
        assert self.SECRET not in help_text
        assert tokens.ENV_TOKEN_VAR in help_text


# --- the admin refusal an env token without the scope gets --------------------------------
def test_the_admin_scope_refusal_names_fm_login_dash_dash_admin():
    """A credential that belongs to a platform admin but was not granted the
    admin scope must be told to pass `--admin` to `fm login`, verified here
    rather than assumed: the client's own added text does not spell out the
    flag, so what makes it true is the server's message being embedded
    verbatim as the first line."""
    from formist_cli import api
    from formist_cli.targets import resolve as resolve_target

    err = api._translate(
        _http_error(
            403,
            {
                "error": "cli-admin-scope-required",
                "message": (
                    "This credential was not granted platform-admin access. Run "
                    "`fm login --admin` and approve the platform-administration "
                    "scope on the authorization page."
                ),
            },
        ),
        resolve_target("evane"),
        "/admin/instance",
    )
    assert "fm login --admin" in str(err)


def _http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    import io

    body = json.dumps(payload).encode()
    return urllib.error.HTTPError(
        "https://x/y", code, "err", {}, io.BytesIO(body)  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Which server a login / refresh / whoami talks to: `fm login --env formist`
# must open Formist's own login page, not Evane's, or a Formist credential
# could never be issued or refreshed.
# ---------------------------------------------------------------------------
def test_only_ae_authenticates_against_global_and_formist_against_itself():
    from formist_cli.targets import resolve

    assert login.auth_target(resolve("formist")).api_base == "https://api.formist.ai"
    assert login.auth_target(resolve("evane-ae")).api_base == "https://api.evane.ai"
    assert login.auth_target(resolve("evane")).api_base == "https://api.evane.ai"
    local = resolve("127.0.0.1:8160")
    assert login.auth_target(local) == local


def test_a_formist_credential_is_refreshed_against_formist(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from formist_cli import context

    login.store_payload(
        "formist",
        {"access_token": "crst_at_old", "refresh_token": "crst_rt_y",
         "scope": "evane:kb", "expires_in": 1},
        client_id="cid-f",
    )
    path = tokens.token_path("formist")
    data = json.loads(path.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))
    seen: list[str] = []

    def _discover(t):
        seen.append(t.api_base)
        return META

    def _refresh(t, *a, **k):
        seen.append(t.api_base)
        return {"access_token": "crst_at_new", "refresh_token": "crst_rt_z",
                "scope": "evane:kb", "expires_in": 3600}

    monkeypatch.setattr(login, "discover", _discover)
    monkeypatch.setattr(login, "refresh", _refresh)
    assert context._refresh_if_stale("formist", "crst_at_old") == "crst_at_new"
    assert seen == ["https://api.formist.ai", "https://api.formist.ai"], seen


def test_connected_as_is_asked_of_the_server_that_issued_the_credential(monkeypatch):
    from formist_cli.targets import resolve

    asked: list[str] = []

    def _get(target, path, **kw):
        asked.append(target.api_base + path)
        return {"email": "user@example.com"}

    monkeypatch.setattr(api, "get", _get)
    assert cli._describe_account(resolve("formist"), {"access_token": "t"}) == "user@example.com"
    assert asked == ["https://api.formist.ai/auth/me"]


# ---------------------------------------------------------------------------
# The client name `fm` registers names the target's product, so a Formist
# login must register a "Formist CLI" app, not "Evane CLI".
# ---------------------------------------------------------------------------
def test_the_target_decides_the_product_name():
    from formist_cli.targets import resolve

    assert login.target_product_name(resolve("formist")) == "Formist"
    assert login.target_product_name(resolve("evane")) == "Evane"
    assert login.target_product_name(resolve("evane-ae")) == "Evane"


@pytest.mark.parametrize("env,product", [("formist", "Formist"), ("evane", "Evane")])
def test_the_registered_client_name_follows_the_target(monkeypatch, env, product):
    from formist_cli.targets import resolve

    sent = {}

    def fake_post(target, path, body, **kw):
        sent.update(body)
        return {"client_id": "cid-1", "client_name": body["client_name"]}

    monkeypatch.setattr(api, "post", fake_post)
    auth = login.auth_target(resolve(env))
    meta = {k: v.replace("http://127.0.0.1:8177", auth.api_base) for k, v in META.items()}
    login.register_client(auth, meta, [f"{auth.api_base}/oauth/cli/code"], [privileges.SCOPE_KB])
    assert sent["client_name"].startswith(f"{product} CLI - "), sent["client_name"]
