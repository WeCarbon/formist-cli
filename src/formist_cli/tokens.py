"""Session-token storage for ``fm`` — on disk at 0600, never on a terminal.

Why this exists at all: during recent sessions a live prod owner session JWT was
pasted into a chat transcript twice. A token that has to be handed around by hand
ends up in scrollback, in history, and in transcripts. ``fm login`` puts it in a
file only this user can read; ``fm logout`` removes it. Nothing here ever prints a
token, and :func:`describe_meta` deliberately returns metadata rather than the
credential.

The file is per-environment, so a local token can never be sent to prod by
accident — the target picks the file, not the other way round.

The access token itself is OPAQUE (``crst_at_…`` — see
``mcp_app/oauth/provider.py::ACCESS_PREFIX``): since ``fm login`` became an OAuth
client (evtool/login.py) there is nothing in it to decode, which is exactly what
makes it revocable. Anything worth reporting about a stored credential — expiry,
scopes, who issued it — was already written to the metadata beside it at the
moment ``fm login``/refresh minted it (:func:`formist_cli.login.store_payload`); see
:func:`describe_meta`, which reads that instead of the token.

THE ONE EXCEPTION TO "ON DISK, NEVER ON A TERMINAL": ``$EVANE_TOKEN`` (see
:func:`env_token`). D301 (二 = C, owner decision 2026-09-09) sanctioned exactly one
credential environment variable, for CI / unattended machines that have no browser
to run ``fm login`` in. It is read fresh on every invocation, wins over whatever is
on disk for that call, and is never written to a file and never refreshed — a CI
token that expires gets re-issued by whoever owns it, the same as the file-based
one needs a fresh ``fm login`` after 30 days. See evtool/login.py's module
docstring for the argument the owner heard and accepted anyway.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class TokenError(Exception):
    """Something wrong with a stored credential. Never contains the credential."""


CONFIG_DIR_VAR = "FORMIST_CONFIG_DIR"


def config_dir() -> Path:
    """``$FORMIST_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/formist``, else ``~/.config/formist``.

    PUBLIC, and the only implementation. `evtool/prefs.py` had a line-for-line copy
    whose docstring read "same directory as the tokens, resolved the same way" — a
    promise that nothing enforced, in the one place where drift is not cosmetic:
    this resolves WHERE THE CREDENTIALS LIVE. Two copies means `fm logout` could
    one day clear a directory that `fm login` did not write to, and the symptom is
    a token that survives a logout.
    """
    explicit = os.environ.get(CONFIG_DIR_VAR)
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "formist"


def _file_key(name: str) -> str:
    """A host name or ``host:port`` as a safe file-name component."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def token_path(env: str) -> Path:
    """The credential file for one host (``env`` is :attr:`Target.name`)."""
    return config_dir() / f"token-{_file_key(env)}.json"


#: The ONE credential environment variable D301 (二 = C) sanctions. No CURIST_
#: fallback: unlike the EVANE_*/CURIST_* pairs elsewhere in this repo (each one a
#: rename of a var that was already deployed under the old brand), this name never
#: existed under ``CURIST_`` — there is no live secret to stay compatible with, so
#: only the primary name is read (project rules: EVANE_* is primary for anything new).
ENV_TOKEN_VAR = "FORMIST_TOKEN"

#: The origin :func:`load_for` reports when the credential came from
#: :data:`ENV_TOKEN_VAR` rather than any environment's file. Never equal to a real
#: env name (``local`` / ``global`` / ``ae``), so ``origin == env`` (the
#: "not borrowed from another region" check callers already do) stays correct
#: unchanged.
ENV_TOKEN_ORIGIN = "env"


def _looks_like_a_session_jwt(value: str) -> bool:
    """True for a 3-segment dot-separated value — the shape the OLD pasted-browser
    credential had (and ``EVANE_ADMIN_TOKEN`` still has, in scripts/watch_events.sh).
    An `fm login` token is opaque (``crst_at_…``, no dots at all — see
    ``mcp_app/oauth/provider.py::ACCESS_PREFIX``), so this shape is never a
    legitimate value here.

    Refusing it is the one guard code can actually run: ``ev`` cannot tell a stolen
    *opaque* token from a legitimate one — that is what "opaque" means — but it CAN
    tell that a JWT landed in the wrong variable, which would hand `ev` a 7-day
    whole-account credential instead of the scoped, revocable one the OAuth flow
    exists to provide.
    """
    parts = value.split(".")
    return len(parts) == 3 and all(parts)


def env_token() -> str:
    """``$EVANE_TOKEN``, stripped; ``""`` when unset or blank.

    Raises :class:`TokenError` — never silently ignores — a JWT-shaped value, so a
    CI job that exported the wrong secret gets one clear message instead of a
    hard-to-place 401 (or worse, a working-but-wrong request against someone else's
    session).
    """
    raw = (os.environ.get(ENV_TOKEN_VAR) or "").strip()
    if raw and _looks_like_a_session_jwt(raw):
        raise TokenError(
            f"{ENV_TOKEN_VAR} looks like a browser session token (three "
            "dot-separated segments), not an `fm login` credential. `fm login` "
            "tokens are opaque and never contain a dot. Mint one with `fm login` "
            "and export the 'token' field of the file it writes "
            f"({token_path('<env>')}) instead."
        )
    return raw


def local_scopes(env: str) -> list | None:
    """The scopes ``fm`` can claim to know about ``env``'s credential, for
    :func:`formist_cli.privileges.check`'s LOCAL pre-flight ONLY.

    ``None`` whenever :data:`ENV_TOKEN_VAR` is set: an env-sourced token is opaque
    and this process never learns what it was granted, so a FILE's recorded scopes
    (if one happens to exist for the same env) describe a DIFFERENT credential than
    the one actually in use and must not be consulted. ``privileges.check`` already
    treats ``None`` as "not recorded, defer to the server" — the correct answer
    here too, and the same answer an operator who never logged in locally at all
    would get.
    """
    if env_token():
        return None
    return load_meta(env).get("scopes")


def store(env: str, token: str, meta: dict | None = None) -> Path:
    """Write ``token`` for ``env`` at 0600, creating the directory at 0700.

    The mode is set BEFORE the write where the platform allows it (``os.open``
    with the mode argument), so the secret is never briefly world-readable — a
    chmod after the fact leaves a window.

    ``meta`` holds what `fm login` learned at the same moment — the ``refresh_token``
    (a secret, and the reason this whole file is 0600), the granted ``scopes``, the
    absolute ``expires_at``, and the ``client_id`` / ``grant_id`` `fm logout` needs
    in order to revoke server-side. They live in the same file rather than a second
    one because they describe THIS credential: a metadata file that could outlive
    its token, or be swapped for another environment's, is a way to revoke the wrong
    thing. :func:`load_meta` never returns either secret.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)  # 0700

    path = token_path(env)
    payload = json.dumps(
        {"env": env, "token": token, "stored_at": _now_iso(), **(meta or {})}, indent=2
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, belt and braces
    return path


def _refuse_loose_permissions(path: Path, env: str) -> None:
    """Refuse a credential file anyone else can read.

    Its own function because the file now holds TWO secrets (the access token and
    the refresh token) read by two callers, and a permission check that guarded one
    of them would be a check that cannot fail for the other.
    """
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TokenError(
            f"{path} is readable by others (mode {stat.filemode(mode)}). "
            "Refusing to use it. Run `fm logout --env "
            f"{env}` and `fm login --host {env}` to replace it."
        )


def load(env: str) -> str | None:
    """The stored token for ``env``, or None. Refuses a file with loose permissions."""
    path = token_path(env)
    if not path.exists():
        return None
    _refuse_loose_permissions(path, env)
    try:
        return json.loads(path.read_text()).get("token") or None
    except (json.JSONDecodeError, OSError) as exc:
        raise TokenError(f"could not read {path}: {exc}") from exc


def load_meta(env: str) -> dict:
    """The non-secret facts stored beside the credential. Never the credential.

    Returns ``{}`` when there is no file, when it is unreadable, or when it was
    written by a pre-``fm login``-flow version that stored only a token — a missing
    ``token_id`` must degrade to "revoke whatever credential this request carries",
    not to a crash.
    """
    path = token_path(env)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k not in ("token", "refresh_token")}


def load_refresh(env: str) -> str | None:
    """The stored refresh token, or None. Same 0600 refusal as :func:`load`.

    Separate from :func:`load_meta` on purpose: this IS a secret — it buys a fresh
    access token for as long as the grant lives — so it is never in the dict that
    gets printed, logged or handed to `fm whoami`.
    """
    path = token_path(env)
    if not path.exists():
        return None
    _refuse_loose_permissions(path, env)
    try:
        return json.loads(path.read_text()).get("refresh_token") or None
    except (json.JSONDecodeError, OSError) as exc:
        raise TokenError(f"could not read {path}: {exc}") from exc


def clear(env: str) -> bool:
    """Remove the stored token for ``env``. True if there was one."""
    path = token_path(env)
    if not path.exists():
        return False
    path.unlink()
    return True


def clear_all() -> list[str]:
    """Remove every stored token. Returns the environments that had one."""
    directory = config_dir()
    if not directory.exists():
        return []
    removed = []
    for path in sorted(directory.glob("token-*.json")):
        removed.append(path.stem.removeprefix("token-"))
        path.unlink()
    return removed


@dataclass(frozen=True)
class TokenInfo:
    """Non-secret facts about a stored token, as recorded in its metadata —
    never decoded from the token (see :func:`describe_meta`)."""

    expires_at: datetime | None
    issued_at: datetime | None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= _utcnow()

    def describe_expiry(self) -> str:
        if self.expires_at is None:
            return "no expiry recorded"
        delta = self.expires_at - _utcnow()
        if delta.total_seconds() <= 0:
            return f"EXPIRED at {self.expires_at.isoformat()}"
        hours = int(delta.total_seconds() // 3600)
        return f"valid for ~{hours}h (until {self.expires_at.isoformat()})"


def describe_meta(meta: dict) -> TokenInfo:
    """Non-secret facts about a stored credential, read from :func:`load_meta`
    — NEVER by decoding the token itself.

    An ``fm login`` access token is opaque (``crst_at_…``, no dots at all — see
    ``mcp_app/oauth/provider.py::ACCESS_PREFIX``), so there is nothing in it to
    decode; the previous version of this function tried to read it as a JWT and
    raised ``TokenError`` on every real credential once the OAuth flow replaced
    the old pasted-session-JWT login (`fm doctor` broke for every logged-in user
    as a result). Everything worth reporting was already written beside the
    token at the moment it was minted or refreshed
    (:func:`formist_cli.login.store_payload`) — ``expires_at`` and ``obtained_at`` —
    so this reads that instead of guessing at the token's shape.
    """
    return TokenInfo(
        expires_at=_parse_iso(meta.get("expires_at")),
        issued_at=_parse_iso(meta.get("obtained_at")),
    )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


#: The Evane regions are one deployment with one sign-in: a token minted on either
#: host is accepted by both. Every other host keeps its own credential.
_SHARED_SECRET_ENVS = frozenset({"evane", "evane-ae"})


def load_for(env: str) -> tuple[str | None, str | None]:
    """The token to use for ``env``, and where it came from.

    ``$EVANE_TOKEN`` (:data:`ENV_TOKEN_VAR`) is checked FIRST and, when set, wins
    over any stored file for THIS call — the origin comes back as
    :data:`ENV_TOKEN_ORIGIN`, never a real env name, so a caller comparing
    ``origin == env`` (the cross-region-borrow check) is unaffected.

    Absent that, falling back between the prod regions saves a second `fm login`
    for one credential. The origin is returned rather than hidden so the caller can
    PRINT it — a credential silently sourced from somewhere else is exactly the
    kind of invisible state this tool is otherwise built to refuse.
    """
    env_value = env_token()
    if env_value:
        return env_value, ENV_TOKEN_ORIGIN
    direct = load(env)
    if direct:
        return direct, env
    if env in _SHARED_SECRET_ENVS:
        for other in sorted(_SHARED_SECRET_ENVS - {env}):
            borrowed = load(other)
            if borrowed:
                return borrowed, other
    return None, None
