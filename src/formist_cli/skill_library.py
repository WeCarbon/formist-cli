"""``fm skill-library-install``: install one skill-library package into a space.

Uses the member-facing routes (``GET /projects/{id}/skill-library/packages`` and
``POST /projects/{id}/skill-library/packages/{package_id}/install``), so it needs
a member/writer credential, not platform admin. Dry run unless ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from formist_cli import api
from formist_cli.cli_support import require_object


class SkillLibraryError(Exception):
    """A refusal about the skill-library surface, written for an operator."""


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _version_sort_key(version: object) -> tuple:
    """Best-effort numeric-aware ordering for a package's freeform ``version``
    string (a plain, non-null string defaulting to 1.0.0, never guaranteed
    semver). Each dot-separated segment sorts as an int when it parses as
    one (numeric segments always rank above textual ones, so "2" cannot
    lose to a stray "10-beta"), else as text. Ties are the caller's
    business (:func:`resolve_package` breaks them on ``created_at``).
    """
    parts: list[tuple[int, int, str]] = []
    for segment in str(version or "").split("."):
        try:
            parts.append((1, int(segment), ""))
        except ValueError:
            parts.append((0, 0, segment))
    return tuple(parts)


def fetch_packages(target, space: str, *, token: str | None, get=None) -> list[dict]:
    """Every skill-library package visible to ``space``:
    ``GET /projects/{id}/skill-library/packages``, the member-facing,
    product-filtered listing (unlike the admin, membership-free catalog
    sync uses). Not paginated: the route takes no ``limit``/``offset``
    and returns every visible row in one envelope.
    """
    get = get or api.get
    payload = get(target, f"/projects/{space}/skill-library/packages", token=token)
    envelope = require_object(payload, what="the skill-library packages list", error=SkillLibraryError)
    items = envelope.get("items")
    if not isinstance(items, list):
        raise SkillLibraryError(
            f"expected an 'items' list from the skill-library packages list, got {envelope!r}"
        )
    return [i for i in items if isinstance(i, dict)]


def fetch_space_skills(target, space: str, *, token: str | None, get=None, page_size: int = 200) -> list[dict]:
    """Every skill already in ``space``, paged to the end:
    ``GET /projects/{id}/skills`` (the generic resource-router listing), so
    :func:`already_installed` can check ``package_id`` against ALL of them, not
    only the first page.
    """
    get = get or api.get
    out: list[dict] = []
    offset = 0
    while True:
        payload = get(
            target, f"/projects/{space}/skills", token=token,
            params={"limit": str(page_size), "offset": str(offset)},
        )
        envelope = require_object(payload, what="the space's skills list", error=SkillLibraryError)
        items = [i for i in (envelope.get("items") or []) if isinstance(i, dict)]
        out.extend(items)
        total = envelope.get("total")
        offset += len(items)
        if not items or total is None or offset >= int(total):
            break
    return out


def resolve_package(packages: list[dict], package_ref: str) -> tuple[dict, list[dict]]:
    """``(chosen, all_candidates)`` for ``package_ref``: a package row UUID
    (matched exactly, one row) or the reverse-DNS ``package`` id, which
    several rows can share, one per version seeded over time. For the
    reverse-DNS form, the newest version wins (:func:`_version_sort_key`,
    ``created_at`` as the tiebreaker) and the caller is expected to print
    every candidate it passed over: this function only decides, it never
    announces.
    """
    ref = str(package_ref).strip()
    if _looks_like_uuid(ref):
        matches = [p for p in packages if str(p.get("id")) == ref]
        if not matches:
            raise SkillLibraryError(
                f"no skill-library package with id {ref!r} is visible to this space."
            )
        return matches[0], matches

    matches = [p for p in packages if p.get("package") == ref]
    if not matches:
        raise SkillLibraryError(
            f"no skill-library package with package id {ref!r} is visible to this space.\n"
            "PACKAGE is the reverse-DNS id (SkillPackage.package, e.g. "
            "'com.wecarbon.esg-report') or the package row's own UUID. "
            "`fm get SKILL_LIBRARY_PACKAGE` is not wired up yet, so check "
            "`GET /projects/{space}/skill-library/packages` (or the gallery UI) "
            "for the id."
        )
    chosen = max(
        matches,
        key=lambda p: (_version_sort_key(p.get("version")), str(p.get("created_at") or "")),
    )
    return chosen, matches


def already_installed(skills: list[dict], package_id: str) -> dict | None:
    """The space's own ``Skill`` row installed FROM ``package_id``, or ``None``.

    ``package_id`` provenance is stamped only by the real install path
    (the same server-side function both the gallery's "Install" button and
    this command's ``--apply`` call reach), so an exact match is
    unambiguous: this space already has this row, whoever or whatever
    installed it.
    """
    for row in skills:
        if row.get("package_id") == package_id:
            return row
    return None


def add_install_parser(sub) -> None:
    p = sub.add_parser(
        "skill-library-install",
        help="install one skill-library package into a space (idempotent; dry run unless --apply)",
        description=(
            "Installs PACKAGE, the reverse-DNS package id (SkillPackage.package, "
            "e.g. 'com.wecarbon.esg-report') or the package row's own UUID, into "
            "--space, through the SAME member-facing route the gallery's own "
            "'Install' button uses (POST /projects/{id}/skill-library/packages/"
            "{package_id}/install), so the installed Skill carries real "
            "package_id/package_version provenance.\n\n"
            "PACKAGE resolves against GET /projects/{id}/skill-library/packages. "
            "If several rows share the reverse-DNS id (one per version seeded "
            "over time), the NEWEST version is picked and every candidate is "
            "printed so the choice is checkable.\n\n"
            "IDEMPOTENT: if the space already has a skill installed from this "
            "exact package (GET /projects/{id}/skills, matched by package_id), "
            "this says so and does nothing: no request is sent.\n\n"
            "DRY RUN by default: prints the plan and touches nothing. --apply "
            "performs the install."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from formist_cli.cli import _add_env, _add_space

    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("package", help="reverse-DNS package id, or the package row UUID")
    p.add_argument("--apply", action="store_true", help="perform the install (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def run_install(args) -> int:
    from formist_cli.cli import _effective, _token_for

    eff = _effective(args, for_write=True, need_space=True)
    token = _token_for(eff)
    target, space = eff.target, eff.space

    packages = fetch_packages(target, space, token=token)
    chosen, matches = resolve_package(packages, args.package)
    if len(matches) > 1:
        print(
            f"  {len(matches)} package rows share package id {args.package!r}; "
            f"picked the newest version {chosen.get('version')!r} (id={chosen['id']}):",
            file=sys.stderr,
        )
        for m in matches:
            marker = "*" if m is chosen else " "
            print(f"    {marker} id={m.get('id')} version={m.get('version')}", file=sys.stderr)

    skills = fetch_space_skills(target, space, token=token)
    existing = already_installed(skills, str(chosen["id"]))
    if existing is not None:
        result = {
            "already_installed": True,
            "package": {"id": chosen.get("id"), "name": chosen.get("name"), "version": chosen.get("version")},
            "skill": {"id": existing.get("id"), "name": existing.get("name")},
        }
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(
                f"already installed: nothing to do.\n"
                f"  package: {chosen.get('name')!r} v{chosen.get('version')} (id={chosen.get('id')})\n"
                f"  skill:   id={existing.get('id')}  name={existing.get('name')!r}"
            )
        return 0

    if not args.apply:
        print(
            f"DRY RUN: nothing was changed.\n"
            f"Would install package {chosen.get('name')!r} v{chosen.get('version')} "
            f"(id={chosen.get('id')}, package={chosen.get('package')!r}) into space "
            f"{space} ({target.env}).\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    payload = api.post(
        target, f"/projects/{space}/skill-library/packages/{chosen['id']}/install", {}, token=token
    )
    envelope = require_object(payload, what="the skill-library install endpoint", error=SkillLibraryError)
    item = envelope.get("item")
    if not isinstance(item, dict) or not item.get("id"):
        raise SkillLibraryError(f"install returned an unexpected shape: {envelope!r}")

    result = {
        "already_installed": False,
        "package": {"id": chosen.get("id"), "name": chosen.get("name"), "version": chosen.get("version")},
        "skill": {"id": item.get("id"), "name": item.get("name")},
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"installed.\n"
            f"  package: {chosen.get('name')!r} v{chosen.get('version')} (id={chosen.get('id')})\n"
            f"  skill:   id={item.get('id')}  name={item.get('name')!r}"
        )
    return 0


# ===========================================================================
# fm skill-library-publish: one package into a deployment's catalog, as data
# ===========================================================================
