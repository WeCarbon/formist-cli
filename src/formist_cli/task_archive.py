"""The local read-only archive marker, the fail-closed local write guard,
and backup/restore — the local half of the one-time cloud import's
post-readback archive step (C1126/C1330's 「备份、归档与恢复」, C1346).

``task_import.py`` deliberately never marks a source archive read-only
itself (see its own module docstring); this module is that separate,
explicit, post-readback operation, plus everything it depends on:

- The marker lives in ``decs.json``'s ``archive`` key (which
  ``task_project.read_project_config``/``write_project_config`` already
  read and write — see that module's own docstring for exactly where
  ``decs.json`` lives on disk). Only ``write_archive_marker`` and
  ``clear_archive_marker`` below may set or clear it; the AUTHORITY to
  call them lives in the ``--archive-local``/``--rollback`` CLI verbs
  (``task_import.py``), not here — this module only enforces the
  precondition each one already checked (a verified backup, or a
  passed server rollback) once more, belt-and-suspenders.
- :func:`guard_write` is the one check every other local write entry
  point calls before touching disk — ``task_store.LocalStore.mutate``/
  ``create``, ``task_queue.with_queue`` (the ``enqueue``/``dequeue``
  bypass of ``mutate``'s own lock — see that module's docstring),
  ``task_project.write_project_config`` (``fm task config``, a second
  write path onto ``decs.json`` that doesn't go through ``LocalStore``
  at all) and ``task_monitor.save_cursor`` (``decisions/.monitor/``, a
  bookkeeping write under ``decisions/`` itself). Read-only verbs
  (``get``/``show``/``tree``/``search``/``check``/...) call none of
  these and are unaffected. C1330's own warning is the reason every one
  of those is listed here, not just the obvious node-edit verbs: "fm
  和 nod 都必须在所有本机写入口读取同一个标记并 fail closed。只改
  formist-cli 会留下 nod 继续写的洞" — a missed entry point is exactly
  that hole, on the `fm` side of it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from formist_cli import task_project, tokens
from formist_cli.task_lock import write_file_atomic
from formist_cli.task_paths import Layout


class ArchiveError(ValueError):
    """An archive/backup/restore refusal the CLI layer should print as an
    error, not a traceback — same convention as every other ``*Error`` in
    this package (``task_store.StoreError``, ``task_attach.AttachError``,
    ...), registered in ``tasks._task_user_errors()``."""


class ArchivedReadOnlyError(ArchiveError):
    """A local write was refused because this project is archived
    read-only. The one exception raised by :func:`guard_write`."""


class BackupVerificationError(ArchiveError):
    """A backup's on-disk copy no longer matches its own manifest —
    missing, extra, or changed files. Never usable as the precondition
    for archiving, and never usable as a restore source."""


def _now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# -- the marker ---------------------------------------------------------


def read_archive(layout: Layout) -> dict[str, Any] | None:
    """This project's ``archive`` marker, or ``None`` if it has none (or
    the field is malformed — treated the same as absent, never trusted
    half-written)."""
    cfg = task_project.read_project_config(layout)
    archive = cfg.get("archive")
    return archive if isinstance(archive, dict) else None


def is_archived_read_only(layout: Layout) -> bool:
    archive = read_archive(layout)
    return bool(archive and archive.get("read_only"))


def guard_write(layout: Layout) -> None:
    """The one check every local write entry point must call, first,
    before touching disk. Raises :class:`ArchivedReadOnlyError` when this
    project is archived read-only; otherwise returns silently."""
    archive = read_archive(layout)
    if archive and archive.get("read_only"):
        raise ArchivedReadOnlyError(
            f"this project is archived read-only (imported as {archive.get('target')!r}, "
            f"import_id={archive.get('import_id')!r}, archived {archive.get('archived_at')!r}); "
            "local writes are refused. Roll back the cloud import "
            "(`fm task import --rollback <receipt> --confirm <import_id>`) to lift this."
        )


def write_archive_marker(layout: Layout, *, import_id: str, target: str, backup_id: str) -> dict[str, Any]:
    """Sets the marker. Only ``--archive-local`` may call this, and only
    after it has itself verified a passed-readback receipt and a
    verified backup (the authority lives there, not here) — this still
    refuses to clobber an existing marker, belt-and-suspenders."""
    if is_archived_read_only(layout):
        raise ArchivedReadOnlyError("this project is already archived read-only")
    marker = {
        "read_only": True,
        "import_id": import_id,
        "target": target,
        "backup_id": backup_id,
        "archived_at": _now_stamp(),
    }
    cfg = task_project.write_project_config(layout, archive=marker, _allow_when_archived=True)
    return cfg["archive"]


def clear_archive_marker(layout: Layout, *, verified: bool) -> dict[str, Any]:
    """Clears the marker. Only ``--rollback`` may call this, and only
    once the server has confirmed ``rolled_back=true`` for the matching
    ``import_id`` — ``verified`` is a defensive re-check of that
    authority (the caller must pass ``True`` only once it has that
    proof in hand), not a substitute for it."""
    if not verified:
        raise ArchivedReadOnlyError("refusing to clear the archive marker without a verified rollback")
    if not is_archived_read_only(layout):
        raise ArchiveError("this project is not archived; nothing to clear")
    return task_project.write_project_config(layout, archive=None, _allow_when_archived=True)


# -- backup / restore -----------------------------------------------------


def backup_root() -> Path:
    return tokens.config_dir() / "archive-backups"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): _sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _manifest_digest(manifest: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _diff_against_manifest(current: dict[str, str], manifest: dict[str, str]) -> list[dict[str, str]]:
    """Every path where ``current`` (a real directory's own
    ``_hash_tree``) disagrees with ``manifest`` — missing, extra, or a
    changed hash. Empty means an exact match."""
    diffs: list[dict[str, str]] = []
    for rel, digest in manifest.items():
        cur = current.get(rel)
        if cur is None:
            diffs.append({"path": rel, "kind": "missing"})
        elif cur != digest:
            diffs.append({"path": rel, "kind": "changed"})
    for rel in current:
        if rel not in manifest:
            diffs.append({"path": rel, "kind": "extra"})
    return diffs


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.rglob("*")):
        if p.is_file():
            out_path = dst / p.relative_to(src)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out_path)


def _backup_dir(backup_id: str) -> Path:
    return backup_root() / backup_id


def _read_backup_record(backup_id: str) -> dict[str, Any]:
    path = _backup_dir(backup_id) / "manifest.json"
    if not path.is_file():
        raise BackupVerificationError(f"no backup manifest for {backup_id}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupVerificationError(f"cannot read backup manifest {path}: {exc}") from exc
    if not isinstance(record, dict) or not isinstance(record.get("manifest"), dict):
        raise BackupVerificationError(f"malformed backup manifest {path}")
    return record


def make_backup(layout: Layout) -> dict[str, Any]:
    """Byte-for-byte snapshot of the whole local source — the entire
    ``decisions/`` tree (which already contains ``decs.json``,
    ``active/``, ``archived/`` and ``attachments/`` — see
    ``task_paths.layout_for``) — into Formist's own config dir, outside
    the project. Self-verifies before returning: a backup that doesn't
    round-trip is never handed back as usable (C1330: "备份写到项目外的
    Formist 配置目录 ... 并做一次解包目录和每文件 hash 验证")."""
    source = Path(layout.root)
    if not source.is_dir():
        raise ArchiveError(f"no decisions/ directory at {source}")
    manifest = _hash_tree(source)
    backup_id = str(uuid.uuid4())
    dest = _backup_dir(backup_id)
    dest.mkdir(parents=True, exist_ok=False)
    _copy_tree(source, dest / "files")
    record = {
        "backup_id": backup_id,
        "source_root": str(source),
        "created_at": _now_stamp(),
        "file_count": len(manifest),
        "manifest": manifest,
        "manifest_hash": _manifest_digest(manifest),
    }
    write_file_atomic(str(dest / "manifest.json"), json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    verify_backup(backup_id)  # fail loudly now, not whenever archiving happens to read it back
    return record


def verify_backup(backup_id: str) -> dict[str, Any]:
    """Re-hashes every file the backup's own manifest claims to hold and
    compares. Raises :class:`BackupVerificationError` on any mismatch —
    missing file, extra file, or a changed hash — rather than returning
    a verdict a caller might forget to check."""
    record = _read_backup_record(backup_id)
    current = _hash_tree(_backup_dir(backup_id) / "files")
    diffs = _diff_against_manifest(current, record["manifest"])
    if diffs:
        raise BackupVerificationError(f"backup {backup_id} failed verification: {diffs}")
    if _manifest_digest(record["manifest"]) != record.get("manifest_hash"):
        raise BackupVerificationError(f"backup {backup_id}'s own manifest_hash does not match its manifest")
    return record


def _unique_restore_dir(target: Path, backup_id: str) -> Path:
    base = target.parent / f"{target.name}-restored-{backup_id[:8]}"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = Path(f"{base}-{n}")
        n += 1
    return candidate


def restore_backup(backup_id: str, target_dir: str | Path) -> dict[str, Any]:
    """Verifies the backup first (raises on a corrupted one — never used
    as a restore source), then restores. Never silently overwrites an
    existing ``target_dir`` whose current bytes differ from the backup's
    manifest: when it has diverged, this restores into a fresh sibling
    directory and reports the diff instead of clobbering (C1330: "不能
    自动覆盖现有目录;若目录 bytes 已变化,恢复到新目录并报告差异")."""
    record = verify_backup(backup_id)
    target = Path(target_dir)
    files_dir = _backup_dir(backup_id) / "files"
    if target.exists():
        current = _hash_tree(target)
        diffs = _diff_against_manifest(current, record["manifest"])
        if diffs:
            fresh = _unique_restore_dir(target, backup_id)
            _copy_tree(files_dir, fresh)
            return {"backup_id": backup_id, "restored_to": str(fresh), "diverged": True, "diff": diffs}
    _copy_tree(files_dir, target)
    return {"backup_id": backup_id, "restored_to": str(target), "diverged": False, "diff": []}
