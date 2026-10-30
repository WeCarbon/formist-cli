"""Which env, which space, which token — resolved and announced in ONE place.

Every ``ev`` command that talks to the API answers the same three questions and
has to say where each answer came from. Before this module that operation existed
**twice**, under two different names, so neither a grep for a function name nor a
review of either file on its own would have found the pair:

  * ``cli.py::_effective`` + ``cli.py::_token_for`` — 10 call sites
  * ``logs.py::run`` — the same sequence written inline, later, by the same person

⚠️ AND THEY HAD ALREADY DRIFTED, which is the argument for this module rather than
a style preference. ``logs.py`` refused locally when no token was stored — *"no
stored session for X → fm login"* — and ``cli.py`` did not.

MEASURED rather than assumed, because the first version of this paragraph claimed
"nine other commands" and that was wrong: the ``/admin`` commands were already
refused locally by ``api.admin_get``'s own guard, so the gap was never theirs.
Instrumenting ``urllib.request.urlopen`` showed it is exactly **three** — ``ev
chats``, ``fm get`` and ``fm export`` — which reach ``api.get`` / ``api.download``
/ ``api.post``, none of which guards. ``fm chats --host evane`` with no stored
token opened a real socket to ``https://api.evane.ai/projects/…/chats``.

So the true shape is better than the one I first wrote: the guard existed at the
RESOLVER layer (logs.py) and at the LIBRARY layer (api.admin_get), and three
commands sat at neither. That is also why ``api.admin_get`` keeps its copy — see
below.

Five more commands are coming (``fm users``, ``fm files``, ``fm meetings``,
``fm cache``, ``fm connectors --keys``). At one copy each that is seven, so
``tests/test_evtool_context.py`` counts callers of ``prefs.effective`` by AST and
fails if this stops being the only one. A property, not a discipline.

WHAT IS DELIBERATELY *NOT* COLLAPSED INTO THIS
-----------------------------------------------
``api.admin_get`` raises its own "no stored session" refusal. That one stays: it is
a LIBRARY-layer guard for a caller that never came through the CLI resolver (a
script importing ``formist_cli.api`` directly), which is defence in depth rather than
duplication. GATE.md's rule for a layered guard applies — each site names the
other, so the next reader does not delete one believing it dead.
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
    #: Where the space came from (flag, remembered, unset) — already rendered into
    #: the banner, kept so a caller can say it again without re-deriving it.
    space_source: str


def resolve(
    args,
    *,
    for_write: bool = False,
    need_space: bool = False,
    need_token: bool = False,
) -> Context:
    """Resolve the target, space and token, printing provenance to STDERR.

    The banner goes to stderr on EVERY command, reads included. Printing the target
    alone is close to useless — the operator already believes they know it. Printing
    where each half came from, and how old a remembered one is, is what breaks a
    wrong belief. stderr and not stdout so a piped command stays pipeable.

    ``need_token`` refuses LOCALLY rather than letting the request go and reading a
    401 back. That costs no socket, works when the server is unreachable, and says
    the same thing sooner. It is off by default because ``fm health`` is public and
    must work before you have a token at all — a resolver that always demanded one
    would break the single command you run first.
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
            "→ `fm use --space <uuid>`, or pass `--space <uuid>` on this command.\n"
            "Find the id with `fm spaces --host evane`."
        )

    token, origin = tokens.load_for(eff.target.name)
    if origin == tokens.ENV_TOKEN_ORIGIN:
        # Used exactly as supplied: no meta file exists to read an expiry from, and
        # nothing here may write one — see evtool/tokens.py on why refreshing (or
        # storing) an env-sourced credential would defeat the point of it.
        print(
            f"  token: environment ({tokens.ENV_TOKEN_VAR}) — used as-is, not "
            "stored, not refreshed",
            file=sys.stderr,
        )
    else:
        token = _refresh_if_stale(eff.target.name, token)
        if origin and origin != eff.target.name:
            # A credential silently sourced from another region's file is exactly
            # the invisible state this tool refuses everywhere else, so the borrow
            # is said out loud rather than being a convenience nobody sees.
            print(
                f"  using the {origin} token ({eff.target.name} has none stored) — "
                f"both prod regions share one signing secret",
                file=sys.stderr,
            )
    if need_token and not token:
        raise tokens.TokenError(
            f"no stored session for {eff.target.name}. "
            f"→ `fm login --host {eff.target.name}`"
        )

    return Context(
        target=eff.target,
        space=eff.space,
        token=token,
        space_source=getattr(eff, "space_source", "unknown"),
    )


#: Refresh this many seconds BEFORE the access token actually expires. A token that
#: is valid when the request is built and expired when the server reads it produces
#: a 401 for a credential that was never wrong, and the operator is told to log in
#: again for nothing.
_REFRESH_SKEW_SECONDS = 120


def _refresh_if_stale(env: str, token: str | None) -> str | None:
    """Trade the refresh token for a fresh access token when the stored one is
    about to expire. Returns whatever `ev` should use now.

    THE ONE PLACE THIS HAPPENS, and it is here for the same reason everything else
    in this module is: the alternative is a retry-on-401 inside ``evtool/api.py``,
    which would have to be threaded through eleven call paths and would re-send a
    request nobody re-authorised. Refreshing at the RESOLVER, before any request is
    built, means the credential is either fresh or absent by the time a socket is
    opened, and the failure is one message rather than an intermittent 401.

    The Evane authorization server issues a 1-hour access token and a ~30-day
    refresh token (``mcp_app/oauth/config.py``), so without this an `ev` session
    would demand a browser every hour — which is precisely the pressure that makes
    people want a static token again.

    Failure is SILENT-but-not-hidden: a refresh that does not work leaves the old
    token in place and prints why. Raising here would break `fm health`, which is
    public and must work when nothing else does.
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
            f"with. → `fm login --host {env}`",
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
    except Exception as exc:  # noqa: BLE001 — a failed refresh must not be fatal here
        print(f"  could not refresh the {env} credential: {exc}", file=sys.stderr)
        return token

    stored = login_mod.store_payload(env, payload, client_id=str(client_id),
                                     client_name=meta.get("client_name"))
    print(f"  refreshed the {env} credential (valid again for ~1h)", file=sys.stderr)
    return stored
