"""Safe, restartable local-task import client.

This module deliberately owns only the client side of the local-import
protocol.  In particular it never marks a source archive read-only: that is
a separate, post-readback operation shared with noddit.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from formist_cli import api, context, task_json, task_paths, tokens, uploads
from formist_cli.task_lock import with_file_lock, write_file_atomic
from formist_cli.task_store import LocalStore


class ImportError(Exception):
    pass


_VERSION = 1
_NAMESPACE = uuid.UUID("2db0e857-825c-55ae-8ec0-a7416f6cbe51")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def import_id(source_root: str, target: str, manifest_hash: str) -> str:
    """A stable id, including the content hash so changed input is never replayed."""
    return str(uuid.uuid5(_NAMESPACE, f"{os.path.realpath(source_root)}\0{target}\0{manifest_hash}"))


def _attachment(path: str, node_dir: Path, root: Path) -> dict[str, Any]:
    """Resolve one attachment ``target`` string to the file it names.

    ``target`` is whatever ``attach_sources`` (``task_attach.py``) wrote onto
    the node — ``os.path.relpath(dest_path, os.path.dirname(node.file))`` —
    i.e. relative to THAT NODE'S OWN FILE, not the project root: a top-level
    node's file lives in ``decisions/active/``, a child's in
    ``decisions/active/<parent>/``, so the same ``decisions/attachments/N/x``
    target is spelled ``../attachments/N/x`` one level down but
    ``../../attachments/N/x`` two levels down. Resolving against a single
    shared root (as this used to) finds nothing for most real attachments —
    only an absolute (``--link``) target or a URL happens to need no base at
    all, and both already skip this branch (see ``is_absolute()`` and
    ``build_manifest``'s own ``http(s)://`` filter). ``root`` still fixes the
    containment boundary (``decisions/attachments/`` of THIS project, never
    anywhere else), independent of where the href resolved from.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = node_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
        attachments = (root / "decisions" / "attachments").resolve(strict=True)
        resolved.relative_to(attachments)
    except (OSError, ValueError) as exc:
        raise ImportError(f"attachment is outside decisions/attachments or missing: {path}") from exc
    if not resolved.is_file():
        raise ImportError(f"attachment is not a file: {path}")
    data = resolved.read_bytes()
    return {"source": str(resolved.relative_to(root)), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def build_manifest(store: LocalStore, section: str) -> dict[str, Any]:
    """Create the exact stable payload sent to plan/apply, without a network call."""
    nodes = []
    attachments: dict[str, dict[str, Any]] = {}
    root = Path(store.layout.project_root)
    for node in store.all_nodes():
        node_dir = Path(os.path.dirname(node.file)) if node.file else root
        payload = task_json.to_json(node)
        payload.pop("file", None)  # workstation path is not import data
        for att in payload.get("attachments", []) + payload.get("body_attachments", []):
            target = att.get("target")
            if isinstance(target, str) and not target.startswith(("http://", "https://")):
                item = _attachment(target, node_dir, root)
                attachments[item["sha256"]] = item
        nodes.append({"source_node_id": node.id, "payload": payload, "sha256": digest(payload)})
    body = {"version": _VERSION, "section": section, "source_alias": f"local-{root.name}", "nodes": nodes, "attachments": sorted(attachments.values(), key=lambda a: a["sha256"])}
    body["manifest_hash"] = digest(body)
    return body


def receipt_path(layout, target_key: str, manifest_hash: str) -> Path:
    root_hash = hashlib.sha256(os.path.realpath(layout.project_root).encode()).hexdigest()
    target_hash = hashlib.sha256(target_key.encode()).hexdigest()
    return tokens.config_dir() / "imports" / root_hash / target_hash / f"{manifest_hash}.json"


def read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists(): return None
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc: raise ImportError(f"cannot read receipt {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != _VERSION: raise ImportError(f"invalid receipt {path}")
    return value


def write_receipt(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); os.chmod(path.parent, stat.S_IRWXU)
    write_file_atomic(str(path), json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _resume_attachment(ctx, source_root: str, item: dict[str, Any], state: dict[str, Any], save) -> str:
    key = item["sha256"]; entry = state.setdefault("uploads", {}).setdefault(key, {})
    file_path = Path(source_root) / item["source"]
    upload_id = entry.get("upload_id")
    if upload_id:
        opened = uploads.session_state(ctx.target, ctx.space, upload_id, token=ctx.token)
    else:
        opened = uploads.open_session(ctx.target, ctx.space, token=ctx.token, filename=file_path.name,
            content_type="application/octet-stream", total_size=item["size"])
        upload_id = opened["upload_id"]; entry["upload_id"] = upload_id; save()
    received = set(opened.get("received_chunks") or [])
    size, count = opened["chunk_size"], opened["chunk_count"]
    with file_path.open("rb") as fh:
        for index in range(count):
            if index in received: continue
            fh.seek(index * size); data = fh.read(min(size, item["size"] - index * size))
            uploads._put_chunk_with_retry(ctx.target, ctx.space, upload_id, index, data, token=ctx.token, put=None, retries=uploads.DEFAULT_CHUNK_RETRIES)
            entry["received_chunks"] = sorted(received | {index}); save()
    complete = uploads.complete_session(ctx.target, ctx.space, upload_id, token=ctx.token)
    file_id = complete["item"].get("id")
    if not file_id: raise ImportError(f"attachment completion did not return a file id: {key}")
    readback = uploads.verify_file_size(ctx.target, ctx.space, file_id, item["size"], token=ctx.token)
    if readback.get("sha256") not in (None, key): raise ImportError(f"attachment {file_id} hash readback differs; refusing apply")
    entry["file_id"] = file_id; save(); return file_id


def run(args) -> int:
    """Dispatches ``fm task import``'s three shapes: the default plan/
    apply flow (below), and the two post-readback, archive-side verbs
    this module shares with ``task_archive.py`` — ``--archive-local``
    (mark the source read-only, only after a passed receipt and a
    verified backup) and ``--rollback`` (undo the cloud import via the
    server, and only then lift the marker). The three never mix: each
    of ``--archive-local``/``--rollback`` takes a receipt PATH, not
    ``--section``, and needs no fresh plan/apply of its own."""
    archive_local = getattr(args, "archive_local", None)
    rollback = getattr(args, "rollback", None)
    if archive_local and rollback:
        raise ImportError("--archive-local and --rollback don't mix: pick one.")
    if archive_local:
        return _run_archive_local(args)
    if rollback:
        return _run_rollback(args)
    if not getattr(args, "section", None):
        raise ImportError("--section is required for a plan/apply import (not --archive-local/--rollback)")
    return _run_plan_apply(args)


def _run_plan_apply(args) -> int:
    layout = task_paths.find_layout(getattr(args, "dir", None))
    if not layout: raise ImportError("no decisions/ directory found")
    store = LocalStore(layout); ctx0 = context.resolve(args, for_write=bool(args.apply), need_space=True, need_token=True)
    manifest = build_manifest(store, args.section); target_key = f"{ctx0.target.api_base}|{ctx0.space}|{args.section}"
    path = receipt_path(layout, target_key, manifest["manifest_hash"])
    def locked():
        receipt = read_receipt(path) or {"version": _VERSION, "import_id": import_id(layout.project_root, target_key, manifest["manifest_hash"]), "manifest_hash": manifest["manifest_hash"], "uploads": {}}
        receipt["target_key"] = target_key  # so --archive-local/--rollback have a target without re-deriving ctx
        def save(): write_receipt(path, receipt)
        save()  # identity is durable before any network call
        request = {"import_id": receipt["import_id"], "manifest": manifest}
        plan = api.post(ctx0.target, f"/projects/{ctx0.space}/local-imports/plan", request, token=ctx0.token)
        if not isinstance(plan, dict) or not isinstance(plan.get("plan_token"), str): raise ImportError("plan endpoint returned no plan_token")
        receipt["plan"] = plan; save()
        if not args.apply:
            print(json.dumps({"dry_run": True, "import_id": receipt["import_id"], "plan": plan, "receipt": str(path)}, indent=2)); return 0
        if args.apply != plan["plan_token"]: raise ImportError("--apply must equal this fresh plan_token")
        files = {a["sha256"]: _resume_attachment(ctx0, layout.project_root, a, receipt, save) for a in manifest["attachments"]}
        applied = api.post(ctx0.target, f"/projects/{ctx0.space}/local-imports/apply", {**request, "plan_token": plan["plan_token"], "files": files}, token=ctx0.token)
        if not isinstance(applied, dict): raise ImportError("apply endpoint returned an invalid response")
        receipt["apply"] = applied; save()
        verified = api.get(ctx0.target, f"/projects/{ctx0.space}/local-imports/{receipt['import_id']}", token=ctx0.token)
        if not isinstance(verified, dict) or verified.get("manifest_hash") != manifest["manifest_hash"]: raise ImportError("receipt/readback mismatch; source remains writable")
        receipt["readback"] = verified; receipt["verification"] = "passed"; save()
        print(json.dumps({"dry_run": False, "import_id": receipt["import_id"], "receipt": str(path), "readback": verified}, indent=2)); return 0
    return with_file_lock(str(path) + ".lock", locked, stale_seconds=300, wait_seconds=30)


def _read_receipt_or_raise(receipt_path_arg: str) -> dict[str, Any]:
    receipt = read_receipt(Path(receipt_path_arg))
    if receipt is None:
        raise ImportError(f"no receipt at {receipt_path_arg}")
    return receipt


def _run_archive_local(args) -> int:
    """``fm task import --archive-local <receipt> --confirm <name>``: the
    one path that may write the archive marker (task_archive.py's own
    "the authority lives in the caller" note). Runs only after: the
    receipt at this path has a passed readback, ``--confirm`` matches
    this project's own display name (a safety check against archiving
    the wrong folder), and a fresh backup has been made AND verified."""
    from formist_cli import task_archive, task_project

    confirm = getattr(args, "confirm", None)
    if not confirm:
        raise ImportError("--archive-local needs --confirm <this project's display name>")
    layout = task_paths.find_layout(getattr(args, "dir", None))
    if not layout:
        raise ImportError("no decisions/ directory found")
    receipt = _read_receipt_or_raise(args.archive_local)
    if receipt.get("verification") != "passed":
        raise ImportError(
            "this receipt has not passed readback verification (see `fm task import --section ... --apply ...`); "
            "refusing to archive"
        )
    display_name = task_project.project_display_name(layout)
    if confirm != display_name:
        raise ImportError(
            f"--confirm {confirm!r} does not match this project's display name {display_name!r}; "
            "refusing to archive the wrong folder"
        )
    if task_archive.is_archived_read_only(layout):
        raise ImportError("this project is already archived read-only")
    backup = task_archive.make_backup(layout)
    task_archive.verify_backup(backup["backup_id"])  # belt-and-suspenders, right before writing the marker
    target = receipt.get("target_key") or ""
    marker = task_archive.write_archive_marker(
        layout, import_id=receipt["import_id"], target=target, backup_id=backup["backup_id"],
    )
    print(json.dumps({
        "archived": True, "import_id": receipt["import_id"], "backup_id": backup["backup_id"], "archive": marker,
    }, indent=2))
    return 0


def _run_rollback(args) -> int:
    """``fm task import --rollback <receipt> --confirm <import_id>``:
    calls the server's rollback endpoint (fixed contract, agreed with
    C1347's lane: ``POST /projects/{space}/local-imports/{import_id}/
    rollback`` -> ``{rolled_back, reason, task_ids}``). Only on
    ``rolled_back=true``, and only when this source is currently
    archived under this same ``import_id``, does it clear the marker —
    the response plus the matching local receipt is the proof
    "unarchive needs a receipt" asks for, not the operator's say-so."""
    from formist_cli import task_archive

    confirm = getattr(args, "confirm", None)
    if not confirm:
        raise ImportError("--rollback needs --confirm <import_id>")
    layout = task_paths.find_layout(getattr(args, "dir", None))
    if not layout:
        raise ImportError("no decisions/ directory found")
    receipt = _read_receipt_or_raise(args.rollback)
    import_id_ = receipt.get("import_id")
    if confirm != import_id_:
        raise ImportError(
            f"--confirm {confirm!r} does not match this receipt's import_id {import_id_!r}; "
            "refusing to roll back the wrong import"
        )
    ctx0 = context.resolve(args, for_write=True, need_space=True, need_token=True)
    response = api.post(ctx0.target, f"/projects/{ctx0.space}/local-imports/{import_id_}/rollback", {}, token=ctx0.token)
    if not isinstance(response, dict):
        raise ImportError("rollback endpoint returned an invalid response")
    rolled_back = bool(response.get("rolled_back"))
    reason = response.get("reason")
    task_ids = response.get("task_ids") or []
    receipt["rollback"] = response
    write_receipt(Path(args.rollback), receipt)
    cleared = False
    if rolled_back:
        archive = task_archive.read_archive(layout)
        if archive and archive.get("import_id") == import_id_ and archive.get("read_only"):
            task_archive.clear_archive_marker(layout, verified=True)
            cleared = True
    print(json.dumps({
        "rolled_back": rolled_back, "reason": reason, "task_ids": task_ids, "marker_cleared": cleared,
    }, indent=2))
    return 0
