"""``fm task import --archive-local``/``--rollback`` (C1346), and the
backup/restore primitives they're built on. The server side is faked
the same way ``test_task_mirror.py`` fakes it (``task_import.api.post``/
``.get`` monkeypatched); none of this needs C1347's real endpoints or a
live server to prove the CLI contract.

Never points at a real project: every test builds its own throwaway
``decisions/`` tree under ``tmp_path``."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from formist_cli import task_archive, task_paths, task_import, tasks
from formist_cli.targets import Target


def _run(capsys, *argv):
    code = tasks.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _project(tmp_path, monkeypatch, capsys) -> Path:
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · import-archive test")
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "formist-config"))
    project = tmp_path / "project"
    assert tasks.main(["init", str(project)]) == 0
    assert tasks.main(["add", "Sample task", "--task", "--dir", str(project), "--json"]) == 0
    assert tasks.main(["config", "name", "Archive Fixture Project", "--dir", str(project)]) == 0
    capsys.readouterr()
    return project


def _fake_ctx():
    return SimpleNamespace(
        target=Target(env="local", api_base="http://127.0.0.1:9999", name="local"),
        space="11111111-1111-1111-1111-111111111111",
        token="test-token",
    )


def _fake_plan_apply_server(monkeypatch):
    """A minimal fake for `/local-imports/plan`, `/apply`, and the GET
    readback — just enough for ``task_import.run`` to reach
    ``verification: "passed"``, same shape as the real C1347 contract's
    plan/apply/readback trio (task_import.py's own module docstring)."""
    state: dict = {}

    def post(_target, path, body, *, token):
        if path.endswith("/local-imports/plan"):
            return {"plan_token": "plan-token-1", "blockers": [], "counts": {}}
        if path.endswith("/local-imports/apply"):
            state["manifest_hash"] = body["manifest"]["manifest_hash"]
            state["import_id"] = body["import_id"]
            return {"task_ids": [], "import_id": body["import_id"]}
        raise AssertionError(f"unexpected POST {path}")

    def get(_target, path, *, token):
        assert path.endswith(f"/local-imports/{state['import_id']}")
        return {"manifest_hash": state["manifest_hash"], "import_id": state["import_id"]}

    monkeypatch.setattr(task_import.context, "resolve", lambda *_a, **_k: _fake_ctx())
    monkeypatch.setattr(task_import.api, "post", post)
    monkeypatch.setattr(task_import.api, "get", get)
    return state


def _passed_receipt_path(project: Path, monkeypatch, capsys) -> Path:
    """Drives the real plan -> apply -> readback flow (against the fake
    server above) to produce a genuine ``verification: "passed"``
    receipt, then returns its path — exactly what a real `fm task
    import --section ... --apply <token>` run leaves behind."""
    import argparse

    _fake_plan_apply_server(monkeypatch)
    plan_args = argparse.Namespace(dir=str(project), section="C", apply=None, archive_local=None, rollback=None, confirm=None)
    assert task_import.run(plan_args) == 0

    layout = task_paths.find_layout(str(project))
    store_manifest = task_import.build_manifest(task_import.LocalStore(layout), "C")
    target_key = f"http://127.0.0.1:9999|{_fake_ctx().space}|C"
    path = task_import.receipt_path(layout, target_key, store_manifest["manifest_hash"])
    receipt = task_import.read_receipt(path)
    assert receipt is not None and "plan" in receipt

    apply_args = argparse.Namespace(
        dir=str(project), section="C", apply=receipt["plan"]["plan_token"],
        archive_local=None, rollback=None, confirm=None,
    )
    assert task_import.run(apply_args) == 0
    receipt = task_import.read_receipt(path)
    assert receipt["verification"] == "passed"
    capsys.readouterr()  # drain the plan/apply dry-run JSON this helper printed
    return path


# -- backup / restore -------------------------------------------------------


def test_backup_round_trip_verifies_and_restores_byte_for_byte(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))

    backup = task_archive.make_backup(layout)
    verified = task_archive.verify_backup(backup["backup_id"])
    assert verified["manifest"] == backup["manifest"]
    assert backup["file_count"] > 0

    restore_target = tmp_path / "restored" / "decisions"
    result = task_archive.restore_backup(backup["backup_id"], restore_target)
    assert result["diverged"] is False
    assert result["diff"] == []
    restored_hashes = task_archive._hash_tree(restore_target)
    assert restored_hashes == backup["manifest"]


def test_corrupted_backup_fails_verification_and_refuses_restore(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    backup = task_archive.make_backup(layout)

    corrupted = next(iter(backup["manifest"]))
    (task_archive._backup_dir(backup["backup_id"]) / "files" / corrupted).write_text("tampered", encoding="utf-8")

    with pytest.raises(task_archive.BackupVerificationError):
        task_archive.verify_backup(backup["backup_id"])
    with pytest.raises(task_archive.BackupVerificationError):
        task_archive.restore_backup(backup["backup_id"], tmp_path / "wherever")


def test_restore_never_clobbers_a_diverged_target(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    backup = task_archive.make_backup(layout)

    # The live project moves on after the backup was taken.
    assert tasks.main(["retitle", "X1", "Changed after the backup", "--human", "--dir", str(project)]) == 0
    capsys.readouterr()
    live_before = task_archive._hash_tree(Path(layout.root))

    result = task_archive.restore_backup(backup["backup_id"], Path(layout.root))

    assert result["diverged"] is True
    assert result["diff"]
    assert result["restored_to"] != str(layout.root)
    # The live, diverged directory must be untouched.
    assert task_archive._hash_tree(Path(layout.root)) == live_before
    restored_hashes = task_archive._hash_tree(Path(result["restored_to"]))
    assert restored_hashes == backup["manifest"]


# -- --archive-local ---------------------------------------------------------


def test_archive_local_refuses_without_a_passed_receipt(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    receipt_path = tmp_path / "unverified-receipt.json"
    task_import.write_receipt(receipt_path, {
        "version": 1, "import_id": "aaaaaaaa-0000-0000-0000-000000000000", "manifest_hash": "x", "uploads": {},
    })

    code, _out, err = _run(
        capsys, "import", "--archive-local", str(receipt_path), "--confirm", "Archive Fixture Project",
        "--dir", str(project),
    )
    assert code == 2
    assert "has not passed readback verification" in err
    assert not task_archive.is_archived_read_only(layout)
    assert not task_archive.backup_root().exists() or not list(task_archive.backup_root().iterdir())


def test_archive_local_refuses_on_confirm_mismatch(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    receipt_path = _passed_receipt_path(project, monkeypatch, capsys)

    code, _out, err = _run(
        capsys, "import", "--archive-local", str(receipt_path), "--confirm", "The Wrong Name",
        "--dir", str(project),
    )
    assert code == 2
    assert "does not match this project's display name" in err
    assert not task_archive.is_archived_read_only(layout)


def test_archive_local_backs_up_and_marks_read_only_on_success(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    receipt_path = _passed_receipt_path(project, monkeypatch, capsys)
    receipt = task_import.read_receipt(receipt_path)

    code, out, _err = _run(
        capsys, "import", "--archive-local", str(receipt_path), "--confirm", "Archive Fixture Project",
        "--dir", str(project),
    )
    assert code == 0
    result = json.loads(out)
    assert result["archived"] is True
    assert result["import_id"] == receipt["import_id"]

    assert task_archive.is_archived_read_only(layout)
    marker = task_archive.read_archive(layout)
    assert marker["import_id"] == receipt["import_id"]
    assert marker["backup_id"] == result["backup_id"]
    task_archive.verify_backup(marker["backup_id"])  # the backup this run made really is usable


def test_archive_local_refuses_when_already_archived(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    receipt_path = _passed_receipt_path(project, monkeypatch, capsys)
    assert _run(capsys, "import", "--archive-local", str(receipt_path), "--confirm", "Archive Fixture Project",
                "--dir", str(project))[0] == 0

    code, _out, err = _run(
        capsys, "import", "--archive-local", str(receipt_path), "--confirm", "Archive Fixture Project",
        "--dir", str(project),
    )
    assert code == 2
    assert "already archived" in err


# -- --rollback ---------------------------------------------------------


def _write_minimal_receipt(path: Path, import_id: str) -> None:
    task_import.write_receipt(path, {
        "version": 1, "import_id": import_id, "manifest_hash": "irrelevant-for-rollback", "uploads": {},
    })


def _fake_rollback_server(monkeypatch, response: dict):
    calls = []

    def post(_target, path, body, *, token):
        calls.append(path)
        assert path.endswith("/rollback")
        return dict(response)

    monkeypatch.setattr(task_import.context, "resolve", lambda *_a, **_k: _fake_ctx())
    monkeypatch.setattr(task_import.api, "post", post)
    return calls


def test_rollback_true_clears_the_marker_for_the_matching_import(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    import_id_ = "bbbbbbbb-0000-0000-0000-000000000000"
    backup = task_archive.make_backup(layout)
    task_archive.write_archive_marker(layout, import_id=import_id_, target="t", backup_id=backup["backup_id"])

    receipt_path = tmp_path / "rollback-receipt.json"
    _write_minimal_receipt(receipt_path, import_id_)
    _fake_rollback_server(monkeypatch, {"rolled_back": True, "reason": None, "task_ids": ["T1", "T2"]})

    code, out, _err = _run(
        capsys, "import", "--rollback", str(receipt_path), "--confirm", import_id_, "--dir", str(project),
    )
    assert code == 0
    result = json.loads(out)
    assert result == {"rolled_back": True, "reason": None, "task_ids": ["T1", "T2"], "marker_cleared": True}
    assert not task_archive.is_archived_read_only(layout)


def test_rollback_false_leaves_the_marker_untouched_and_reports_the_reason(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    import_id_ = "cccccccc-0000-0000-0000-000000000000"
    backup = task_archive.make_backup(layout)
    task_archive.write_archive_marker(layout, import_id=import_id_, target="t", backup_id=backup["backup_id"])

    receipt_path = tmp_path / "rollback-receipt.json"
    _write_minimal_receipt(receipt_path, import_id_)
    _fake_rollback_server(monkeypatch, {
        "rolled_back": False, "reason": "a task was edited in the cloud since the import", "task_ids": [],
    })

    code, out, _err = _run(
        capsys, "import", "--rollback", str(receipt_path), "--confirm", import_id_, "--dir", str(project),
    )
    assert code == 0
    result = json.loads(out)
    assert result["rolled_back"] is False
    assert result["reason"] == "a task was edited in the cloud since the import"
    assert result["marker_cleared"] is False
    assert task_archive.is_archived_read_only(layout)  # untouched


def test_rollback_for_a_different_import_id_does_not_clear_this_archive(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    archived_under = "dddddddd-0000-0000-0000-000000000000"
    backup = task_archive.make_backup(layout)
    task_archive.write_archive_marker(layout, import_id=archived_under, target="t", backup_id=backup["backup_id"])

    other_import_id = "eeeeeeee-0000-0000-0000-000000000000"
    receipt_path = tmp_path / "rollback-receipt.json"
    _write_minimal_receipt(receipt_path, other_import_id)
    _fake_rollback_server(monkeypatch, {"rolled_back": True, "reason": None, "task_ids": []})

    code, out, _err = _run(
        capsys, "import", "--rollback", str(receipt_path), "--confirm", other_import_id, "--dir", str(project),
    )
    assert code == 0
    assert json.loads(out)["marker_cleared"] is False
    marker = task_archive.read_archive(layout)
    assert marker["import_id"] == archived_under  # this project's own archive is untouched


def test_rollback_requires_confirm_to_match_the_receipts_import_id(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    receipt_path = tmp_path / "rollback-receipt.json"
    _write_minimal_receipt(receipt_path, "ffffffff-0000-0000-0000-000000000000")
    calls = _fake_rollback_server(monkeypatch, {"rolled_back": True, "reason": None, "task_ids": []})

    code, _out, err = _run(
        capsys, "import", "--rollback", str(receipt_path), "--confirm", "not-the-import-id", "--dir", str(project),
    )
    assert code == 2
    assert "does not match this receipt's import_id" in err
    assert calls == []  # never even called the server
