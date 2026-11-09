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
        task_import._attachment(str(outside), root)


def test_attachment_manifest_records_the_read_bytes_not_a_claim(tmp_path):
    root = tmp_path / "project"; folder = root / "decisions" / "attachments"; folder.mkdir(parents=True)
    path = folder / "proof.txt"; path.write_bytes(b"received")
    item = task_import._attachment("decisions/attachments/proof.txt", root)
    assert item == {"source": "decisions/attachments/proof.txt", "size": 8, "sha256": hashlib.sha256(b"received").hexdigest()}
