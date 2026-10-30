"""``fm export`` - two different exports, one of which is a write.

The two routes are not the same kind of thing. ``GET /projects/{id}/export``
is a read: one JSON bundle, open to any member regardless of role, so a
viewer reaches it.

``POST /projects/{id}/export/bundle`` is a write. It inserts a File row
under Files > Exports, writes blob bytes into storage, and emits an
export event. Every member sees the artefact afterwards. It is
owner/admin only, and the download shares that tier, because gating the
build while leaving the download open would let a viewer exfiltrate the
entire workspace with nothing but the file id.

A command called "export" reads as a read on both halves, which is
exactly how the second one gets run against the wrong environment. So the
bundle half goes through ``for_write=True`` in the CLI and can never
inherit ``fm use``'s environment.

What the GDPR bundle contains, and the scoping caveat, is stated here and
in ``--help`` because ``--help`` is the only place an operator looks
before running it.

The bundle carries every project-scoped table plus a members roster with
email addresses and a files manifest (names, paths, web_urls). Secrets
are scrubbed: connection credentials redacted, API-token hashes dropped.

On some deployments the per-member scoping of four of those tables (chat
transcripts, personal notifications, audit before/after snapshots, and
document_index.markdown_text) is wider than the per-member access the
rest of the product enforces: any member, viewer included, can reach all
four through this route, which never mentions them. The operator who ran
this needs to know what they just downloaded; a file on disk outlives
the release note, so :data:`SCOPING_NOTE` is printed every time.

A file-writing command can lie in three ways. It can overwrite something
(unrecoverable, and looks like success). It can write nothing and print a
path, and an empty export file is that failure with a different noun. Or
it can write somewhere else, because the filename came from the server's
``Content-Disposition`` and a path separator in it is a path nobody chose.

All three are refusals below rather than cautions. Nothing else in `fm`
takes a filename out of a response, so nothing else is watching that
last one.

Stdlib only: `fm` must add no runtime dependency.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from formist_cli import api, context

#: What the GDPR bundle carries, for `--help`. Kept as data rather than prose in
#: one f-string so the list cannot drift out of the help text silently.
BUNDLE_CONTENTS = (
    "every project-scoped table (stakeholders, sessions, tasks, files, entities...)",
    "a MEMBERS ROSTER including email addresses",
    "a files manifest: names, paths, web_urls",
    "chat transcripts, notifications, audit snapshots, document text",
)

SCOPING_NOTE = (
    "PERSONAL DATA. The per-member scoping of chats, notifications, audit "
    "before/after snapshots and document_index.markdown_text can be wider than "
    "the rest of the product enforces: any member, VIEWER included, may be able "
    "to export all four on some deployments. The file you just wrote may "
    "contain other people's private data and the full text of restricted "
    "documents. Check what you are about to share it with."
)


class ExportError(Exception):
    """A refusal about an export, written for an operator."""


@dataclass(frozen=True)
class Written:
    """Where the bytes actually landed. Reported instead of where they were aimed."""

    path: Path
    bytes_written: int


def filename_from_disposition(header: str | None) -> str | None:
    """The filename out of a ``Content-Disposition``, or ``None``.

    ``None`` rather than a guess: a caller that cannot tell what the server called
    the file should fall back deliberately, not be handed an invention that looks
    like it came from the response.
    """
    if not header:
        return None
    # RFC 5987 `filename*=UTF-8''%E5%90%88...` wins: it is the only form that can
    # carry a Chinese or Arabic name, and read as a plain value it came out as the
    # literal "UTF-8''%E5%90%88..." on disk.
    extended = re.search(r"filename\*\s*=\s*([^';]+)'[^']*'([^;]+)", header, re.IGNORECASE)
    if extended:
        charset, encoded = extended.group(1).strip(), extended.group(2).strip()
        try:
            name = unquote(encoded, encoding=charset or "utf-8", errors="strict").strip()
        except (LookupError, UnicodeDecodeError):
            name = ""
        if name:
            return name
    match = re.search(r'(?<![*\w])filename\s*=\s*(?:"([^"]+)"|([^;]+))', header, re.IGNORECASE)
    if not match:
        return None
    name = (match.group(1) or match.group(2) or "").strip()
    return name or None


def safe_filename(name: str | None) -> str:
    """The bare filename, with any path the server supplied stripped off.

    ``Content-Disposition`` names a file. It never names a path, and a server
    that puts one there (compromised, buggy, or proxied) must not be able to
    choose where an operator's bytes land. ``os.path.basename`` semantics,
    applied to both separators because a response is not necessarily produced
    on this platform.
    """
    if not name:
        raise ExportError("the server supplied no usable filename.")
    bare = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    # `..` survives basename(): it is a valid final segment, so it is rejected
    # explicitly rather than assumed away.
    if not bare or bare in {".", ".."}:
        raise ExportError(
            f"refusing the server-supplied filename {name!r}: it names a path, "
            "not a file.\n-> Pass `-o <path>` to choose the destination yourself."
        )
    return bare


def write_export(path: Path, data: bytes) -> Written:
    """Write ``data`` to ``path``, refusing to overwrite and refusing to write nothing.

    The empty check comes first, so a zero-byte response cannot leave a file
    behind. If it did, the refusal would create the very file the next run
    then refuses to overwrite, a failed export that permanently blocks the
    retry.
    """
    path = Path(path)
    if not data:
        raise ExportError(
            f"the server returned 0 bytes, nothing was written to {path}.\n"
            "An empty export file is indistinguishable from a successful one on "
            "disk, so this is a refusal rather than a warning.\n"
            "-> Check `fm health` and that the space id is right."
        )
    if path.exists():
        raise ExportError(
            f"refusing to overwrite {path}.\n"
            "An export is a point-in-time copy and replacing one silently is not "
            "recoverable.\n"
            "-> Move or delete it, or pass `-o <other path>`."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Written(path=path, bytes_written=len(data))


def destination(out: str | None, server_filename: str | None, fallback: str) -> Path:
    """Where to write: ``-o`` wins, then the server's name, then ``fallback``.

    ``-o`` pointing at a DIRECTORY keeps the server's filename inside it, because
    ``fm export -o ~/exports`` obviously means "put it there", and silently
    creating a file literally named ``exports`` would be a surprising reading of an
    unambiguous instruction.
    """
    name = safe_filename(server_filename) if server_filename else fallback
    if out is None:
        return Path(name)
    chosen = Path(out).expanduser()
    if chosen.is_dir():
        return chosen / name
    return chosen


def precheck_out(out: str | None) -> None:
    """Refuse an occupied ``-o`` destination before anything is built server-side.

    ``write_export`` already refuses to overwrite, but it runs at the end,
    and for ``--bundle`` the end is after a POST that has already inserted
    a File row, written blob bytes and emitted an export event. So a
    mistyped ``-o`` would leave a real artefact in the space that the
    operator then cannot receive, and the only way to "fix" it is another
    build, i.e. another row.

    Only possible when ``-o`` names a file: with no ``-o``, or with a
    directory, the destination is not known until the server says what it
    called the bundle. That is why this is a pre-check in addition to, and
    not instead of, the one in :func:`write_export`.
    """
    if out is None:
        return
    chosen = Path(out).expanduser()
    if chosen.is_dir():
        return
    if chosen.exists():
        raise ExportError(
            f"refusing to overwrite {chosen}.\n"
            "Checked BEFORE building: --bundle creates a File row in the space, "
            "so failing after the build would leave an artefact behind that you "
            "could not download.\n"
            "-> Move or delete it, or pass a different `-o`."
        )


def render_bundle_report(response: object) -> str:
    """Describe what the server said it built. Takes only the response.

    The request is not a parameter, so it cannot be echoed: a report
    assembled from the options that were requested would confirm whatever
    was asked for rather than whatever happened.

    ``missing_blobs`` is reported even though it is the unflattering number. A
    bundle with blobs it could not fetch is incomplete, and printing the counts
    while omitting the holes describes an incomplete export as a whole one.
    """
    if not isinstance(response, dict):
        return str(response)

    lines: list[str] = []
    if response.get("filename"):
        lines.append(f"filename   {response['filename']}")
    if response.get("file_id"):
        lines.append(f"file_id    {response['file_id']}")

    manifest = response.get("manifest")
    if isinstance(manifest, dict):
        if manifest.get("source_project_name"):
            lines.append(f"space      {manifest['source_project_name']}")
        if manifest.get("bundle_id"):
            lines.append(f"bundle_id  {manifest['bundle_id']}")
        options = manifest.get("options")
        if isinstance(options, dict):
            enabled = sorted(k for k, v in options.items() if v)
            lines.append(f"options    {', '.join(enabled) if enabled else '(defaults)'}")
        # `classes` is a list of {name, file, count} dicts on the wire, not a
        # dict of name->count: treating it as the latter silently skips the
        # most useful part of the report, the per-class row counts.
        classes = manifest.get("classes")
        if isinstance(classes, list) and classes:
            lines.append(f"classes    {len(classes)}")
            for entry in classes:
                if isinstance(entry, dict):
                    lines.append(
                        f"    {str(entry.get('name', '?')):<28} {entry.get('count')}"
                    )
        elif classes is not None:
            # Present but not the shape we know. Saying so beats skipping: a
            # silently omitted section reads as "the bundle had none of that",
            # which is how the wrong shape stayed invisible the first time.
            lines.append(
                f"classes    (unrecognised shape: {type(classes).__name__}, "
                "the report cannot summarise it; read manifest.json in the zip)"
            )
        blobs = manifest.get("blobs")
        if isinstance(blobs, dict):
            lines.append(
                f"blobs      {blobs.get('count')} "
                f"({blobs.get('total_bytes')} bytes)"
            )
        missing = manifest.get("missing_blobs")
        if missing:
            lines.append(
                f"{len(missing)} MISSING blob(s): this bundle is incomplete "
                "and will not round-trip:"
            )
            for item in missing:
                lines.append(f"    {item}")
    return "\n".join(lines)


# ===========================================================================
# The CLI surface. Per-module `add_parser`: adding a command costs the
# top-level parser one line instead of a block two branches both land in.
# ===========================================================================
def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space
    p = sub.add_parser(
        "export",
        help="download a space's data bundle to a file",
        description=(
            "TWO different exports, and only the first is a read.\n\n"
            "  (default)   GET /projects/{id}/export, the GDPR Article 20 JSON "
            "bundle. Any MEMBER may run it, VIEWER included.\n"
            "  --bundle    POST /projects/{id}/export/bundle, the round-trip ZIP "
            "with blob bytes. OWNER/ADMIN.\n\n"
            "--bundle is a WRITE. It inserts a File row, writes blob bytes "
            "into the space's storage and emits an export event; every "
            "member sees the artefact afterwards. So it never inherits the "
            "environment from `fm use`: with one remembered the write is REFUSED "
            "until you pass --env, and with nothing remembered it targets the "
            "built-in default (local).\n\n"
            "The bundle contains:\n"
            + "\n".join(f"  - {line}" for line in BUNDLE_CONTENTS)
            + "\n\n"
            + SCOPING_NOTE
            + "\n\nThe file is never overwritten and never written empty; the "
            "path printed at the end is the path that was actually written."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument(
        "--bundle",
        action="store_true",
        help="the round-trip ZIP instead of the JSON bundle (a write; OWNER/ADMIN)",
    )
    p.add_argument(
        "--include-history",
        dest="include_history",
        action="store_true",
        help="--bundle only: include chats/proposals/audit/notifications",
    )
    p.add_argument(
        "--include-document-index",
        dest="include_document_index",
        action="store_true",
        help="--bundle only: include converted document text",
    )
    p.add_argument("-o", "--out", help="destination file, or a directory to write into")


def cmd_export(args) -> int:
    """Download a space's data to a file, saying what is in it before it lands.

    The two halves differ in the one way that matters: the JSON bundle is a read
    and may inherit `fm use`'s environment, while --bundle is a write and cannot.
    That is not a stylistic split: POST /export/bundle creates a File row every
    member can see and emits an event, so running it against the wrong space is
    visible to that space's members.
    """
    if args.bundle:
        return _export_bundle(args)
    return _export_gdpr(args)


def _export_gdpr(args) -> int:
    # Lazy: `cli` imports this module, so a module-level import here
    # would be a cycle.
    eff = context.resolve(args, need_space=True, need_token=True)
    token = eff.token
    precheck_out(args.out)

    body, disposition = api.download(
        eff.target, f"/projects/{eff.space}/export", token=token
    )
    path = destination(
        args.out,
        filename_from_disposition(disposition),
        f"space-{eff.space}.json",
    )
    written = write_export(path, body)
    print(f"wrote {written.path}  ({written.bytes_written} bytes)")
    print(SCOPING_NOTE, file=sys.stderr)
    return 0


def _export_bundle(args) -> int:
    # Lazy: `cli` imports this module, so a module-level import here
    # would be a cycle.
    eff = context.resolve(args, for_write=True, need_space=True, need_token=True)
    token = eff.token

    # BEFORE the POST: the build inserts a File row and emits an event, so a
    # destination that will be refused must be refused now, not after the space
    # has an artefact in it that this run cannot deliver.
    precheck_out(args.out)

    # Said BEFORE the call, not after. This is the half an operator does not expect
    # from a command called "export", and afterwards is too late to decline.
    print(
        "  --bundle BUILDS the archive on the server: it adds a File row and "
        "emits an export event. Every member of this space will see it.",
        file=sys.stderr,
    )

    response = api.post(
        eff.target,
        f"/projects/{eff.space}/export/bundle",
        {
            "include_history": args.include_history,
            "include_document_index": args.include_document_index,
        },
        token=token,
    )
    print(render_bundle_report(response))

    if not isinstance(response, dict) or not response.get("file_id"):
        raise ExportError(
            "the server accepted the build but returned no file_id, so there is "
            "nothing to download.\n"
            "The archive may still exist among the space's files, check there "
            "before rebuilding, or a second run adds a second copy."
        )

    file_id = response["file_id"]
    body, disposition = api.download(
        eff.target,
        f"/projects/{eff.space}/export/bundle/{file_id}",
        token=token,
    )
    # The POST's `filename` wins over Content-Disposition: the download
    # route hardcodes a generic filename, while the POST returns the
    # timestamped, space-slugged name the server actually filed the
    # artefact under. Preferring the header would make every bundle from
    # every space land on disk under the same generic filename, which the
    # no-overwrite rule then turns into a refusal on the second export.
    path = destination(
        args.out,
        response.get("filename") or filename_from_disposition(disposition),
        f"evane-export-{eff.space}.zip",
    )
    written = write_export(path, body)
    print(f"\nwrote {written.path}  ({written.bytes_written} bytes)")
    print(SCOPING_NOTE, file=sys.stderr)
    return 0
