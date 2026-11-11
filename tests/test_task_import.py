"""The local-import contract is fixed here against a fake server boundary."""
from __future__ import annotations

import hashlib

import pytest

from formist_cli import task_import


def test_identical_input_has_a_stable_import_id():
    first = task_import.import_id("/tmp/source", "https://example.test|space|S", "a" * 64)
    assert first == task_import.import_id("/tmp/source", "https://example.test|space|S", "a" * 64)
    assert first != task_import.import_id("/tmp/source", "https://example.test|space|S", "b" * 64)


def test_attachment_outside_project_is_a_blocking_collection_failure(tmp_path):
    root = tmp_path / "project"; (root / "decisions" / "attachments").mkdir(parents=True)
    outside = tmp_path / "secret.txt"; outside.write_text("no")
    with pytest.raises(task_import.ImportError, match="outside"):
        task_import._attachment(str(outside), root, root)


def test_attachment_manifest_records_the_read_bytes_not_a_claim(tmp_path):
    root = tmp_path / "project"; folder = root / "decisions" / "attachments"; folder.mkdir(parents=True)
    path = folder / "proof.txt"; path.write_bytes(b"received")
    item = task_import._attachment("decisions/attachments/proof.txt", root, root)
    assert item == {"source": "decisions/attachments/proof.txt", "size": 8, "sha256": hashlib.sha256(b"received").hexdigest()}


def test_attachment_target_resolves_against_the_nodes_own_file_not_project_root(tmp_path):
    """C1348 rehearsal finding: ``attach_sources`` (``task_attach.py``) always
    writes ``target`` relative to ``dirname(node.file)`` — never to the
    project root — because that is where the ``## 附件`` markdown link in the
    node's own file has to point for the link to resolve when someone opens
    it. A node two levels deep (``decisions/active/<parent>/<child>.md``)
    therefore spells the exact same file ``../../attachments/2/x``, not
    ``../attachments/2/x``. The OLD ``_attachment(path, root)`` joined every
    target against the single project root regardless of nesting, so it
    could only ever happen to work by coincidence (never, for a real one).
    """
    root = tmp_path / "project"
    node_dir = root / "decisions" / "active" / "1-parent" / "2-child"
    node_dir.mkdir(parents=True)
    attachments_dir = root / "decisions" / "attachments" / "2"
    attachments_dir.mkdir(parents=True)
    (attachments_dir / "proof.txt").write_bytes(b"received")

    # The real href `attach_sources` would have written for this file, two
    # directories below `decisions/active/`.
    target = "../../../attachments/2/proof.txt"

    item = task_import._attachment(target, node_dir, root)
    assert item == {
        "source": "decisions/attachments/2/proof.txt",
        "size": 8,
        "sha256": hashlib.sha256(b"received").hexdigest(),
    }


def test_build_manifest_collects_a_real_attachment_from_a_nested_node(tmp_path):
    """The actual regression, end to end through ``build_manifest`` and a
    real ``LocalStore``/``store.attach`` (the same write path ``fm task
    attach``/``nod attach`` use) — not the isolated ``_attachment`` unit
    above. A CHILD node's file lives one directory deeper than a root
    node's, so this is also the case that most needed a child, not a
    top-level node, to be caught at all. Without the fix, this raised
    "attachment is outside decisions/attachments or missing" for an
    attachment that is, in fact, both inside the project and present —
    found by C1348's rehearsal running the real `fm task import` CLI
    against a real `nod`-built project, not by this test (which is the
    capture of that finding, per GATE.md §5)."""
    from formist_cli import task_paths
    from formist_cli.task_store import LocalStore

    root = tmp_path / "project"
    (root / "decisions").mkdir(parents=True)
    layout = task_paths.layout_for(str(root))
    store = LocalStore(layout)
    parent = store.create("parent", "t", task=True)
    child = store.create("child", "t", task=True, parent=str(parent.id))

    src = tmp_path / "source.txt"
    src.write_bytes(b"attachment bytes")
    store.attach(child.id, [str(src)], "t")

    manifest = task_import.build_manifest(store, "C")
    assert len(manifest["attachments"]) == 1
    assert manifest["attachments"][0]["sha256"] == hashlib.sha256(b"attachment bytes").hexdigest()
    assert manifest["attachments"][0]["source"].startswith("decisions/attachments/")
