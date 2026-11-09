"""Incrementally mirror one local ``decisions/`` project into an Evane Section."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from formist_cli import api, context, task_json, task_project, tokens
from formist_cli.task_lock import with_file_lock, write_file_atomic
from formist_cli.task_store import LocalStore


class MirrorError(Exception):
    pass


_CURSOR_VERSION = 1
_NON_PROJECTED = {
    "file",
    "project",
    "project_name",
    "status_label",
    "wait_due",
    "delay",
    "deferred_by_ai",
    "staleness",
    "reopened_after_archive",
    "children",
    "queued",
}


def _state_path(store: LocalStore) -> Path:
    root_key = hashlib.sha256(os.path.realpath(store.layout.project_root).encode()).hexdigest()
    return tokens.config_dir() / "mirrors" / f"{root_key}.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _CURSOR_VERSION, "source_key": str(uuid.uuid4()), "targets": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MirrorError(f"could not read mirror cursor {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("source_key"), str):
        raise MirrorError(f"could not read mirror cursor {path}: invalid shape")
    if raw.get("version") != _CURSOR_VERSION:
        raise MirrorError(f"mirror cursor {path} has unsupported version {raw.get('version')!r}")
    if not isinstance(raw.get("targets"), dict):
        raw["targets"] = {}
    return raw


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    write_file_atomic(path.as_posix(), json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _projection(node) -> dict[str, Any]:
    payload = task_json.to_json(node)
    projected = {key: value for key, value in payload.items() if key not in _NON_PROJECTED}
    projected["source_ref"] = payload.get("ref")
    return projected


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _target_key(ctx) -> str:
    return f"{ctx.target.api_base}|{ctx.space}"


def sync(args) -> dict[str, Any]:
    """Detect changed/deleted nodes, POST one batch, then advance the cursor."""
    from formist_cli import task_paths

    layout = task_paths.find_layout(getattr(args, "dir", None))
    if layout is None:
        raise MirrorError(
            "no decisions/ directory found here or in any parent.\n"
            "Run `fm task init` in the project root to create one."
        )
    store = LocalStore(layout)
    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)
    if not getattr(args, "section", None):
        raise MirrorError("--section is required (the mirror Section id or letter)")
    state_path = _state_path(store)

    def run() -> dict[str, Any]:
        snapshot_at = datetime.now(timezone.utc).isoformat()
        state = _read_state(state_path)
        # Persist the source identity before the network call. If the call lands
        # but the process dies before advancing hashes, the next replay uses the
        # same mirror and is idempotent instead of inventing a second source.
        _write_state(state_path, state)
        key = _target_key(ctx)
        target_cursor = state["targets"].get(key) or {}
        previous = target_cursor.get("nodes") if isinstance(target_cursor.get("nodes"), dict) else {}
        nodes = store.all_nodes()
        projected = {str(node.id): _projection(node) for node in nodes}
        hashes = {node_id: _digest(payload) for node_id, payload in projected.items()}
        changed_ids = sorted(
            (
                int(node_id)
                for node_id, digest in hashes.items()
                if getattr(args, "full", False) or previous.get(node_id) != digest
            )
        )
        tombstones = sorted(int(node_id) for node_id in set(previous).difference(hashes))
        request = {
            "section": args.section,
            "source_key": state["source_key"],
            "source_name": task_project.project_display_name(layout),
            "source_letter": task_project.project_letter(layout),
            "snapshot_at": snapshot_at,
            "nodes": [
                {
                    "source_node_id": node_id,
                    "payload": projected[str(node_id)],
                }
                for node_id in changed_ids
            ],
            "tombstones": tombstones,
        }
        response = api.post(
            ctx.target,
            f"/projects/{ctx.space}/local-mirrors/sync",
            request,
            token=ctx.token,
        )
        if not isinstance(response, dict) or not isinstance(response.get("last_synced_at"), str):
            raise MirrorError("the mirror endpoint returned an invalid response")
        state["targets"][key] = {
            "space_id": ctx.space,
            "section_id": response.get("section_id"),
            "last_synced_at": response["last_synced_at"],
            "nodes": hashes,
        }
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(state_path, state)
        return {
            **response,
            "changed": len(changed_ids),
            "deleted": len(tombstones),
            "cursor": state_path.as_posix(),
        }

    return with_file_lock(f"{state_path}.lock", run, stale_seconds=300.0, wait_seconds=30.0)
