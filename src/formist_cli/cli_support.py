"""Small helpers shared by more than one ``fm`` subcommand module.

Kept deliberately tiny and dependency-free (no import of ``formist_cli.cli``, so any
subcommand module can import this without a cycle): this is where a helper goes
the moment a second subcommand module needs the identical body, so it never gets
a third independent copy.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from formist_cli.targets import Target

#: The ``/admin/*`` gate, said once. Every `fm` subcommand that reaches an
#: ``/admin`` route shows this in its ``--help`` epilog, and they must not
#: drift: two half-right descriptions of one authorisation rule is how an
#: operator concludes the wrong thing about a 403.
PLATFORM_ADMIN_GATE_NOTE = (
    "GATE: site-wide platform admin (User.is_platform_admin), re-checked fresh on "
    "every call. A project OWNER or ADMIN is NOT sufficient: membership in a space "
    "grants nothing here, so a 403 means the flag is missing from YOUR user row, not "
    "that the user or the space is missing. `fm spaces --admin --host evane` reads "
    "/admin behind the identical flag, so a working `fm spaces --admin` is the "
    "cheapest proof you have it."
)

#: Help text for every ``--admin`` flag this CLI adds. Deliberately generic:
#: naming, in ``--help`` output, exactly which commands ``--admin`` unlocks is
#: the same kind of list this project's README keeps out of its public-facing
#: half, for the same reason, so the flag's own help never grows one either.
ADMIN_FLAG_HELP = "platform administrators only"


def add_admin_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--admin`` to a subcommand, the same way ``cli._add_env`` wires
    ``--host``: ``default=argparse.SUPPRESS`` so that when this flag is not
    given on the subcommand itself, a value already set at the root parser
    (``fm --admin <command>``) is left alone instead of being overwritten
    back to ``False``.
    """
    parser.add_argument(
        "--admin", action="store_true", default=argparse.SUPPRESS, help=ADMIN_FLAG_HELP,
    )


def as_dict(payload: object, *, what: str, error: type[Exception]) -> dict:
    """``payload`` as a dict, or raise ``error``, never ``{}``.

    Anything that is not a dict is a server contract change, not an empty result.
    Returning ``{}`` would render an empty table, which reads as "no rows", a
    wrong answer where an error is the right one, and the exact
    true-about-what-it-fetched-and-false-about-what-exists shape ``fm users``'
    docstring is about.

    ``error`` is passed in rather than raising one shared exception type because
    each subcommand's own error class is what ``cli.py``'s handler catches
    to print operator prose instead of a traceback.
    """
    if not isinstance(payload, dict):
        raise error(
            f"expected a JSON object from {what}, got {type(payload).__name__}. "
            "That is a server contract change, not an empty result."
        )
    return payload


def pagination_footer(*, shown: int, total: int | None, offset: int = 0) -> str:
    """The shown-vs-total footer line every paginated ``fm`` listing ends with.

    ``fm users list`` and ``fm spaces`` (and its ``conversations`` / ``skills`` /
    ``invitations`` subjects) each need this: a page of 50 printed on its own
    reads as a platform with 50 rows, the exact shape ``fm users``' module
    docstring is about.

    The three cases:

    * the server sent no ``total`` at all: say UNKNOWN rather than a count with
      no denominator, which would be the most confident-looking way to be wrong;
    * more rows exist than were shown: name both flags that get the rest;
    * this page is the whole set (including the empty set): no alarm, because
      an alarm that fires when nothing is wrong is an alarm nobody reads.
    """
    if total is None:
        return f"{shown} shown, total UNKNOWN (the server sent no `total`)"
    if offset + shown < int(total):
        return (
            f"{shown} shown (rows {offset + 1}-{offset + shown} of {total}), "
            f"MORE EXIST. Use --offset {offset + shown} for the next page, or a "
            f"larger --limit."
        )
    return f"{shown} of {total}"


class SubcommandParser(argparse.ArgumentParser):
    """Subcommand parser that accepts options between positionals on Python < 3.12.

    ``fm settings set --host evane a=true`` puts an option between two
    positionals; argparse only handles that natively from 3.12.
    """

    _intermixed = False

    def parse_known_args(self, args=None, namespace=None):
        if sys.version_info >= (3, 12) or self._intermixed:
            return super().parse_known_args(args, namespace)
        self._intermixed = True
        try:
            return super().parse_known_intermixed_args(args, namespace)
        except TypeError:
            return super().parse_known_args(args, namespace)
        finally:
            self._intermixed = False


def build_standalone_parser(
    add_parser: Callable[[argparse._SubParsersAction], argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """The same parser one subcommand module builds, detached, so ``--help`` can
    be rendered/asserted in a test without going through the full `fm` CLI.

    Built by calling the module's own ``add_parser`` rather than repeating its
    arguments here: two definitions of one parser is two things to keep in
    step, and the copy that drifts is the one a test asserts on. Each caller
    stays a one-line, no-argument ``build_standalone_parser()``, so this is
    the shared body, not a new public entry point to remember.
    """
    holder = argparse.ArgumentParser(prog="fm")
    return add_parser(holder.add_subparsers(dest="command", parser_class=SubcommandParser))


def render_grant_rows(items: list[dict]) -> str:
    """A grants table: one renderer for both ``fm users grants`` and
    ``fm connectors grants``.

    Both commands read the same server-side view, so they get the same
    renderer rather than one each. Two copies of this would drift on exactly the
    columns that are easy to get wrong (see below), and nothing would compare them.

    The columns that must not lie, and why each is rendered the way it is:

    ``scopes``  ``-`` when the provider records none (``scopes_recorded`` false)
                versus ``(none granted)`` when it records an EMPTY set. Printing
                an empty column for both would say "this credential may do
                nothing" about an API key that may do everything.
    ``expiry``  never a bare timestamp. ``access_token`` expiries are refreshed
                silently and are always minutes away, so they are labelled as
                such; ``none_recorded`` prints ``(not recorded)``, which is not
                the same claim as "never expires".
    ``status``  ``-`` when the provider records no auth status at all. An ``ok``
                there would be an assurance nothing measured.
    """
    if not items:
        return "(no grants, this subject has connected nothing)"
    lines = [
        f"{'provider':<18} {'identity':<34} {'status':<14} {'scopes':<9} expiry",
        f"{'-' * 18} {'-' * 34} {'-' * 14} {'-' * 9} {'-' * 24}",
    ]
    for row in items:
        if row.get("scopes_recorded"):
            scopes = str(len(row.get("scopes") or [])) if row.get("scopes") else "0"
        else:
            scopes = "-"
        lines.append(
            f"{str(row.get('provider') or '-'):<18.18} "
            f"{str(row.get('identity') or row.get('account') or '-'):<34.34} "
            f"{str(row.get('auth_status') or '-'):<14} "
            f"{scopes:<9} "
            f"{_expiry_cell(row)}"
        )
    return "\n".join(lines)


def _expiry_cell(row: dict) -> str:
    """The expiry column, which is the one most able to mislead.

    ``access_token`` is a ~1h token the server refreshes on its own; printing it
    as a deadline would show every healthy connector as expiring within the hour.
    """
    kind = row.get("expiry_kind")
    if kind == "access_token":
        return f"{row.get('expires_at') or '?'} (access token, auto-renewed)"
    if kind == "credential":
        return f"{row.get('expires_at') or '?'} (credential)"
    return "(not recorded)"


def require_object(body: object, *, what: str, error: type[Exception]) -> dict:
    """The decoded body as a dict, or ``error`` explaining what came back instead.

    Shared rather than re-written per command: several modules had grown
    their own version of this, and the reason that matters is not tidiness.
    Each copy decided on its own what "malformed" meant, so the same broken
    response produced a different message depending on which subcommand you
    happened to run.

    ``what`` names the endpoint in the operator's words; ``error`` is the command's
    own exception type, so a caller still catches what it always caught.
    """
    if not isinstance(body, dict):
        raise error(
            f"expected a JSON object from {what}, got "
            f"{type(body).__name__}: {str(body)[:200]}"
        )
    return body


def default_fetch(target: Target, path: str, **kwargs: Any) -> Any:
    """The plain ``api.get`` a ``fetch=`` parameter falls back to when a caller
    does not inject one.

    Imported lazily so a module that only needs this as a *default* stays
    importable (and unit-testable with a fake fetcher) without pulling in the
    HTTP layer.
    """
    from formist_cli import api

    return api.get(target, path, **kwargs)
