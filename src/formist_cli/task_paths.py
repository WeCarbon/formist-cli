"""Where ``decisions/`` lives and how its files are named — a port of
noddit's ``core/paths.ts`` (plus the id-allocation half of ``store.ts``'s
private ``reserveId``, since there is no separate "sequence" file on the
TS side either — see the C1052 research spec, §2.2).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

ENTRY_NAME_RE = re.compile(r"^D?(\d+)(?:-(.*))?\.md$")
_CHILD_DIR_RE = re.compile(r"^D?(\d+)(?:-.*)?$")


@dataclass(frozen=True)
class Layout:
    project_root: str
    root: str
    active_dir: str
    archived_dir: str
    ids_dir: str


def to_posix_path(p: str) -> str:
    return p.replace(os.sep, "/") if os.sep != "/" else p


def _canonical(p: str) -> str:
    try:
        return os.path.realpath(p, strict=True)
    except OSError:
        return os.path.abspath(p)


def layout_for(project_root: str) -> Layout:
    root_abs = _canonical(project_root)
    root = os.path.join(root_abs, "decisions")
    return Layout(
        project_root=root_abs,
        root=root,
        active_dir=os.path.join(root, "active"),
        archived_dir=os.path.join(root, "archived"),
        ids_dir=os.path.join(root, ".ids"),
    )


def main_worktree(cwd: str) -> str | None:
    """The repo's main working tree, or ``None`` outside git — mirrors
    ``paths.ts``'s ``mainWorktree``: ``--git-common-dir`` finds the shared
    ``.git`` from any linked worktree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    common = out.stdout.strip()
    return os.path.dirname(common) if common else None


def find_layout(cwd: str | None = None) -> Layout | None:
    """The main working tree wins over a linked worktree's own stale copy
    of ``decisions/``; outside git, or with none in the main tree, walk up
    from ``cwd``."""
    cwd = cwd or os.getcwd()
    main = main_worktree(os.path.abspath(cwd))
    if main and os.path.isdir(os.path.join(main, "decisions", "active")):
        return layout_for(main)

    d = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(d, "decisions", "active")):
            return layout_for(d)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def require_layout(cwd: str | None = None) -> Layout:
    layout = find_layout(cwd)
    if not layout:
        raise RuntimeError(
            "no decisions/ directory found here or in any parent.\n"
            "Run `fm task init` in the project root to create one."
        )
    return layout


def init_layout(project_root: str) -> Layout:
    layout = layout_for(project_root)
    for d in (layout.root, layout.active_dir, layout.archived_dir, layout.ids_dir):
        os.makedirs(d, exist_ok=True)
    return layout


def entry_file_name(id_: int, slug: str) -> str:
    return f"{id_}-{slug}.md" if slug else f"{id_}.md"


def attachment_dir(layout: Layout, id_: int) -> str:
    legacy = os.path.join(layout.root, "attachments", f"D{id_}")
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(layout.root, "attachments", str(id_))


def child_dir_of(file: str) -> str:
    if file.endswith(".md"):
        return file[: -len(".md")]
    return file


@dataclass(frozen=True)
class EntryFile:
    id: int
    slug: str
    path: str
    archived: bool
    parent: int | None
    mtime_ns: int = 0
    size: int = 0


def list_entry_files(layout: Layout) -> list[EntryFile]:
    out: list[EntryFile] = []

    def walk(d: str, archived: bool, parent: int | None) -> None:
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isdir(p):
                m = _CHILD_DIR_RE.match(name)
                if m:
                    walk(p, archived, int(m.group(1)))
                continue
            m = ENTRY_NAME_RE.match(name)
            if not m or not os.path.isfile(p):
                continue
            out.append(EntryFile(
                id=int(m.group(1)), slug=m.group(2) or "", path=p,
                archived=archived, parent=parent,
                mtime_ns=st.st_mtime_ns, size=st.st_size,
            ))

    walk(layout.active_dir, False, None)
    walk(layout.archived_dir, True, None)
    out.sort(key=lambda e: e.id)
    return out


def reserve_id(layout: Layout) -> int:
    """Claim the next free node number, exactly as ``Store.reserveId`` does:
    the directory listing itself is the counter (no persisted counter row),
    claimed cross-process with an O_EXCL zero-byte file under ``.ids/``."""
    os.makedirs(layout.ids_dir, exist_ok=True)
    used = {e.id for e in list_entry_files(layout)}
    n = (max(used) if used else 0) + 1
    while True:
        claim_path = os.path.join(layout.ids_dir, str(n))
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            n += 1
            continue
        active_plain = os.path.join(layout.active_dir, f"{n}.md")
        active_legacy = os.path.join(layout.active_dir, f"D{n}.md")
        if n not in used and not os.path.exists(active_plain) and not os.path.exists(active_legacy):
            return n
        n += 1


def release_id(layout: Layout, id_: int) -> None:
    try:
        os.remove(os.path.join(layout.ids_dir, str(id_)))
    except OSError:
        pass
