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

from formist_cli import prefs, targets, tokens
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
        token = _refresh_if_stale(eff.target.name, token)
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


def _refresh_if_stale(env: str, token: str | None) -> str | None:
    """Trade the refresh token for a fresh access token if the stored one is
    about to expire. Returns the token to use now.

    Refreshing happens here, before any request is built, rather than
    as a retry-on-401 in the API layer, so the credential is either
    fresh or absent by the time a socket is opened.

    A failed refresh is not fatal: it leaves the old token in place
    and prints why, since raising here would break ``fm health``,
    which is public and must work when nothing else does.
    """
    if not token:
        return token
    meta = tokens.load_meta(env)
    expires_at = meta.get("expires_at")
    if not expires_at:
        return token
    try:
        deadline = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return token
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline - timedelta(seconds=_REFRESH_SKEW_SECONDS) > datetime.now(timezone.utc):
        return token

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
