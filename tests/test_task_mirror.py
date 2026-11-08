"""Incremental fm local-mirror cursor and deletion propagation (C1296)."""

import argparse
import json
import shutil
from types import SimpleNamespace

from formist_cli import task_mirror, task_paths
from formist_cli.targets import Target
from formist_cli.task_store import LocalStore


def _project(tmp_path):
    source = __import__("pathlib").Path(__file__).parent / "fixtures" / "task_decisions"
    root = tmp_path / "project"
    shutil.copytree(source, root / "decisions")
    (root / "decisions" / "decs.json").write_text(
        json.dumps({"letter": "C", "name": "Mirror fixture"}), encoding="utf-8"
    )
    return root


def test_only_changed_nodes_are_sent_and_a_missing_file_becomes_a_tombstone(tmp_path, monkeypatch):
    """Finding shape 1: one local edit must replace the matching cloud projection."""
    project = _project(tmp_path)
    config = tmp_path / "config"
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(config))
    ctx = SimpleNamespace(
        target=Target(env="local", api_base="http://127.0.0.1:8160", name="local"),
        space="0f24567d-f3cd-46ca-b3f1-08f901a7436e",
        token="user-token",
    )
    monkeypatch.setattr(task_mirror.context, "resolve", lambda *_args, **_kwargs: ctx)
    calls = []
    remote = {}

    def post(_target, path, body, *, token):
        assert path.endswith("/local-mirrors/sync")
        assert token == "user-token"
        calls.append(body)
        for node in body["nodes"]:
            remote[node["source_node_id"]] = node["payload"]
        for node_id in body["tombstones"]:
            remote.pop(node_id, None)
        return {
            "mirror_id": "9c08aa8b-51d4-409b-a5f9-5762773014a2",
            "section_id": "efbb8af6-5a0a-4527-a601-9e3df0c82606",
            "source_name": body["source_name"],
            "last_synced_at": f"2026-11-09T01:00:0{len(calls)}+00:00",
            "created": len(body["nodes"]),
            "updated": 0,
            "tombstoned": len(body["tombstones"]),
            "task_ids": [],
        }

    monkeypatch.setattr(task_mirror.api, "post", post)
    args = argparse.Namespace(dir=str(project), section="C", full=False, host="local", space=ctx.space)

    first = task_mirror.sync(args)
    assert first["changed"] == 10
    assert first["deleted"] == 0
    assert len(remote) == 10

    second = task_mirror.sync(args)
    assert second["changed"] == 0
    assert second["deleted"] == 0
    assert calls[-1]["nodes"] == []

    store = LocalStore(task_paths.layout_for(str(project)))
    store.retitle(1, "Changed on this laptop", by="test-agent", role="agent")
    node_two = next(path for path in (project / "decisions" / "active").glob("2-*.md"))
    node_two.unlink()
    third = task_mirror.sync(args)

    assert third["changed"] == 1
    assert third["deleted"] == 1
    assert [node["source_node_id"] for node in calls[-1]["nodes"]] == [1]
    assert calls[-1]["tombstones"] == [2]
    assert remote[1]["title"] == "Changed on this laptop"
    assert 2 not in remote
    cursor = json.loads(__import__("pathlib").Path(third["cursor"]).read_text(encoding="utf-8"))
    target_cursor = next(iter(cursor["targets"].values()))
    assert target_cursor["last_synced_at"] == third["last_synced_at"]
