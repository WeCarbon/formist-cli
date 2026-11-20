"""Resolve which deployment, space and token a command uses, in one place.

Every command that talks to the API needs the same three answers (env,
target, token) and should report where each came from. Centralising
the resolution here keeps the logic, and the local "no token yet"
refusal, from drifting between call sites.

``api.admin_get`` keeps its own "no stored session" guard rather than
relying on this module: it also covers a caller that imports
``formist_cli.api`` directly, without going through the CLI resolver
at all.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from formist_cli import api, prefs, targets, tokens
from formist_cli.targets import Target


@dataclass(frozen=True)
class Context:
    """A resolved destination plus the credential for it."""

    target: Target
    space: str | None
    token: str | None
    #: Where the space came from (flag, remembered, unset). Already
    #: rendered into the banner; kept so a caller can say it again
    #: without re-deriving it.
    space_source: str


def resolve(
    args,
    *,
    for_write: bool = False,
    need_space: bool = False,
    need_token: bool = False,
) -> Context:
    """Resolve the target, space and token, printing provenance to stderr.

    The banner is printed on every command, including reads, since the
    target alone tells the operator little; where each value came
    from, and how stale a remembered one is, is what catches a wrong
    assumption. It goes to stderr so a piped command stays pipeable.

    ``need_token`` refuses locally instead of sending a request and
    reading back a 401: it costs no socket, works when the server is
    unreachable, and reports the same problem sooner. It defaults to
    off because ``fm health`` is public and must work before a token
    exists.
    """
    eff = prefs.effective(
        env_flag=getattr(args, "host", None),
        space_flag=getattr(args, "space", None),
        for_write=for_write,
    )
    print(eff.banner(), file=sys.stderr)

    if need_space and not eff.space:
        raise prefs.PrefsError(
            "no space given and none remembered.\n"
            "Run `fm use --space <uuid>`, or pass `--space <uuid>` on this command.\n"
            "Find the id with `fm spaces --host evane`."
        )

    token, origin = tokens.load_for(eff.target.name)
    if origin == tokens.ENV_TOKEN_ORIGIN:
        api.set_reauthenticator(None)
        # Used exactly as supplied: no metadata file exists to read an
        # expiry from, and nothing here may write one, since refreshing
        # or storing an env-sourced credential would defeat the point
        # of using one.
        print(
            f"  token: environment ({tokens.ENV_TOKEN_VAR}), used as-is, not "
            "stored, not refreshed",
            file=sys.stderr,
        )
    else:
        # Refresh against ORIGIN, not ``eff.target.name``: when borrowing
        # (origin != eff.target.name), the refresh token and its metadata
        # live in origin's file, not in a file for eff.target.name, which
        # may not even exist on disk. Refreshing against the wrong file
        # reads an empty `load_meta`, finds no `expires_at`, and silently
        # returns the stale token unchanged, which is exactly the bug a
        # borrowed, expired token used to hit (401, never refreshed).
        token_env = origin or eff.target.name
        token = _refresh_if_stale(token_env, token)
        api.set_reauthenticator(lambda _target, stale: reauthenticate(token_env, stale))
        if origin and origin != eff.target.name:
            # Borrowing a credential from another region's file is said
            # out loud rather than left as invisible state.
            print(
                f"  using the {origin} token ({eff.target.name} has none stored), "
                f"both prod regions share one signing secret",
                file=sys.stderr,
            )
    if need_token and not token:
        raise tokens.TokenError(
            f"no stored session for {eff.target.name}. "
            f"Run `fm login --host {eff.target.name}`"
        )

    return Context(
        target=eff.target,
        space=eff.space,
        token=token,
        space_source=getattr(eff, "space_source", "unknown"),
    )


#: Refresh this many seconds before the access token actually expires,
#: so a token that is valid when the request is built does not expire
#: by the time the server reads it.
_REFRESH_SKEW_SECONDS = 120


def _is_stale(meta: dict) -> bool:
    expires_at = meta.get("expires_at")
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline - timedelta(seconds=_REFRESH_SKEW_SECONDS) <= datetime.now(timezone.utc)


def _refresh_if_stale(env: str, token: str | None) -> str | None:
    """Trade the refresh token for a fresh access token if the stored one is
    about to expire. Returns the token to use now.

    Refreshing happens here, before any request is built, so the credential is
    normally fresh by the time a socket is opened; a 401 mid-run is handled by
    :func:`reauthenticate`.

    Under :func:`tokens.refresh_lock`, and the file is read again inside it:
    another process may have refreshed while this one waited, and trading the
    same refresh token a second time would fail (the server rotates it).

    A failed refresh is not fatal: it leaves the old token in place
    and prints why, since raising here would break ``fm health``,
    which is public and must work when nothing else does.
    """
    if not token or not _is_stale(tokens.load_meta(env)):
        return token
    with tokens.refresh_lock(env):
        if not _is_stale(tokens.load_meta(env)):
            return tokens.load(env) or token
        return _refresh_locked(env, token)


def reauthenticate(env: str, stale: str) -> str | None:
    """A token other than ``stale`` after the server refused it, or None.

    First the file: another process that refreshed has already written the
    replacement there. Otherwise one refresh. Called at most once per refused
    request (``api._retry_once_on_401``).
    """
    with tokens.refresh_lock(env):
        current = tokens.load(env)
        if current and current != stale:
            return current
        fresh = _refresh_locked(env, stale)
        return fresh if fresh and fresh != stale else None


def _refresh_locked(env: str, token: str) -> str:
    """The refresh exchange itself; the caller holds the lock."""
    meta = tokens.load_meta(env)
    refresh_token = tokens.load_refresh(env)
    client_id = meta.get("client_id")
    if not refresh_token or not client_id:
        print(
            f"  the {env} credential has expired and there is nothing to refresh it "
            f"with. Run `fm login --host {env}`",
            file=sys.stderr,
        )
        return token

    from formist_cli import login as login_mod

    try:
        target = login_mod.auth_target(targets.resolve(env))
        payload = login_mod.refresh(
            target, login_mod.discover(target),
            client_id=str(client_id), refresh_token=refresh_token,
        )
    except Exception as exc:  # noqa: BLE001 - a failed refresh must not be fatal here
        print(f"  could not refresh the {env} credential: {exc}", file=sys.stderr)
        return token

    stored = login_mod.store_payload(env, payload, client_id=str(client_id),
                                     client_name=meta.get("client_name"))
    print(f"  refreshed the {env} credential (valid again for ~1h)", file=sys.stderr)
    return stored
