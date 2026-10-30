"""`fm login` - the CLI as an OAuth client of the authorization server.

This file implements no protocol of its own: the authorization server used
here is the same one an MCP connector uses, and the CLI is just another
registered client of it. It is a textbook RFC 8252 native-app client:

  1. Discover: ``GET {api}/.well-known/oauth-authorization-server``. One
     request that also produces the only honest error message when a
     deployment has no authorization server mounted (see
     :class:`LoginError` on ``MCP_OAUTH_ENABLED``).
  2. Register: DCR (RFC 7591) as a public client,
     ``token_endpoint_auth_method = "none"``, because a client distributed
     on PyPI has no secret it could keep. Registration happens per login
     because the loopback port is ephemeral and redirect URIs must match
     exactly.
  3. Authorize: open ``{authorization_endpoint}`` in a browser. The user
     signs in with the ordinary product login.
  4. Receive the code: the browser is redirected to
     ``http://127.0.0.1:<ephemeral>/callback``; or, with ``--no-browser``,
     to a page on the API that displays the code for the user to paste.
  5. Exchange: ``POST {token_endpoint}`` with the PKCE verifier, form-encoded.

Three local defences, none of which a server can provide:

* The verifier never leaves this process. Whoever ends up holding the code
  (a local process watching the loopback port, a human reading a screen)
  cannot exchange it. This is what makes the paste mode survivable at all.
* The listener is one-shot, on 127.0.0.1, on an ephemeral port, and is
  closed before the exchange.
* ``state`` is checked in both modes: in the loopback mode it stops another
  local process answering the listener; in the paste mode it stops a code
  from somebody else's authorization from being pasted into this terminal.
  The manual page prints ``code:state`` for exactly this.

There is no ``--token`` flag. The one non-interactive credential path is the
``$EVANE_TOKEN`` environment variable, for CI and other unattended callers
that cannot complete a browser flow. Its value must be a token `fm login`
itself minted, an opaque bearer value; a value shaped like a pasted browser
session token is refused rather than silently accepted
(:func:`formist_cli.tokens.env_token`). It is read fresh from the
environment on every invocation and never written to disk, so a leaked
shell environment leaks one process's credential rather than a file that
outlives the process. It is never refreshed by `fm` itself: an expired CI
token fails loudly and must be re-issued by whoever owns it. It carries
whatever scopes it was issued with, unchanged, so a non-admin token still
gets the server's ``fm login --admin`` refusal.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import platform
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

from formist_cli import api, tokens
from formist_cli.targets import REGION_AE, Target, TargetError, resolve

#: How long the loopback listener waits before falling back to "paste the code".
CALLBACK_TIMEOUT_SECONDS = 300

#: RFC 8414 discovery. The SDK mounts it; a deployment with the authorization
#: server switched off does not, which is the distinction :func:`discover` reports.
DISCOVERY_PATH = "/.well-known/oauth-authorization-server"

_CALLBACK_PATH = "/callback"

_DONE_PAGE = """<!doctype html><meta charset="utf-8">
<title>{product} CLI connected</title>
<body style="font:16px system-ui;margin:4rem auto;max-width:32rem;text-align:center">
<h1 style="font-size:1.25rem">{product} CLI connected</h1>
<p>You can close this window and go back to your terminal.</p>
</body>"""

_FAIL_PAGE = """<!doctype html><meta charset="utf-8">
<title>{product} CLI</title>
<body style="font:16px system-ui;margin:4rem auto;max-width:32rem;text-align:center">
<h1 style="font-size:1.25rem">That did not come from your terminal</h1>
<p>The authorization did not match the request this machine started.
Close this window and run <code>fm login</code> again.</p>
</body>"""


class LoginError(Exception):
    """A refusal or failure during login. The message is written for a human."""


# --- PKCE ----------------------------------------------------------------------
def generate_pkce() -> tuple[str, str]:
    """``(verifier, challenge)`` for PKCE S256 (RFC 7636).

    96 random bytes produce 128 base64url characters, the maximum the RFC
    allows: there is no reason to pick anything smaller for a value that
    lives for one exchange inside one process.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def device_label() -> str:
    """A human-recognisable name for this machine, shown on the consent screen and
    in Settings > Connected apps, so "revoke the one from the old laptop" is
    something a person can actually do."""
    host = ""
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        pass
    system = platform.system() or "unknown"
    return f"{host} - {system}" if host else f"fm on {system}"


#: Fallback when `fm` is not running from an installed distribution, which is
#: every run out of a git checkout.
_UNPACKAGED_VERSION = "dev"


def cli_version() -> str:
    """The `fm` build that asked for a credential, recorded on the registered client
    so an old one can be spotted in Settings > Connected apps and revoked.

    Read from the package's own metadata, not from an environment variable:
    a version an operator can set is a version that can lie, and the only
    thing this field is for is telling the truth about what is installed.
    `importlib.metadata` is the one source that cannot disagree with the
    code actually running.

    Falls back to ``"dev"`` when there is no distribution to ask, which is every run
    from a git checkout. Stdlib only, so it carries no extra dependency.
    """
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("formist-cli",):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001 - a broken metadata dir must not break login
            break
    return _UNPACKAGED_VERSION


def client_version() -> str:
    return f"fm/{cli_version()}"


# --- the loopback listener -------------------------------------------------------
class _Callback:
    """A one-shot 127.0.0.1 listener that captures ``?code=&state=`` exactly once.

    Not a context manager by accident: `fm login` closes this socket before the
    token exchange, so a failure in the exchange cannot leave a port open on a
    developer's machine waiting for anything at all.
    """

    def __init__(self, product: str = "") -> None:
        #: The product the page names ("<product> CLI connected"), from the
        #: target (:func:`target_product_name`), not from whatever product
        #: this checkout runs as.
        self.product = product or target_product_name(None)
        self.code: str | None = None
        self.state: str | None = None
        self._event = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = (params.get("code") or [""])[0]
                state = (params.get("state") or [""])[0]
                ok = parsed.path == _CALLBACK_PATH and bool(code)
                body = (_DONE_PAGE if ok else _FAIL_PAGE).format(product=outer.product).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                if ok:
                    outer.code, outer.state = code, state
                    outer._event.set()

            def log_message(self, *args):  # keep the terminal clean
                return

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{_CALLBACK_PATH}"

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def close(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except OSError:
            pass


# --- where login talks to ---------------------------------------------------------
def auth_target(target: Target) -> Target:
    """The target the login itself is performed against (also used for the
    token refresh and the ``/auth/me`` lookup).

    Evane's AE region has no authorization server of its own: the user
    tables live in Global, which serves the auth surface for it. So
    `fm login --env ae` signs in against ``api.evane.ai``; the credential
    then works at ``ae.api.evane.ai`` unchanged, because the OAuth token is
    looked up across regions rather than being bound to the database that
    issued it.

    Only ``ae`` maps to Global. Every other target, including ``global``,
    ``local``, and a standalone product deployment such as ``formist``
    (its own database, its own user tables, its own authorization server
    at ``api.formist.ai``), authenticates against itself.
    """
    if target.env == REGION_AE:
        return resolve("evane")
    return target


def discover(auth: Target) -> dict:
    """The authorization server's RFC 8414 metadata, or a LoginError that says why.

    The likeliest failure here is not a network one: a deployment can have
    the authorization server switched off entirely (``MCP_OAUTH_ENABLED``),
    which serves no ``/authorize``, no ``/token`` and no ``/register`` and
    produces a 404 on discovery. A bare "404" would send an operator
    looking for a typo in a URL this file constructed correctly, so this
    says the real thing instead.
    """
    try:
        payload = api.get(auth, DISCOVERY_PATH)
    except api.ApiError as exc:
        if getattr(exc, "status", None) == 404:
            raise LoginError(
                f"{auth.api_base} is not serving an OAuth authorization server.\n"
                "`fm login` signs in through the same OAuth flow an MCP "
                "connector uses, and that server is mounted only when "
                "MCP_OAUTH_ENABLED is set. It is set in every deployment; a "
                "local dev server needs `MCP_OAUTH_ENABLED=1` in its environment.\n"
                f"(Checked {auth.api_base}{DISCOVERY_PATH}.)"
            ) from exc
        raise LoginError(f"could not read the authorization server metadata: {exc}") from exc
    if not isinstance(payload, dict):
        raise LoginError(f"unexpected discovery document: {payload!r}")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not payload.get(key):
            raise LoginError(
                f"the authorization server at {auth.api_base} advertises no "
                f"{key}, so `fm login` cannot complete. Dynamic client registration "
                "is required (RFC 7591)."
            )
    return payload


def _path_of(auth: Target, url: str) -> str:
    """An absolute endpoint URL reduced to a path on ``auth``.

    Discovery returns absolute URLs. They must be sent through
    ``formist_cli.api`` (host allow-list, error translation) rather than
    opened directly, and the refusal has to happen here if the metadata
    names another host: a discovery document is attacker-influenceable in
    exactly the way an allow-list exists for.
    """
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return url if url.startswith("/") else f"/{url}"
    base = urllib.parse.urlparse(auth.api_base)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        raise LoginError(
            f"the authorization server metadata points at {parsed.scheme}://"
            f"{parsed.netloc}, which is not {auth.api_base}. Refusing to follow it."
        )
    return parsed.path or "/"


# --- the flow ---------------------------------------------------------------------
def target_product_name(target: Target | None) -> str:
    """The product a target deployment is ("Formist" for ``--host formist``,
    "Evane" for ``global``/``ae``). A local/override target (or none) is
    whatever product this checkout runs as.

    The client name a CLI registers is shown on the deployment's own
    consent screen and Settings > Connected apps, so it must be that
    deployment's product, never the local checkout's default.
    """
    return (getattr(target, "product", None) or "fm") if target is not None else "fm"


def register_client(auth: Target, meta: dict, redirect_uris: list[str], scopes: list[str]) -> dict:
    """Register this `fm` as a public OAuth client (RFC 7591), named after the
    product of the deployment it registers with (:func:`target_product_name`)."""
    from formist_cli import privileges

    body = {
        "client_name": f"{target_product_name(auth)} CLI - {device_label()}",
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(scopes),
        "software_id": privileges.CLI_SOFTWARE_ID,
        "software_version": client_version(),
    }
    payload = api.post(auth, _path_of(auth, meta["registration_endpoint"]), body)
    if not isinstance(payload, dict) or not payload.get("client_id"):
        raise LoginError(f"client registration failed: {payload!r}")
    return payload


def authorize_url(auth: Target, meta: dict, *, client_id: str, redirect_uri: str,
                  challenge: str, state: str, scopes: list[str]) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes),
        }
    )
    return f"{auth.api_base.rstrip('/')}{_path_of(auth, meta['authorization_endpoint'])}?{query}"


def exchange(auth: Target, meta: dict, *, client_id: str, code: str,
             redirect_uri: str, verifier: str) -> dict:
    payload = api.post_form(
        auth,
        _path_of(auth, meta["token_endpoint"]),
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise LoginError(f"the token exchange returned no access token: {payload!r}")
    return payload


def refresh(auth: Target, meta: dict, *, client_id: str, refresh_token: str) -> dict:
    """Trade a refresh token for a fresh pair. The access token is short-lived
    (``MCP_OAUTH_ACCESS_TTL``, 1h) while the refresh token lasts ~30 days, so this
    is what stops `fm` asking for a browser every hour."""
    payload = api.post_form(
        auth,
        _path_of(auth, meta["token_endpoint"]),
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise LoginError(f"the refresh exchange returned no access token: {payload!r}")
    return payload


def _prompt_code(url: str, state: str) -> str:
    print("", file=sys.stderr)
    print("Open this page and sign in:", file=sys.stderr)
    print(f"  {url}", file=sys.stderr)
    print("", file=sys.stderr)
    print("The page will show a code of the form  <code>:<state>", file=sys.stderr)
    print(f"its state must be  {state}", file=sys.stderr)
    print("If it is not, that page belongs to somebody else's login. Refuse it.", file=sys.stderr)
    print("", file=sys.stderr)
    try:
        return input("Paste the code from the page: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise LoginError("cancelled") from None


def _split_pasted(pasted: str) -> tuple[str, str]:
    """``code:state`` as the manual page prints it. A bare code is accepted,
    and the state check simply cannot run; the caller says so out loud."""
    if ":" in pasted:
        code, _, state = pasted.rpartition(":")
        return code.strip(), state.strip()
    return pasted.strip(), ""


def perform_login(
    env: str | None, *, no_browser: bool = False, admin: bool = False
) -> dict:
    """Perform the whole flow. Returns ``{**token_response, "client_id": ...}``.

    Not called ``run``: every module in this package that binds a top-level
    ``run`` means "this is a subcommand's entry point", and this module is
    not one (``cli.py::cmd_login`` is).

    Storing is the caller's job (``cli.py::cmd_login``), so this can be
    exercised end to end without writing to anybody's config directory.
    """
    from formist_cli.privileges import SCOPE_ADMIN, SCOPE_CLI, SCOPE_KB

    target = env if isinstance(env, Target) else resolve(env)
    auth = auth_target(target)
    meta = discover(auth)

    scopes = [SCOPE_KB, SCOPE_CLI] + ([SCOPE_ADMIN] if admin else [])
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)

    callback = None
    if not no_browser:
        try:
            callback = _Callback()
            callback.product = target_product_name(auth)
        except OSError:
            # No loopback socket available (a locked-down box, a sandbox). Not a
            # failure: it is exactly the case --no-browser exists for.
            callback = None

    manual_uri = f"{auth.api_base.rstrip('/')}/oauth/cli/code"
    redirect_uri = callback.redirect_uri if callback else manual_uri
    # BOTH are registered even when only one is used: the loopback listener can time
    # out and fall back to the paste flow, and re-registering mid-login would mean a
    # second client row and a second authorize hop for the same human.
    redirect_uris = [manual_uri] if callback is None else [callback.redirect_uri, manual_uri]

    try:
        client = register_client(auth, meta, redirect_uris, scopes)
        url = authorize_url(
            auth, meta, client_id=str(client["client_id"]), redirect_uri=redirect_uri,
            challenge=challenge, state=state, scopes=scopes,
        )

        code = ""
        if callback is not None:
            print(f"Opening {url}", file=sys.stderr)
            opened = False
            try:
                opened = webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a headless box raises all sorts
                opened = False
            if not opened:
                print("Could not open a browser. Open the URL above yourself.", file=sys.stderr)
            if callback.wait(CALLBACK_TIMEOUT_SECONDS):
                if (callback.state or "") != state:
                    raise LoginError(
                        "the callback's state did not match this login. Something "
                        "else answered the loopback port; nothing was exchanged."
                    )
                code = callback.code or ""
            else:
                print("", file=sys.stderr)
                print(
                    "No callback received. If the page finished but nothing came "
                    "back, the browser could not reach this machine's loopback "
                    "port. Paste the code from the page instead:",
                    file=sys.stderr,
                )
                pasted, pasted_state = _split_pasted(_prompt_code(url, state))
                _check_state(pasted_state, state)
                code, redirect_uri = pasted, redirect_uri
        else:
            pasted, pasted_state = _split_pasted(_prompt_code(url, state))
            _check_state(pasted_state, state)
            code = pasted

        if not code:
            raise LoginError("no authorization code received")
    finally:
        # Closed before the exchange, unconditionally: a failure in the exchange
        # must not leave a listening socket behind.
        if callback is not None:
            callback.close()

    tokens_payload = exchange(
        auth, meta, client_id=str(client["client_id"]), code=code,
        redirect_uri=redirect_uri, verifier=verifier,
    )
    tokens_payload["client_id"] = str(client["client_id"])
    tokens_payload["client_name"] = str(client.get("client_name") or "")
    return tokens_payload


def _check_state(got: str, expected: str) -> None:
    """Refuse a pasted code whose state is not ours; say so when there is none.

    A missing state is not treated as a failure (the user may have copied
    only the first half), but it is not treated as a pass either: the check
    that would have caught a phished code did not run, and the operator is
    told, because a security control that silently did not happen is worse
    than one that is absent.
    """
    if not got:
        print(
            "  the pasted value carried no state, so `fm` could not check that "
            "this code came from the page it opened.",
            file=sys.stderr,
        )
        return
    if got != expected:
        raise LoginError(
            "that code came from a different authorization request. Nothing was "
            "exchanged. Run `fm login` again and use the page it opens."
        )


def store_payload(
    env: str, payload: dict, *, client_id: str, client_name: str | None = None
) -> str:
    """Persist a token response at 0600 and return the access token to use now.

    The one place a token response becomes a file, called by both `fm login`
    and the transparent token refresh. Two copies of "which fields go on
    disk" is how a refresh ends up dropping the ``client_id`` that the next
    refresh needs, a failure that appears 30 days later as "please log in
    again" with no explanation.

    ``expires_in`` is turned into an absolute ``expires_at`` here. A
    duration is only meaningful next to the instant it was measured from,
    and that instant is now; storing the duration would make every later
    reader re-derive it from a file mtime, which is not the same thing.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    try:
        lifetime = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    meta = {
        "client_id": client_id,
        "client_name": client_name or payload.get("client_name") or "",
        "scopes": str(payload.get("scope") or "").split(),
        "grant_id": payload.get("grant_id"),
        "obtained_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=lifetime)).isoformat() if lifetime else None,
    }
    refresh_token = payload.get("refresh_token")
    if refresh_token:
        meta["refresh_token"] = str(refresh_token)
    access = str(payload["access_token"])
    tokens.store(env, access, meta)
    return access


def token_metadata(payload: dict) -> dict:
    """The non-secret facts about a token response, for printing."""
    return {
        "client_id": payload.get("client_id"),
        "client_name": payload.get("client_name"),
        "scopes": str(payload.get("scope") or "").split(),
        "expires_in": payload.get("expires_in"),
    }


def list_apps(target: Target, token: str) -> list[dict]:
    """The caller's connected apps, as the server sees them."""
    payload = api.get(auth_target(target), "/auth/apps", token=token)
    if isinstance(payload, dict) and isinstance(payload.get("apps"), list):
        return [a for a in payload["apps"] if isinstance(a, dict)]
    return []


def revoke(target: Target, token: str, grant_id: str | None) -> bool:
    """Revoke a grant SERVER-SIDE. True if the server confirmed.

    Best-effort by design: `fm logout` must still delete the local file when the
    server is unreachable, or an offline laptop would keep a credential it believes
    it has thrown away. The caller says which happened.
    """
    if not grant_id:
        return False
    try:
        payload = api.post(
            auth_target(target), "/auth/apps/revoke", {"grant_id": grant_id}, token=token
        )
    except (api.ApiError, TargetError, OSError):
        return False
    return isinstance(payload, dict) and bool(payload.get("ok"))


__all__ = [
    "CALLBACK_TIMEOUT_SECONDS",
    "LoginError",
    "auth_target",
    "device_label",
    "discover",
    "exchange",
    "generate_pkce",
    "list_apps",
    "refresh",
    "register_client",
    "revoke",
    "perform_login",
    "store_payload",
    "token_metadata",
]
