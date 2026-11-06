"""Cross-tool interop: the real `nod` CLI and `fm task` operate on the SAME
``decisions/`` directory and read each other's writes.

This is the live half of the C1052 brief's "shared-fixture parity test
proving that `nod` and `fm task` produce identical files in local mode" —
``test_task_parse_serialize.py`` covers the static, always-on half with
fixtures frozen into the repo; this file additionally drives the real
`nod` binary when it's on PATH, which needs Node.js and a checkout of
``decisions-md`` (https://github.com/wecarbon/noddit) and so is skipped
everywhere else, including CI, rather than becoming a hard dependency of
this public repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from formist_cli import cli, task_parse

NOD = shutil.which("nod")
pytestmark = pytest.mark.skipif(NOD is None, reason="the real `nod` CLI is not on PATH")


def _nod(*args, cwd=None, env=None):
    result = subprocess.run(
        [NOD, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"nod {args} failed: {result.stderr}"
    return result.stdout


def _fm(capsys, *args):
    code = cli.main(["task", *args])
    out = capsys.readouterr()
    assert code == 0, f"fm task {args} failed: {out.err}"
    return out.out


@pytest.fixture
def interop_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · interop test")
    env = {**os.environ, "NODDIT_AUTHOR": "agent · interop test"}
    _nod("init", "--dir", str(tmp_path), env=env)
    return tmp_path, env


def test_nod_creates_fm_mutates_nod_reads_back(interop_env, capsys):
    tmp_path, env = interop_env
    created = json.loads(_nod("add", "Decision from nod", "--dir", str(tmp_path), "-o", "1=A", "-o", "2=B",
                               "--default", "1", "--json", env=env))
    ref = created["ref"]

    _fm(capsys, "push", ref, "1", "--note", "going with A", "--human", "--dir", str(tmp_path))

    nod_view = json.loads(_nod("get", ref, "--dir", str(tmp_path), "--json", env=env))
    assert nod_view["status"] == "decided"
    assert nod_view["answer"]["choice"] == "1"
    assert nod_view["answer"]["by"] == "owner"


def test_fm_creates_nod_reads_and_tree_agrees(interop_env, capsys):
    tmp_path, env = interop_env
    _fm(capsys, "add", "A plain task", "--task", "--dir", str(tmp_path))

    nod_tree = _nod("tree", "--dir", str(tmp_path), "--all", env=env)
    assert "A plain task" in nod_tree

    out = _fm(capsys, "get", "X1", "--dir", str(tmp_path), "--json")
    assert json.loads(out)["title"] == "A plain task"


def test_fm_mutation_is_byte_identical_to_what_nod_would_have_written(interop_env, capsys):
    """The actual parity check: `fm task done` on a node `nod` created
    produces the exact same file `nod done` would — modulo the
    always-different `updated_at`/`archived_at` timestamps."""
    tmp_path, env = interop_env
    created = json.loads(_nod("add", "Task to finish", "--dir", str(tmp_path), "--task", "--json", env=env))
    ref = created["ref"]

    _fm(capsys, "done", ref, "--note", "all done", "--dir", str(tmp_path))
    fm_path = tmp_path / "decisions" / "archived" / f"{created['id']}-task-to-finish.md"
    assert fm_path.is_file()
    fm_text = fm_path.read_text(encoding="utf-8")

    # Re-create the same starting node under `nod` alone and let `nod` do
    # the `done`, in a second throwaway project, to get its own version of
    # the same operation to diff against.
    import tempfile

    with tempfile.TemporaryDirectory() as other:
        _nod("init", "--dir", other, env=env)
        other_created = json.loads(_nod("add", "Task to finish", "--dir", other, "--task", "--json", env=env))
        _nod("done", str(other_created["id"]), "--note", "all done", "--dir", other, env=env)
        nod_path = f"{other}/decisions/archived/{other_created['id']}-task-to-finish.md"
        nod_text = open(nod_path, encoding="utf-8").read()

    def _strip_timestamps(text: str) -> str:
        import re

        text = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+\-]+", "<TS>", text)
        # The human-readable heading form: "2026-11-06 18:53:51".
        return re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<TS>", text)

    assert _strip_timestamps(fm_text) == _strip_timestamps(nod_text)


def test_move_relink_matches_nod(interop_env, capsys):
    tmp_path, env = interop_env
    _nod("add", "Root one", "--dir", str(tmp_path), "--task", "--json", env=env)
    _nod("add", "Child", "--dir", str(tmp_path), "--task", "--parent", "1", "--json", env=env)
    sample = tmp_path / "sample.txt"
    sample.write_text("hi")
    _nod("attach", "2", str(sample), "--dir", str(tmp_path), "--json", env=env)

    _fm(capsys, "move", "X2", "--root", "--dir", str(tmp_path))
    text = (tmp_path / "decisions" / "active" / "2-child.md").read_text()
    assert "](../attachments/2/sample.txt)" in text


def test_order_matches_nod(interop_env, capsys):
    tmp_path, env = interop_env
    _nod("add", "A", "--dir", str(tmp_path), "--task", "--json", env=env)
    _nod("add", "B", "--dir", str(tmp_path), "--task", "--json", env=env)
    _fm(capsys, "order", "X2", "X1", "--dir", str(tmp_path))
    nod_json = json.loads(_nod("get", "1", "--dir", str(tmp_path), "--json", env=env))
    assert nod_json["order"] == -1


def test_round_trip_every_fixture_through_both_tools(interop_env):
    """Belt and suspenders: every fixture file this repo ships round-trips
    through a real `nod get --json` the same way it round-trips through
    `task_parse`/`task_serialize` (test_task_parse_serialize.py)."""
    import pathlib

    tmp_path, env = interop_env
    fixtures = pathlib.Path(__file__).parent / "fixtures" / "task_decisions"
    for sub in ("active", "archived"):
        dest = tmp_path / "decisions" / sub
        dest.mkdir(parents=True, exist_ok=True)
        for f in (fixtures / sub).glob("*.md"):
            shutil.copy(f, dest / f.name)

    for f in (fixtures / "active").glob("*.md"):
        node_id = f.name.split("-", 1)[0]
        nod_json = json.loads(_nod("get", node_id, "--dir", str(tmp_path), "--json", env=env))
        text = f.read_text(encoding="utf-8")
        from formist_cli import task_json

        mine = task_json.to_json(task_parse.parse_decision(text, str(f), False, project="task_decisions"))
        for key in ("title", "author", "status", "kind", "tags", "options"):
            assert mine[key] == nod_json[key], f"{f.name}: {key} mismatch"
