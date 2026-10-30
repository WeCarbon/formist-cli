"""Remembered defaults for ``fm``, and the rule that stops one writing production.

A remembered target is a target someone forgets they set, which argues
for having no memory at all. This module takes a narrower position: a
remembered value is dangerous when it silently supplies the dangerous
half of a decision, and the two halves are not equally dangerous.

A space is a *what*: 36 characters nobody verifies by eye, and the half a
transcribed-id mistake lives in. Worth remembering.

An env is a *where*: one word, and the half that decides whether this is
production. Not worth remembering.

So: remember the what, require the where. A write resolves its
environment from the command line only. A read may inherit it, because a
read against the wrong environment is a wrong answer, while a write
against the wrong environment is a wrong database.

Three properties follow, and they are why this is a flag rather than a prompt:

- ``yes |`` cannot defeat it. There is nothing to answer.
- It survives scripting. A script that writes production contains the string
  ``ae`` in its source, where a reviewer sees it.
- It cannot be satisfied by habit, because the operator types the environment in
  the same breath as the thing that makes it dangerous.

The remaining hazard is a stale default, which is handled by making
staleness visible rather than by expiring it: :meth:`Effective.banner`
prints where each half came from and how old it is, on every command
including reads. Printing the target alone is close to useless, since the
operator already believes they know it. Printing why it is what it is is
what breaks a wrong belief.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from formist_cli import tokens
from formist_cli.targets import (
    DEFAULT_HOST,
    REGION_AE,
    REGION_GLOBAL,
    Target,
    TargetError,
    require_space_id,
    resolve,
)

#: Both ``dev`` and ``local`` are accepted and ``local`` is canonical: it is
#: what the host allow-list key, the token filename (``token-local.json``) and
#: every existing string already say, and "dev" is genuinely ambiguous in
#: this project in a way that would bite (a dev email provider setting, a
#: dev Attendee instance, a local env file, and "the dev environment" which
#: is a different thing from this laptop).
class PrefsError(Exception):
    """A refusal about remembered state."""


_REGION_HOSTS = {REGION_GLOBAL: "evane", REGION_AE: "evane-ae"}


def canonical_env(env: str | None) -> str | None:
    """Normalise a ``--host`` value to the name credentials are stored under.

    None stays None. Raises :class:`PrefsError` for a value that names no host.
    """
    if env is None:
        return None
    try:
        return resolve(env).name
    except TargetError as exc:
        raise PrefsError(str(exc)) from None


def prefs_path() -> Path:
    return tokens.config_dir() / "prefs.json"


@dataclass(frozen=True)
class Prefs:
    """What ``fm use`` remembered, and when."""

    space: str | None = None
    env: str | None = None
    #: The region ``/admin/spaces`` reported for :attr:`space` when it was set.
    #: Used to refuse a contradicting write BEFORE a socket is opened.
    space_region: str | None = None
    set_at: datetime | None = None

    @property
    def age(self) -> str:
        if self.set_at is None:
            return "unknown age"
        delta = _utcnow() - self.set_at
        seconds = int(delta.total_seconds())
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{seconds // 60}m ago"
        if seconds < 172800:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"


def load() -> Prefs:
    path = prefs_path()
    if not path.exists():
        return Prefs()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise PrefsError(
            f"could not read {path}: {exc}\n-> `fm use --clear` to start over."
        ) from exc
    if not isinstance(raw, dict):
        raise PrefsError(f"could not read {path}: expected a JSON object.")
    return Prefs(
        space=raw.get("space"),
        env=raw.get("host"),
        space_region=raw.get("space_region"),
        set_at=_parse_ts(raw.get("set_at")),
    )


_UNSET = object()


def store(
    *,
    space: str | object = _UNSET,
    env: str | object = _UNSET,
    space_region: str | object = _UNSET,
) -> Path:
    """Update the remembered defaults, leaving unmentioned fields alone.

    Partial by design: ``fm use --env ae`` must not silently forget the space the
    operator set five minutes ago.
    """
    current = load()

    if space is _UNSET:
        new_space = current.space
    elif space is None:
        new_space = None
    else:
        try:
            # A UUID coming out of a file is no more trustworthy than one typed on
            # a command line, so it goes through the same gate.
            new_space = require_space_id(str(space))
        except TargetError as exc:
            raise PrefsError(str(exc)) from None

    new_env = current.env if env is _UNSET else canonical_env(env)  # type: ignore[arg-type]
    new_region = current.space_region if space_region is _UNSET else space_region

    # A space that changed identity invalidates what we knew about its region.
    if space is not _UNSET and new_space != current.space and space_region is _UNSET:
        new_region = None

    payload = {
        "space": new_space,
        "host": new_env,
        "space_region": new_region,
        "set_at": _now_iso(),
    }
    directory = tokens.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)  # 0700, it sits beside the tokens
    path = prefs_path()
    path.write_text(json.dumps(payload, indent=2))
    return path


def clear() -> bool:
    """Forget everything. True if there was anything to forget."""
    path = prefs_path()
    if not path.exists():
        return False
    path.unlink()
    return True


@dataclass(frozen=True)
class Effective:
    """The target a command will actually act on, and where each half came from."""

    target: Target
    space: str | None
    env_source: str
    space_source: str
    for_write: bool

    def banner(self) -> str:
        """The provenance line printed before every command, reads included.

        Goes to stderr so it never corrupts piped JSON.
        """
        mode = "WRITE" if self.for_write else "read"
        lines = [
            f"-> {mode} {self.target.name}  {self.target.api_base}"
            f"   [host: {self.env_source}]"
        ]
        if self.space:
            lines.append(f"  space {self.space}   [space: {self.space_source}]")
        return "\n".join(lines)


def effective(
    *,
    env_flag: str | None,
    space_flag: str | None,
    for_write: bool,
) -> Effective:
    """Resolve the effective target, refusing rather than inheriting a write's env.

    ``for_write`` is the whole switch. A read inherits ``fm use``'s environment; a
    write refuses to, and says so.
    """
    prefs = load()
    env_flag = canonical_env(env_flag)

    if env_flag is not None:
        env, env_source = env_flag, "--host flag"
    elif for_write and prefs.env is not None:
        # The one refusal this module exists for.
        raise PrefsError(
            f"refusing to write using a remembered environment.\n"
            f"  `fm use` remembers --host {prefs.env} (set {prefs.age}), and a write "
            f"NEVER inherits it.\n"
            "\n"
            "A remembered environment is one somebody forgets they set, and the "
            "cost of forgetting is a write to the wrong database. Naming it costs "
            "one word and puts it in the command you just ran, and in the source "
            "of any script that does this, where a reviewer sees it.\n"
            "\n"
            f"-> Re-run with `--host {prefs.env}` if that is what you meant."
        )
    elif prefs.env is not None:
        env, env_source = prefs.env, f"fm use default, set {prefs.age}"
    else:
        env, env_source = DEFAULT_HOST, "default"

    if space_flag is not None:
        try:
            space = require_space_id(space_flag)
        except TargetError as exc:
            raise PrefsError(str(exc)) from None
        space_source = "--space flag"
    elif prefs.space:
        space, space_source = prefs.space, f"fm use default, set {prefs.age}"
    else:
        space, space_source = None, "not set"

    try:
        target = resolve(env)
    except TargetError as exc:
        raise PrefsError(str(exc)) from None

    # Two guards, and the second one is not obvious:
    #
    # A recorded region describes the REMEMBERED space only. Applying it to a space
    # passed explicitly would refuse a correct write, or, worse, permit a wrong one
    # by vouching for a space it never described.
    #
    # And `space_region` is DATA RESIDENCY (`global` / `ae`, a property of the row)
    # while `env` is a DEPLOYMENT TARGET (`local` / `ae` / `global`, a property of
    # the host). They share two of three tokens and are not the same vocabulary. A
    # local dev server reports every space it holds as `region=global`, its own
    # HOME_REGION, so comparing the two refused every local write, and the
    # refusal's remediation line said `--host evane`, which aims a dev payload at
    # PRODUCTION. A residency claim is only meaningful about a prod target.
    if (
        for_write
        and target.env in (REGION_GLOBAL, REGION_AE)
        and space_flag is None
        and prefs.space
        and prefs.space_region
        and prefs.space_region != target.env
    ):
        raise PrefsError(
            f"refusing to write: the remembered space lives in "
            f"{prefs.space_region!r}, but this command targets {env!r}.\n"
            f"  space {prefs.space} was recorded as resident in "
            f"{prefs.space_region} when `fm use` was run ({prefs.age}).\n"
            "\n"
            "Nothing was sent. A space-scoped write must reach the region that owns "
            "the row; the server would answer 421 with where to retry, and fm does "
            "not follow a redirect into a mutation: a redirect is the server "
            "saying your belief about WHERE was wrong, which is the one belief a "
            "write must not be wrong about.\n"
            "\n"
            f"-> `--host {_REGION_HOSTS.get(prefs.space_region, prefs.space_region)}`, or `fm use --space <id>` to re-record "
            "the region if the space has moved."
        )

    return Effective(
        target=target,
        space=space,
        env_source=env_source,
        space_source=space_source,
        for_write=for_write,
    )


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()
