"""``fm skills-add`` / ``fm skills-update`` - install or replace ONE project's
own skill from a local ``SKILL.md`` (+ optional attachments), never from a
repo-shipped tree.

Skills are data, stored in the database rather than shipped as files in a
repo; these two commands are the operator's per-space way to put a skill's
starting text there. They read a ``SKILL.md`` off a local directory (or a
``.zip`` of one) and write it into one named space, through the same two
routes a member's own "create skill" / "edit skill" UI action uses
underneath, parsed server-side, never by this module (an HTTP client,
stdlib only).

``skills-add`` always creates a new row; ``skills-update`` always replaces an
existing one (resolved by id or exact name, the same resolver ``fm get SKILL``
uses) and never creates a second. Deliberately no bulk "seed every space" verb.

Attachments (any file in SOURCE besides ``SKILL.md``) are registered through
the same primitives ``fm skill-attach`` already uses
(``formist_cli.uploads.upload_file`` / ``attach_file_to_skill`` /
``find_existing_attachment``) rather than a second copy of them, PLUS a
name-then-content dedupe (see ``_attach_all``) that is the whole reason
``skills-update`` can be re-run on the same SOURCE without its attachment
count growing by one every time.

Dry run by default, same contract as every other write command here
(``fm upload``, ``fm skill-attach``); ``--apply`` performs the write.

Run:
  fm skills-add    --space <id> <dir-or-zip>
  fm skills-update --space <id> --skill <id-or-name> <dir-or-zip>
"""

from __future__ import annotations

import argparse
import json as json_mod
import sys
import tempfile
import zipfile
from pathlib import Path

from formist_cli import api

#: Deliberately not named `SKILL_FILENAME`: that name already exists
#: server-side, and this module must never import server code (pure HTTP
#: client, stdlib only). Same literal value, two independent constants.
SKILL_MD_NAME = "SKILL.md"


class SkillSourceError(SystemExit):
    """A local ``source`` argument that is not a usable SKILL.md source."""


class SkillGroupError(Exception):
    """An unknown subject under the `fm skill` noun group
    (`cli.cmd_skill`): add/update/attach/install-package, the four commands
    `skills-add`/`skills-update`/`skill-attach`/`skill-library-install` used
    to be reached under before being grouped under one noun.
    """


def _read_skill_dir(path: Path) -> tuple[str, list[Path]]:
    """``(markdown text, [attachment file paths])`` from a directory holding
    ``SKILL.md``: every OTHER file directly inside it is an attachment,
    exactly the shape ``fm skill-attach`` registers one file at a time."""
    md_path = path / SKILL_MD_NAME
    if not md_path.is_file():
        raise SkillSourceError(f"error: no {SKILL_MD_NAME} under {path}")
    markdown = md_path.read_text(encoding="utf-8")
    attachments = sorted(
        p for p in path.iterdir() if p.is_file() and p.name != SKILL_MD_NAME
    )
    return markdown, attachments


def resolve_source(source: str) -> tuple[str, list[Path], tempfile.TemporaryDirectory | None]:
    """``source`` (a directory, or a ``.zip`` of one) -> ``(markdown,
    attachments, cleanup)``, where ``cleanup`` is the extracted-zip tempdir the
    caller must ``.cleanup()`` when not ``None``, or ``None`` for a plain
    directory (nothing to clean up).

    A zip may wrap its ``SKILL.md`` in one top-level directory (how most zip
    tools produce one) or not; both are accepted; anything else refuses
    rather than guessing which nested directory was meant.
    """
    path = Path(source)
    if path.is_dir():
        markdown, attachments = _read_skill_dir(path)
        return markdown, attachments, None
    if path.is_file() and path.suffix.lower() == ".zip":
        tmp = tempfile.TemporaryDirectory(prefix="fm-skills-")
        try:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmp.name)
        except Exception:
            tmp.cleanup()
            raise
        extracted = Path(tmp.name)
        for candidate in (extracted, *sorted(p for p in extracted.iterdir() if p.is_dir())):
            if (candidate / SKILL_MD_NAME).is_file():
                markdown, attachments = _read_skill_dir(candidate)
                return markdown, attachments, tmp
        tmp.cleanup()
        raise SkillSourceError(f"error: no {SKILL_MD_NAME} found anywhere in {path}")
    raise SkillSourceError(f"error: {source!r} is neither a directory nor a .zip file")


def _attach_all(
    target,
    space: str,
    item: dict,
    attachments: list[Path],
    *,
    token: str | None,
    upload_file=None,
    attach_file_to_skill=None,
    find_existing_attachment=None,
    download_file_bytes=None,
    replace_file_in_skill=None,
) -> dict:
    """Upload + register every attachment on the just-created/updated skill,
    reusing ``fm skill-attach``'s own functions rather than a second copy.

    DEDUPES BY NAME, the same match rule ``fm skill-attach`` uses
    (:func:`formist_cli.uploads.find_existing_attachment`): a same-named
    attachment already on ``item`` is never re-attached under a second
    entry. A no-op for ``fm skills-add`` (a brand-new skill starts with no
    attachments, so nothing here ever matches); it is what stops ``fm
    skills-update`` re-attaching its template file on every run.

    A name match is then settled by CONTENT, not just the name: identical
    bytes (sha256, after an equal-size fast path) are left alone; changed
    bytes are uploaded and swapped into the SAME slot
    (:func:`formist_cli.uploads.replace_file_in_skill`), never appended
    as a second attachment under the name a human already recognises.

    Every injectable here defaults to the real ``formist_cli.uploads``
    function, the same dependency-injection shape those functions
    themselves already accept, so a test can pass a fake transport all the
    way through without patching module globals.
    """
    from formist_cli import uploads as uploads_mod

    upload_file = upload_file or uploads_mod.upload_file
    attach_file_to_skill = attach_file_to_skill or uploads_mod.attach_file_to_skill
    find_existing_attachment = find_existing_attachment or uploads_mod.find_existing_attachment
    download_file_bytes = download_file_bytes or uploads_mod.download_file_bytes
    replace_file_in_skill = replace_file_in_skill or uploads_mod.replace_file_in_skill

    for att in attachments:
        existing = find_existing_attachment(target, space, item, att.name, token=token)
        if existing is not None:
            local_bytes = att.read_bytes()
            if existing.get("size") == len(local_bytes):
                remote_bytes = download_file_bytes(target, space, existing["id"], token=token)
                if uploads_mod.sha256_hex(remote_bytes) == uploads_mod.sha256_hex(local_bytes):
                    continue  # identical content already attached: nothing to do
            uploaded = upload_file(target, space, att, token=token)
            file_id = uploaded["item"]["id"]
            item = replace_file_in_skill(target, space, item, existing["id"], file_id, token=token)
            continue
        uploaded = upload_file(target, space, att, token=token)
        file_id = uploaded["item"]["id"]
        item = attach_file_to_skill(target, space, item, file_id, token=token)
    return item


def add_skill_from_source(
    target,
    space: str,
    source: str,
    *,
    token: str | None,
    post=None,
    upload_file=None,
    attach_file_to_skill=None,
    find_existing_attachment=None,
    download_file_bytes=None,
    replace_file_in_skill=None,
) -> dict:
    """The testable core of ``fm skills-add``: read SOURCE, POST it, attach
    whatever else SOURCE held. ``post`` is injectable (default:
    ``formist_cli.api.post``), the fake-transport convention every other
    write command in this test suite uses."""
    post = post or api.post
    markdown, attachments, tmp = resolve_source(source)
    try:
        item = post(target, f"/projects/{space}/skills/import", {"markdown": markdown}, token=token)
        if not isinstance(item, dict) or not item.get("id"):
            raise SystemExit(f"error: import returned an unexpected shape: {item!r}")
        return _attach_all(
            target,
            space,
            item,
            attachments,
            token=token,
            upload_file=upload_file,
            attach_file_to_skill=attach_file_to_skill,
            find_existing_attachment=find_existing_attachment,
            download_file_bytes=download_file_bytes,
            replace_file_in_skill=replace_file_in_skill,
        )
    finally:
        if tmp is not None:
            tmp.cleanup()


def update_skill_from_source(
    target,
    space: str,
    skill: dict,
    source: str,
    *,
    token: str | None,
    patch=None,
    upload_file=None,
    attach_file_to_skill=None,
    find_existing_attachment=None,
    download_file_bytes=None,
    replace_file_in_skill=None,
) -> dict:
    """The testable core of ``fm skills-update``: read SOURCE, PATCH the
    already-resolved ``skill`` dict (see ``formist_cli.uploads.resolve_skill``),
    attach whatever else SOURCE held. Never resolves ``skill`` itself: same
    split ``formist_cli.uploads.attach_file_to_skill`` uses, so a caller controls
    exactly when the resolve-by-name network round trip happens.

    Attachments are DEDUPED BY NAME then settled by content (see
    :func:`_attach_all`): a re-run with an unchanged template file leaves
    ``skill["files"]`` exactly as it was, rather than growing by one every
    time.
    """
    patch = patch or api.patch
    markdown, attachments, tmp = resolve_source(source)
    try:
        item = patch(
            target,
            f"/projects/{space}/skills/{skill['id']}/import",
            {"markdown": markdown},
            token=token,
        )
        if not isinstance(item, dict) or not item.get("id"):
            raise SystemExit(f"error: import returned an unexpected shape: {item!r}")
        return _attach_all(
            target,
            space,
            item,
            attachments,
            token=token,
            find_existing_attachment=find_existing_attachment,
            download_file_bytes=download_file_bytes,
            replace_file_in_skill=replace_file_in_skill,
            upload_file=upload_file,
            attach_file_to_skill=attach_file_to_skill,
        )
    finally:
        if tmp is not None:
            tmp.cleanup()


# ===========================================================================
# The CLI surface. Per-module `add_parser` is the convention, see chats.py.
# ===========================================================================
def add_parser(sub) -> None:
    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "skills-add",
        help="install a local SKILL.md (+ attachments) as a NEW skill in a space (WRITE)",
        description=(
            "Reads SOURCE/SKILL.md and creates a brand-new Skill in the target "
            "space (POST .../skills/import; the server parses SKILL.md). "
            "Every OTHER file directly under SOURCE is uploaded and registered as "
            "a template attachment (the same mechanism `fm skill-attach` uses).\n\n"
            "SOURCE is a local directory holding SKILL.md, or a .zip of one.\n\n"
            "DRY RUN by default: prints what would be created and touches "
            "nothing. --apply performs the write."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("source", help="a directory holding SKILL.md, or a .zip of one")
    p.add_argument("--apply", action="store_true", help="perform the write (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser(
        "skills-update",
        help="replace an EXISTING skill's content from a local SKILL.md (+ attachments) (WRITE)",
        description=(
            "Reads SOURCE/SKILL.md and REPLACES --skill's content/description/etc "
            "in place (PATCH .../skills/{id}/import), never creates a second "
            "row. --skill is a skill id (UUID) or its exact name (resolved the "
            "same way `fm get SKILL --name ...` does; refuses on more than one "
            "match). Attachments under SOURCE are DEDUPED BY NAME, the same "
            "match rule `fm skill-attach` uses: a same-named attachment that "
            "already holds this exact content is left alone (a re-run is a "
            "no-op), one whose content changed is uploaded and swapped into "
            "the same slot, and a name with no existing match is uploaded "
            "and added. Existing attachments under OTHER names are never "
            "removed.\n\n"
            "SOURCE is a local directory holding SKILL.md, or a .zip of one.\n\n"
            "DRY RUN by default: prints what would change and touches nothing. "
            "--apply performs the write."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("--skill", required=True, help="skill id (UUID) or exact name")
    p.add_argument("source", help="a directory holding SKILL.md, or a .zip of one")
    p.add_argument("--apply", action="store_true", help="perform the write (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def cmd_skills_add(args) -> int:
    from formist_cli.cli import _effective, _token_for

    eff = _effective(args, for_write=True)
    token = _token_for(eff)
    if not args.apply:
        markdown, attachments, tmp = resolve_source(args.source)
        if tmp is not None:
            tmp.cleanup()
        print(
            f"DRY RUN: nothing was changed.\n"
            f"Would create a new skill in space {eff.space} ({eff.target.name}) "
            f"from {args.source} ({len(markdown)} chars of {SKILL_MD_NAME}"
            + (f", {len(attachments)} attachment(s)" if attachments else "")
            + ").\nTo apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0
    item = add_skill_from_source(eff.target, eff.space, args.source, token=token)
    if args.json:
        print(json_mod.dumps(item, indent=2, ensure_ascii=False))
    else:
        print(f"created: id={item['id']}  name={item['name']!r}  hook={item.get('hook')!r}")
        if item.get("files"):
            print(f"  attached {len(item['files'])} file(s)")
    return 0


def cmd_skills_update(args) -> int:
    from formist_cli.cli import _effective, _token_for
    from formist_cli.uploads import resolve_skill

    eff = _effective(args, for_write=True)
    token = _token_for(eff)
    skill = resolve_skill(args.skill, target=eff.target, space=eff.space, token=token)
    if not args.apply:
        markdown, attachments, tmp = resolve_source(args.source)
        if tmp is not None:
            tmp.cleanup()
        print(
            f"DRY RUN: nothing was changed.\n"
            f"Would replace skill {skill['id']} ({skill.get('name')!r}) in "
            f"space {eff.space} ({eff.target.name}) from {args.source} "
            f"({len(markdown)} chars of {SKILL_MD_NAME}"
            + (f", {len(attachments)} attachment(s)" if attachments else "")
            + ").\nTo apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0
    item = update_skill_from_source(eff.target, eff.space, skill, args.source, token=token)
    if args.json:
        print(json_mod.dumps(item, indent=2, ensure_ascii=False))
    else:
        print(f"updated: id={item['id']}  name={item['name']!r}")
        if item.get("files"):
            print(f"  attached {len(item['files'])} file(s)")
    return 0
