"""``fm import`` - load a workspace bundle into a space. The most destructive
command in this tool, so it is dry-run by default.

Three exclusions are properties of the server, not of this command, and all
three routinely surprise people trying to mirror one space into another:

1. SharePoint-sourced files do not travel, the rows, not just the bytes.
   A mirror built this way can have a knowledge base that looks present
   (the space opens, the counts are non-zero) while being mostly absent.
2. Connector credentials do not travel, and connection foreign keys are
   NULLed on the way in. So ``fm import`` can never configure the target
   with the same connectors as the source: that is not a gap to work
   around, it is the boundary that keeps a copied workspace from holding
   live credentials for somebody else's tenant.
3. Users resolve by email and accounts are never created. A member of the
   source who has no account on the target lands in ``unresolved_users``,
   which is why that list is printed rather than summarised away.

Why dry run is the default and ``--apply`` is the flag: the server computes
the full two-pass resolution under ``dry_run=true`` and returns the same
report it would return for real, writing nothing. So the safe call is also
the informative one, and there is no reason for the operator's first
attempt to be the write. This mirrors ``fm airtable direction``, and for
the same reason: the plan the operator authorizes should be a plan they
have seen.

The dry run and the apply are two separate uploads of the same archive,
and the space can move between them. A report is only true of the state
it ran against. So ``--apply`` re-runs the dry run itself, prints it,
applies, and then :func:`divergences` compares what was predicted with
what the server says happened, printed whether or not they agree, because
a comparison that is only shown when it fails is indistinguishable from
one that never ran. This is not a theoretical concern: the import takes
an advisory lock per project precisely because concurrent imports into one
space are expected.

Stdlib only: `fm` must add no runtime dependency.
"""

from __future__ import annotations

import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from formist_cli import api

#: The server's compressed-upload cap, mirrored client-side so a huge
#: upload is flagged before it is sent rather than after. This is a copied
#: constant and is used only to warn, never to refuse: if the deployment
#: raises its limit, a client-side refusal would block an upload the
#: server would have accepted. The server's own check is the one that
#: counts; this only predicts.
SERVER_ZIP_CAP_HINT = 200 * 1024 * 1024

#: Merge policies the server accepts. ``append`` is restricted to one kind
#: of row there; it is offered here because the server owns that
#: restriction and will say so better than a second copy of the rule here.
MERGE_POLICIES = ("skip-if-older", "overwrite", "skip-existing", "append")


class ImportRefused(Exception):
    """A refusal about an import, written for an operator.

    Named ``ImportRefused`` and not ``ImportError``: the builtin of that
    name is raised by the interpreter itself, and a module shadowing it
    would turn "your bundle is bad" and "a module failed to load" into one
    ``except`` clause.
    """


@dataclass(frozen=True)
class Bundle:
    """A bundle that has been read off disk and looks like a bundle."""

    path: Path
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def read_bundle(raw_path: str) -> Bundle:
    """Read the ZIP at ``raw_path``, refusing the ways an upload silently does nothing.

    Every check here is about a failure that would otherwise be reported as a
    successful import of an empty or wrong archive:

    * missing: the obvious one, and the only one that fails loudly anyway.
    * a directory: ``read_bytes`` raises ``IsADirectoryError``, whose message
      does not mention the bundle.
    * empty: zero bytes uploads fine and the server answers ``empty-bundle``;
      catching it here says which file, which the server cannot know.
    * not a ZIP: the common miss is passing the GDPR JSON from a plain
      ``fm export``, which is a perfectly good file and the wrong one entirely.
      The refusal names that case because it is the one that actually happens.
    * no manifest: a ZIP without ``manifest.json`` is not a bundle. The server
      rejects it after the whole upload; this rejects it before.
    """
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise ImportRefused(f"no such file: {path}")
    if path.is_dir():
        raise ImportRefused(
            f"{path} is a directory.\n"
            "-> Pass the bundle ZIP itself, not the folder holding it."
        )
    data = path.read_bytes()
    if not data:
        raise ImportRefused(
            f"{path} is 0 bytes.\n"
            "An empty file uploads perfectly well and the server would answer "
            "`empty-bundle` without naming it, so this is refused here where the "
            "filename is still known."
        )
    if not zipfile.is_zipfile(path):
        head = data[:1].decode("utf-8", "replace")
        hint = (
            "\nIt starts with `{`, this looks like the GDPR JSON from a plain "
            "`fm export`. The importable archive is the one built by "
            "`fm export --bundle`."
            if head == "{"
            else ""
        )
        raise ImportRefused(f"{path} is not a zip archive.{hint}")
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ImportRefused(f"{path} is a corrupt zip archive: {exc}") from None
    if "manifest.json" not in names:
        raise ImportRefused(
            f"{path} is a zip but carries no manifest.json, so it is not a "
            "workspace bundle.\n"
            "-> Build one with `fm export --bundle --host <host> --space <uuid>`."
        )
    return Bundle(path=path, data=data)


def _counts(section: object) -> list[tuple[str, int]]:
    """``{class: count}`` as sorted pairs, skipping zeros. ``[]`` for any other shape.

    Shape-tolerant on purpose: a report renderer written against a guessed
    shape, with a unit test encoding the same guess, is how the test and the
    code can agree with each other and not with the server.
    ``inserted``/``merged``/``skipped``/``dropped_rows`` are dicts on the
    wire; anything else is reported as unrecognised rather than silently
    rendering as empty.
    """
    if not isinstance(section, dict):
        return []
    out = []
    for name, value in section.items():
        if isinstance(value, int) and value:
            out.append((str(name), value))
    return sorted(out)


def _total(section: object) -> int:
    return sum(count for _, count in _counts(section))


def render_report(response: object) -> str:
    """Describe what the server said it did. Takes only the response.

    The request is deliberately not a parameter, so nothing here can echo
    what was asked for: a report assembled partly from the local payload
    confirms the request rather than the result, which is how a
    confirmation line can "confirm" whatever landed instead of what the
    server actually reported.
    """
    if not isinstance(response, dict):
        return str(response)

    lines: list[str] = []
    mode = "DRY RUN (nothing was written)" if response.get("dry_run") else "APPLIED"
    lines.append(f"mode       {mode}")
    for key, label in (
        ("source_project_id", "source"),
        ("bundle_id", "bundle_id"),
        ("merge", "merge"),
    ):
        if response.get(key) is not None:
            lines.append(f"{label:<10} {response[key]}")

    for key, label in (
        ("inserted", "inserted"),
        ("merged", "merged"),
        ("skipped", "skipped"),
        ("dropped_rows", "dropped"),
    ):
        section = response.get(key)
        pairs = _counts(section)
        if pairs:
            lines.append(f"{label:<10} {_total(section)}")
            for name, count in pairs:
                lines.append(f"    {name:<28} {count}")
        elif isinstance(section, dict):
            lines.append(f"{label:<10} 0")
        elif section is not None:
            lines.append(
                f"{label:<10} (unrecognised shape: {type(section).__name__}, "
                "this report cannot summarise it; read the JSON with --json)"
            )

    subs = response.get("user_substitutions")
    if subs:
        lines.append(f"user_subs  {subs}")

    # The four unflattering lists, always printed when non-empty. A bundle that
    # dropped references or could not fetch blobs imported INCOMPLETELY, and a
    # summary that prints the counts while omitting the holes describes a partial
    # import as a whole one.
    for key, label, note in (
        (
            "unresolved_users",
            "UNRESOLVED USERS",
            "no account on this target; accounts are never created by an import",
        ),
        (
            "missing_blobs",
            "MISSING BLOBS",
            "rows landed whose file bytes did not, they will 404 when opened",
        ),
        ("conflicts", "CONFLICTS", "the merge policy chose; check it was the one you wanted"),
        ("dropped_refs", "DROPPED REFS", "references that pointed at rows not in the bundle"),
    ):
        items = response.get(key)
        if isinstance(items, list) and items:
            lines.append(f"{len(items)} {label}: {note}:")
            for item in items[:20]:
                lines.append(f"    {item}")
            if len(items) > 20:
                lines.append(f"    ... and {len(items) - 20} more (use --json for all)")
    return "\n".join(lines)


def divergences(dry: object, applied: object) -> list[str]:
    """Where the applied report disagrees with the dry run that authorized it.

    Returns a list of human sentences; empty means they agreed. Compared per class
    and per bucket rather than as whole dicts, so the message can name WHICH class
    moved, since "something changed" sends an operator to read two JSON blobs by eye.

    ``dry_run`` itself is expected to differ and is excluded; so are the free-text
    lists, which legitimately differ in ORDER without differing in content.
    """
    out: list[str] = []
    if not isinstance(dry, dict) or not isinstance(applied, dict):
        return ["the two reports were not both objects, so they cannot be compared."]
    for bucket in ("inserted", "merged", "skipped", "dropped_rows"):
        before = dict(_counts(dry.get(bucket)))
        after = dict(_counts(applied.get(bucket)))
        for name in sorted(set(before) | set(after)):
            if before.get(name, 0) != after.get(name, 0):
                out.append(
                    f"{bucket}.{name}: dry run predicted {before.get(name, 0)}, "
                    f"the apply reported {after.get(name, 0)}"
                )
    for bucket in ("unresolved_users", "missing_blobs", "conflicts", "dropped_refs"):
        b = dry.get(bucket)
        a = applied.get(bucket)
        if isinstance(b, list) and isinstance(a, list) and len(b) != len(a):
            out.append(f"{bucket}: dry run had {len(b)}, the apply reported {len(a)}")
    return out


# ===========================================================================
# CLI surface. Per-module `add_parser`: adding a command costs the top-level
# parser one line instead of a block two branches both land in.
# ===========================================================================
def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "import",
        help="load a workspace bundle into a space (DRY RUN by default)",
        description=(
            "POST /projects/{id}/import, owner/admin only.\n\n"
            "DRY RUN BY DEFAULT. The server runs the full two-pass resolution and "
            "returns the real report without writing, so your first attempt is "
            "never the write. --apply re-runs the dry run, prints it, applies, and "
            "then compares what was predicted against what happened.\n\n"
            "--apply never inherits the environment from `fm use`. If one is "
            "remembered, the write is refused until you name --env on the command "
            "line; with nothing remembered it targets the built-in default "
            "(local). An import rewrites a workspace, and an environment is one "
            "word that decides whether this is production, so it comes from the "
            "command you just ran, never from a setting made earlier.\n\n"
            "Three things a bundle cannot carry, all server-side, all surprising:\n"
            "  - SharePoint-sourced files, rows included, not just the bytes. A "
            "mirror built from a bundle can look present and be mostly empty.\n"
            "  - connector credentials; connection FKs are NULLed on import. This "
            "command cannot give the target the source's connectors, by design.\n"
            "  - users. They resolve by email and accounts are never created; the "
            "rest land in `unresolved_users`, which is printed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p)
    _add_space(p)
    p.add_argument("bundle", help="path to the bundle ZIP from `fm export --bundle`")
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually write (the dry run still runs first). Refuses a remembered "
            "`fm use` environment: pass --env to write anywhere but local"
        ),
    )
    p.add_argument(
        "--merge",
        default="skip-if-older",
        choices=MERGE_POLICIES,
        help="how an existing row is treated (default: skip-if-older)",
    )
    p.add_argument(
        "--include-history",
        dest="include_history",
        action="store_true",
        help="also import chats/proposals/audit/notifications, if the bundle has them",
    )
    p.add_argument(
        "--include-document-index",
        dest="include_document_index",
        action="store_true",
        help="also import converted document text (the agent's retrieval surface)",
    )
    p.add_argument("--json", action="store_true", help="print the raw server report")


def _send(eff, token, bundle: Bundle, args, *, dry: bool) -> object:
    return api.post_multipart(
        eff.target,
        f"/projects/{eff.space}/import"
        f"?dry_run={'true' if dry else 'false'}"
        f"&merge={args.merge}"
        f"&include_history={'true' if args.include_history else 'false'}"
        f"&include_document_index="
        f"{'true' if args.include_document_index else 'false'}",
        fields={},
        files={"file": (bundle.path.name, bundle.data)},
        token=token,
    )


def cmd_import(args) -> int:
    """Import a bundle, showing the plan before it can become a write."""
    # Lazy: `cli` imports this module, so a module-level import would be a cycle.
    import json as _json

    from formist_cli.cli import _effective, _token_for

    # Read the file before resolving the target: a refusal about the bundle is
    # about the bundle, and making the operator fix an --env to be told their
    # path is wrong wastes the round trip that was going to fail anyway.
    bundle = read_bundle(args.bundle)
    if bundle.size > SERVER_ZIP_CAP_HINT:
        print(
            f"  note: {bundle.size} bytes is above the {SERVER_ZIP_CAP_HINT}-byte "
            "cap this client last knew about. Sending anyway, the server owns "
            "that limit and will refuse if it still applies.",
            file=sys.stderr,
        )

    # for_write is the --apply flag itself: a dry run writes nothing and may
    # inherit an environment; the write may not.
    eff = _effective(args, for_write=bool(args.apply))
    token = _token_for(eff)

    print(
        f"  bundle {bundle.path}  ({bundle.size} bytes)",
        file=sys.stderr,
    )

    dry = _send(eff, token, bundle, args, dry=True)
    if args.json and not args.apply:
        print(_json.dumps(dry, indent=2))
    else:
        print(render_report(dry))

    if not args.apply:
        print(
            "\n-> nothing was written. Re-run with `--apply --host <host>` to "
            "persist this.",
            file=sys.stderr,
        )
        return 0

    print("\n  applying...", file=sys.stderr)
    applied = _send(eff, token, bundle, args, dry=False)
    print("\n" + render_report(applied))
    if args.json:
        print(_json.dumps(applied, indent=2))

    # Printed whether or not they agree: a comparison shown only on failure is
    # indistinguishable from one that never ran.
    diffs = divergences(dry, applied)
    if diffs:
        print(
            "\nTHE APPLY DID NOT MATCH THE DRY RUN THAT AUTHORIZED IT:",
            file=sys.stderr,
        )
        for line in diffs:
            print(f"    {line}", file=sys.stderr)
        print(
            "    The space changed between the two calls, or the merge policy hit "
            "rows the dry run did not see. The import has been applied; this is a "
            "report, not a rollback.",
            file=sys.stderr,
        )
        return 1
    print(
        "\n  the apply matched the dry run that authorized it "
        f"({_total(applied.get('inserted') if isinstance(applied, dict) else None)} "
        "inserted, "
        f"{_total(applied.get('merged') if isinstance(applied, dict) else None)} "
        "merged).",
        file=sys.stderr,
    )
    return 0
