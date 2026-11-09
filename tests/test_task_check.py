"""``fm task check`` — a port of noddit's ``cli/commands/check.ts``.

Most of what ``check`` flags is drift that the Store's own write paths
already refuse to create (a bad ``default``, a duplicate option key —
``LocalStore.create``/``ask`` raise before the file is ever written), so
these tests write raw ``.md``/``decs.json`` files directly into
``decisions/``, exactly as an older tool version, a hand edit, or file
corruption would — mirroring how ``check.ts`` itself is exercised, and
matching the live cross-tool comparison already done against the real
`nod` binary for every one of these problem shapes (see
``test_task_nod_interop.py::test_check_matches_nod_across_every_problem_shape``).
"""

from __future__ import annotations

import json
import os

import pytest

from formist_cli import task_check, task_paths, task_queue, task_store


@pytest.fixture
def layout(tmp_path):
    return task_paths.init_layout(str(tmp_path))


def _write(layout, rel: str, frontmatter: str, body: str = "\nBody.\n") -> None:
    path = os.path.join(layout.project_root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"---\n{frontmatter}\n---\n{body}")


def _reserve(layout, id_: int) -> None:
    os.makedirs(layout.ids_dir, exist_ok=True)
    open(os.path.join(layout.ids_dir, str(id_)), "w").close()


def _messages(problems, level=None):
    return [p.message for p in problems if level is None or p.level == level]


def test_clean_project_reports_no_problems(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: Clean\nauthor: agent · author\nstatus: in_progress\ncreated: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 1)
    problems, total = task_check.check_project(layout, "project")
    assert problems == []
    assert total == 1


def test_duplicate_id_is_an_error(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: First\nauthor: agent · author\nstatus: in_progress\ncreated: 2026-11-01T10:00:00+08:00")
    _write(layout, "decisions/active/1-again/1.md",
           "id: 1\ntitle: Second\nauthor: agent · author\nstatus: in_progress\ncreated: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert any(p.level == "error" and "also used by" in p.message for p in problems)


def test_id_mismatch_between_frontmatter_and_filename(layout):
    _write(layout, "decisions/active/3-mismatch.md",
           "id: 99\ntitle: Mismatched\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 3)
    problems, _ = task_check.check_project(layout, "project")
    assert "front matter says id 99, filename says 3" in _messages(problems, "error")


def test_title_too_long_is_a_warning(layout):
    long_title = "A" * 80
    _write(layout, "decisions/active/1.md",
           f"id: 1\ntitle: {long_title}\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    msg = next(m for m in _messages(problems, "warn") if "columns" in m)
    assert "fm task retitle 1" in msg


@pytest.mark.parametrize(("frontmatter", "expected"), [
    ("id: 1\ntitle: No author\nstatus: not_started\ncreated: 2026-11-01T10:00:00+08:00", "no author"),
    ("id: 1\ntitle: No created\nauthor: agent · author\nstatus: not_started", "no created timestamp"),
])
def test_missing_author_or_created_is_a_warning(layout, frontmatter, expected):
    _write(layout, "decisions/active/1.md", frontmatter)
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert any(expected in m for m in _messages(problems, "warn"))


def test_empty_body_is_a_warning(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: Empty\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00", body="\n")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert "empty body" in _messages(problems, "warn")


def test_duplicate_options_block_is_an_error():
    """The old `nod ask` bug: a second, empty-bodied copy of the same
    options block gets appended after the real one."""
    body = (
        "\n## 选项\n\n### 1 · A\n\nreal body\n\n## 选项\n\n### 1 · A\n\n"
    )
    dup = task_check.find_duplicate_options(body)
    assert dup is not None
    assert dup.to_line > dup.from_line


def test_repeated_option_key_is_an_error(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: Repeated key\nauthor: agent · author\nkind: decision\nstatus: needs_decision\n"
           "created: 2026-11-01T10:00:00+08:00",
           body="\n## 选项\n\n### 1 · A\n\nbody a\n\n### 1 · B\n\nbody b\n")
    _reserve(layout, 1)
    # Hand-edit a second "1 ·" heading in directly (the Store refuses this
    # at write time; a raw file is how this drift actually happens).
    path = os.path.join(layout.active_dir, "1.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read().replace("### 1 · B", "### 1 · A2")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    problems, _ = task_check.check_project(layout, "project")
    assert "option key repeated: 1" in _messages(problems, "error")


@pytest.mark.parametrize(("default_line", "expected_fragment"), [
    ("default: \"9\"\n", 'default is "9" but there are no options at all'),
])
def test_default_with_no_options_is_an_error(layout, default_line, expected_fragment):
    _write(layout, "decisions/active/1.md",
           f"id: 1\ntitle: Bad default\nauthor: agent · author\nkind: decision\nstatus: needs_decision\n"
           f"{default_line}created: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert expected_fragment in _messages(problems, "error")[0]


def test_default_not_one_of_the_options_is_an_error(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: Bad default\nauthor: agent · author\nkind: decision\nstatus: needs_decision\n"
           "default: \"9\"\ncreated: 2026-11-01T10:00:00+08:00",
           body="\n## 选项\n\n### 1 · A\n\nbody a\n")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert 'default is "9" but the options are 1' in _messages(problems, "error")[0]


def test_needs_decision_with_no_options_is_a_warning(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: No options at all\nauthor: agent · author\nkind: decision\n"
           "status: needs_decision\ncreated: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert "no options — nothing for the owner to press" in _messages(problems, "warn")


def test_archived_and_still_queued_is_a_warning(layout, monkeypatch):
    _write(layout, "decisions/archived/1.md",
           "id: 1\ntitle: Archived\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00\narchived_at: 2026-11-02T10:00:00+08:00")
    _reserve(layout, 1)
    task_queue.enqueue(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert "archived but still on the delivery queue" in _messages(problems, "warn")


def test_archived_decision_without_an_answer_is_a_warning(layout):
    _write(layout, "decisions/archived/1.md",
           "id: 1\ntitle: Archived no answer\nauthor: agent · author\nkind: decision\n"
           "status: needs_decision\ncreated: 2026-11-01T10:00:00+08:00\n"
           "archived_at: 2026-11-02T10:00:00+08:00",
           body="\n## 选项\n\n### 1 · A\n\n")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert "archived without an answer" in _messages(problems, "warn")


def test_archived_closed_without_an_answer_is_fine(layout):
    """A closed (not answered) archive is a normal, deliberate close —
    only the decision/acceptance-without-answer combination is wrong."""
    _write(layout, "decisions/archived/1.md",
           "id: 1\ntitle: Closed\nauthor: agent · author\nkind: task\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00\narchived_at: 2026-11-02T10:00:00+08:00\n"
           "close_reason: not needed")
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert not any("without an answer" in m for m in _messages(problems))


def test_approval_without_matching_attachment_is_a_warning(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: Orphan approval\nauthor: agent · author\nstatus: in_progress\n"
           "created: 2026-11-01T10:00:00+08:00\n"
           'approvals:\n  - target: missing.txt\n    hash: deadbeef\n    approve_on: ["1"]')
    _reserve(layout, 1)
    problems, _ = task_check.check_project(layout, "project")
    assert 'approval record for "missing.txt" has no matching attachment anymore' in _messages(problems, "warn")


def test_missing_id_reservation_is_a_warning(layout):
    _write(layout, "decisions/active/1.md",
           "id: 1\ntitle: No reservation\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00")
    # Deliberately NOT calling _reserve(layout, 1).
    problems, _ = task_check.check_project(layout, "project")
    assert "no id reservation — a new entry could reuse 1" in _messages(problems, "warn")


def test_dependency_cycle_is_an_error(layout):
    """Also exercises the ``after: [...]`` flow-sequence YAML fix: a bare
    block-style list would hide this cycle just as well, but the inline
    form is what a hand-edited file is likely to use, and a prior bug in
    ``task_yaml``'s subset parser silently misread it as a one-item list
    containing the literal string ``'["X2"]'`` (found via live comparison
    against real `nod check`, which has no trouble with it since it uses
    a full YAML parser)."""
    _write(layout, "decisions/active/1.md",
           'id: 1\ntitle: A\nauthor: agent · author\nstatus: not_started\n'
           'created: 2026-11-01T10:00:00+08:00\nafter: ["X2"]')
    _write(layout, "decisions/active/2.md",
           'id: 2\ntitle: B\nauthor: agent · author\nstatus: not_started\n'
           'created: 2026-11-01T10:00:00+08:00\nafter: ["X1"]')
    _reserve(layout, 1)
    _reserve(layout, 2)
    problems, _ = task_check.check_project(layout, "project")
    cycle = next(p for p in problems if p.file == "after:")
    assert cycle.level == "error"
    assert "X1 → X2 → X1" in cycle.message


def test_unparseable_file_is_reported_without_aborting_the_sweep(layout):
    """One bad file must not crash the whole check — matching
    ``check.ts``'s own per-file try/catch."""
    path = os.path.join(layout.active_dir, "1.md")
    os.makedirs(layout.active_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('---\ntitle: "unterminated\n---\nBody.\n')
    _write(layout, "decisions/active/2.md",
           "id: 2\ntitle: Fine\nauthor: agent · author\nstatus: not_started\n"
           "created: 2026-11-01T10:00:00+08:00")
    _reserve(layout, 2)
    problems, total = task_check.check_project(layout, "project")
    assert total == 1  # only the good file counted
    bad = next(p for p in problems if p.file.endswith("1.md"))
    assert bad.level == "error"


@pytest.mark.parametrize(("raw", "expected"), [
    ("not json at all", "not valid JSON"),
    (json.dumps(["a", "list", "not", "an", "object"]), "must be a JSON object"),
    (json.dumps({"letter": "ab"}), "letter must be one A-Z letter"),
    (json.dumps({"name": "   "}), "name must be a non-empty string"),
    (json.dumps({"former_letters": ["a", 1]}), "former_letters must be a list of A-Z letters"),
])
def test_decs_json_config_problems(layout, raw, expected):
    os.makedirs(layout.root, exist_ok=True)
    with open(os.path.join(layout.root, "decs.json"), "w", encoding="utf-8") as fh:
        fh.write(raw)
    problems, _ = task_check.check_project(layout, "project")
    assert any(expected in m for m in _messages(problems, "error"))


def test_store_check_matches_check_project(tmp_path):
    """``LocalStore.check()`` is a thin wrapper — it must thread the
    project's own letter through (see the C1140 find: ``letter()``
    used to default to ``None`` instead of ``"X"``, which silently broke
    every unlettered project's ``after``-cycle detection)."""
    layout = task_paths.init_layout(str(tmp_path))
    store = task_store.LocalStore(layout)
    a = store.create("A", "agent · author", task=True)
    b = store.create("B", "agent · author", task=True, after=[f"X{a.id}"])
    c = store.create("C", "agent · author", task=True, after=[f"X{b.id}"])
    # Close the loop by hand (the Store's own `after` refuses a cycle on
    # `move`, but `create`'s `after` is taken as given).
    path = os.path.join(layout.active_dir, task_paths.entry_file_name(a.id, "a"))
    with open(path, encoding="utf-8") as fh:
        text = fh.read().replace("---\n", f'---\nafter: ["X{c.id}"]\n', 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

    problems, total = store.check()
    assert total == 3
    assert any(p.file == "after:" and "cycle" in p.message for p in problems)
