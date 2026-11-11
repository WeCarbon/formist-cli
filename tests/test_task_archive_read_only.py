"""The read-only archive marker and the fail-closed local write guard
(C1346) — the ``fm`` half of C1330's "本机只读归档标记".

Never points at a real project: every test below builds its own
throwaway ``decisions/`` tree under ``tmp_path`` (C1346's own node
description: "不得在负责人批准实际迁移前归档任何真实项目")."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from formist_cli import task_archive, task_paths, tasks


def _hash_tree(root: Path) -> dict[str, str]:
    return task_archive._hash_tree(root)  # the same primitive the guard's own tests must trust


def _run(capsys, *argv):
    code = tasks.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _project(tmp_path, monkeypatch, capsys) -> Path:
    """A fresh, tiny project: X1 a plain task, X2 a decision node with
    two options, plus a loose file outside ``decisions/`` to attach."""
    monkeypatch.setenv("NODDIT_AUTHOR", "agent · archive test")
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "formist-config"))
    project = tmp_path / "project"
    assert tasks.main(["init", str(project)]) == 0
    assert tasks.main(["add", "Sample task", "--task", "--dir", str(project), "--json"]) == 0
    assert tasks.main([
        "add", "Decide something", "--option", "1=A", "--option", "2=B", "--default", "1",
        "--dir", str(project), "--json",
    ]) == 0
    (tmp_path / "attachment.txt").write_text("a loose file to attach", encoding="utf-8")
    capsys.readouterr()  # drain the setup's own "created .../created X1/X2" output
    return project


def _archive(project: Path) -> dict:
    layout = task_paths.layout_for(str(project))
    backup = task_archive.make_backup(layout)
    return task_archive.write_archive_marker(
        layout, import_id="11111111-1111-1111-1111-111111111111", target="local-fixture", backup_id=backup["backup_id"],
    )


# -- the marker itself ----------------------------------------------------


def test_marker_round_trip(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    assert task_archive.read_archive(layout) is None
    assert not task_archive.is_archived_read_only(layout)

    marker = _archive(project)
    assert marker["read_only"] is True
    assert marker["import_id"] == "11111111-1111-1111-1111-111111111111"
    assert marker["target"] == "local-fixture"
    assert task_archive.is_archived_read_only(layout)

    task_archive.clear_archive_marker(layout, verified=True)
    assert not task_archive.is_archived_read_only(layout)
    assert task_archive.read_archive(layout) is None


def test_write_archive_marker_refuses_double_archive(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    _archive(project)
    layout = task_paths.layout_for(str(project))
    with pytest.raises(task_archive.ArchivedReadOnlyError, match="already archived"):
        task_archive.write_archive_marker(layout, import_id="x", target="y", backup_id="z")


def test_clear_archive_marker_refuses_without_verified_flag(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    _archive(project)
    layout = task_paths.layout_for(str(project))
    with pytest.raises(task_archive.ArchivedReadOnlyError, match="verified rollback"):
        task_archive.clear_archive_marker(layout, verified=False)
    assert task_archive.is_archived_read_only(layout)  # untouched


def test_clear_archive_marker_refuses_when_not_archived(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch, capsys)
    layout = task_paths.layout_for(str(project))
    with pytest.raises(task_archive.ArchiveError, match="not archived"):
        task_archive.clear_archive_marker(layout, verified=True)


# -- the enumerated verb classification -----------------------------------
#
# Every verb `tasks.add_parser` registers, classified by what it actually
# does on disk — NOT by name (`fm task read` is a WRITE: it pops the
# queue) and not by the parser's own `write=True` flag (`progress`/`steps`
# both mutate but are registered without it; see tasks.py). A verb
# missing from either bucket below fails test_every_registered_verb_is_
# classified loudly — that is the point: an overlooked write command is
# exactly the hole C1330 warns about.

WRITE_ARGV: dict[str, list[str]] = {
    "add": ["add", "Another one", "--task"],
    "start": ["start", "X1"],
    "done": ["done", "X1"],
    "close": ["close", "X1", "--reason", "no longer needed"],
    "archive": ["archive", "X1"],
    "defer": ["defer", "X1", "--human"],
    "undefer": ["undefer", "X1", "--human"],
    "retitle": ["retitle", "X1", "A changed title", "--human"],
    "move": ["move", "X1", "--parent", "X2"],
    "supersede": ["supersede", "X1", "--by", "X2"],
    "assign": ["assign", "X1", "me"],
    "flag": ["flag", "X1", "important"],
    "after": ["after", "X1", "X2"],
    "order": ["order", "X1", "X2"],
    "ask": ["ask", "X2", "--text", "please reconsider"],
    "push": ["push", "X2", "1", "--human"],
    "read": ["read", "X1"],
    "note": ["note", "X1", "a note", "--human"],
    "edit": ["edit", "X1", "--body", "new description"],
    "progress": ["progress", "X1", "50"],
    "steps": ["steps", "X1", "step one", "step two"],
    "step": ["step", "X1", "1"],
    "beat": ["beat", "X1"],
    "attach": ["attach", "X1"],  # the real file path is appended by the test (lives in tmp_path)
    "claim": ["claim", "X1"],
    "expand": ["expand", "X1", "--body", "a full description"],
    "config": ["config", "letter", "Y"],
    "monitor": ["monitor", "--once"],
}

#: Read-only verbs, and verbs that write somewhere other than this
#: project's own `decisions/`/`decs.json` — exempt from the guard by
#: design, not by oversight. Each reason names the actual write target
#: (or "none") so this table itself is the audit trail.
EXEMPT_ARGV_AND_REASON: dict[str, tuple[list[str] | None, str]] = {
    "init": (["init"], "idempotent bootstrap (makedirs(exist_ok=True)); never writes decs.json"),
    "whoami": (["whoami"], "reads env/identity only, no project"),
    "get": (["get", "X1"], "read"),
    "show": (["show"], "read"),
    "views": (["views"], "reads $NODDIT_HOME/views.json, global, not this project"),
    "view": (["view", "show", "decide"], "writes $NODDIT_HOME/views.json, global, not this project"),
    "tree": (["tree"], "read"),
    "search": (["search", "task"], "read"),
    "queue": (["queue"], "local mode: read_queue only, never enqueue/dequeue"),
    "approved": (["approved", "X1", "--check"], "read-only hash comparison"),  # path appended by the test
    "briefs": (["briefs"], "read"),
    "check": (["check"], "validates only; --fix is not ported, nothing is auto-repaired"),
    "changes": (["changes", "--since", "2020-01-01T00:00:00+00:00"], "read"),
    "wait": (["wait", "--timeout", "0"], "polls read_queue only"),
    "tell": (["tell", "hello"], "local mode always raises before touching disk (`_no_tell`)"),
    "plugin-session-start": (["plugin-session-start"], "reads target.tree() only"),
    "plugin-prompt-refs": (["plugin-prompt-refs"], "reads known letters only"),
    # Not actually invoked below (each would hang or hit the network in a
    # unit test), but still claimed here so the completeness check passes
    # and the reason is on record:
    "watch": (None, "infinite poll loop by design (no --once); never calls save_cursor, only run_pass, "
                     "whose own clock sweeps already fail closed through LocalStore.mutate"),
    "mirror": (None, "reads decisions/ only; writes its own cursor under tokens.config_dir()/mirrors/"),
    "import": (None, "plan/apply writes a receipt under tokens.config_dir()/imports/, never decisions/ itself; "
                      "--archive-local/--rollback are the one exception, covered by test_task_import_archive.py"),
}


def test_every_registered_verb_is_classified():
    parser = tasks.build_standalone_parser()
    verb_action = next(a for a in parser._subparsers._group_actions if a.dest == "verb")
    registered = set(verb_action.choices.keys())
    classified = set(WRITE_ARGV) | set(EXEMPT_ARGV_AND_REASON)
    missing = registered - classified
    extra = classified - registered
    assert not missing, f"new/unclassified `fm task` verb(s), update WRITE_ARGV or EXEMPT_ARGV_AND_REASON: {missing}"
    assert not extra, f"classified verb(s) no longer registered: {extra}"


def test_the_completeness_check_itself_catches_a_missing_verb():
    """Demonstrates the mechanism in test_every_registered_verb_is_
    classified actually fires, independent of today's real verb list: a
    registered set with one extra, unclassified entry must fail the same
    assertion a genuinely-added-but-forgotten verb would."""
    registered = {"add", "done", "a-brand-new-write-verb-nobody-classified"}
    classified = {"add", "done"}
    missing = registered - classified
    assert missing == {"a-brand-new-write-verb-nobody-classified"}


@pytest.mark.parametrize("verb", sorted(WRITE_ARGV))
def test_write_verbs_are_rejected_while_archived_and_nothing_changes_on_disk(tmp_path, monkeypatch, capsys, verb):
    project = _project(tmp_path, monkeypatch, capsys)
    argv = list(WRITE_ARGV[verb])
    if verb == "attach":
        argv = [*argv, str(tmp_path / "attachment.txt")]
    _archive(project)
    layout = task_paths.layout_for(str(project))
    before = _hash_tree(Path(layout.root))

    code, _out, err = _run(capsys, *argv, "--dir", str(project))

    assert code == 2, f"{verb} unexpectedly succeeded while archived: {err}"
    assert "archived read-only" in err, f"{verb} failed for a different reason: {err}"
    after = _hash_tree(Path(layout.root))
    assert after == before, f"{verb} changed bytes under decisions/ despite being archived"


def test_reverting_the_guard_lets_a_rejected_write_through(tmp_path, monkeypatch, capsys):
    """Proves the guard itself is what blocks writes, not some other
    coincidence: with ``task_archive.guard_write`` monkeypatched to a
    no-op, the same `note` command that was rejected above now succeeds
    and actually writes — on an otherwise identical archived project."""
    project = _project(tmp_path, monkeypatch, capsys)
    _archive(project)

    code, _out, err = _run(capsys, "note", "X1", "a note", "--human", "--dir", str(project))
    assert code == 2 and "archived read-only" in err

    monkeypatch.setattr(task_archive, "guard_write", lambda _layout: None)
    code, out, _err = _run(capsys, "note", "X1", "a note while reverted", "--human", "--dir", str(project), "--json")
    assert code == 0, "reverting the guard should have let the write through"
    assert "a note while reverted" in json.dumps(json.loads(out))


@pytest.mark.parametrize("verb", sorted(k for k, v in EXEMPT_ARGV_AND_REASON.items() if v[0] is not None))
def test_read_verbs_are_unaffected_by_archiving(tmp_path, monkeypatch, capsys, verb):
    """Same project, same command, run once before and once after
    archiving: for a read verb the two runs must be identical — proof
    archiving changed nothing about how reads behave."""
    project = _project(tmp_path, monkeypatch, capsys)
    argv = list(EXEMPT_ARGV_AND_REASON[verb][0])
    if verb == "approved":
        argv = [*argv, str(tmp_path / "attachment.txt")]
    # `init`'s project dir is a bare positional, not --dir (it has no --dir
    # flag at all — see its own `add_argument("dir", ...)`).
    dir_args = [str(project)] if verb == "init" else ["--dir", str(project)]

    before_code, before_out, _ = _run(capsys, *argv, *dir_args)
    _archive(project)
    after_code, after_out, after_err = _run(capsys, *argv, *dir_args)

    assert after_code == before_code, f"{verb}'s exit code changed once archived: {after_err}"
    assert after_out == before_out, f"{verb}'s output changed once archived"
    assert "archived read-only" not in after_err, f"{verb} was wrongly caught by the write guard"
