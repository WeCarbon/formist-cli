"""``task_sessions.py``: a port of noddit's ``core/session-nodes.ts`` —
the local-mode "current node"/"remembered author" convenience. See the
module docstring for which two of noddit's three maps are ported, and
why ``session_key``'s harness-PID fallback tier is not.
"""

from __future__ import annotations

from formist_cli import task_sessions


def _env(**over):
    base = {}
    base.update(over)
    return base


# ===========================================================================
# session_key
# ===========================================================================
def test_session_key_none_with_no_identity_and_no_session(tmp_path):
    assert task_sessions.session_key(_env(), cwd=str(tmp_path)) is None


def test_session_key_uses_claude_code_session_id(tmp_path):
    key = task_sessions.session_key(_env(CLAUDE_CODE_SESSION_ID="sess-1"), cwd=str(tmp_path))
    assert key is not None and key.startswith("sess-1")


def test_session_key_explicit_identity_wins(tmp_path):
    key = task_sessions.session_key(
        _env(NODDIT_IDENTITY="pinned", CLAUDE_CODE_SESSION_ID="sess-1"), cwd=str(tmp_path),
    )
    assert key.startswith("pinned")


def test_session_key_is_scoped_by_an_explicit_worktree_env(tmp_path):
    key = task_sessions.session_key(
        _env(CLAUDE_CODE_SESSION_ID="sess-1", NODDIT_WORKTREE="/some/lane"), cwd=str(tmp_path),
    )
    assert key == "sess-1@/some/lane"


def test_session_key_differs_for_different_sessions(tmp_path):
    a = task_sessions.session_key(_env(CLAUDE_CODE_SESSION_ID="sess-a", NODDIT_WORKTREE="/w"), cwd=str(tmp_path))
    b = task_sessions.session_key(_env(CLAUDE_CODE_SESSION_ID="sess-b", NODDIT_WORKTREE="/w"), cwd=str(tmp_path))
    assert a != b


# ===========================================================================
# record_started / current_node
# ===========================================================================
def _home_env(tmp_path, **over):
    e = {"NODDIT_HOME": str(tmp_path / "home"), "CLAUDE_CODE_SESSION_ID": "sess-1", "NODDIT_WORKTREE": "/w"}
    e.update(over)
    return e


def test_current_node_is_none_before_anything_is_recorded(tmp_path):
    env = _home_env(tmp_path)
    assert task_sessions.current_node(env, cwd=str(tmp_path)) is None


def test_record_started_then_current_node_round_trips(tmp_path):
    env = _home_env(tmp_path)
    recorded = task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))
    assert recorded is True
    current = task_sessions.current_node(env, cwd=str(tmp_path))
    assert current == {"root": "/proj", "id": 5, "ref": "X5", "at": current["at"]}


def test_record_started_without_a_session_returns_false_and_records_nothing(tmp_path):
    env = _home_env(tmp_path, CLAUDE_CODE_SESSION_ID="")
    assert task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path)) is False
    assert task_sessions.current_node(env, cwd=str(tmp_path)) is None


def test_a_second_start_overwrites_the_current_node(tmp_path):
    env = _home_env(tmp_path)
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))
    task_sessions.record_started("/proj", 6, "X6", "agent · x", env=env, cwd=str(tmp_path))
    assert task_sessions.current_node(env, cwd=str(tmp_path))["ref"] == "X6"


def test_different_sessions_do_not_see_each_others_current_node(tmp_path):
    env_a = _home_env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-a")
    env_b = _home_env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-b")
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env_a, cwd=str(tmp_path))
    assert task_sessions.current_node(env_b, cwd=str(tmp_path)) is None


# ===========================================================================
# record_author / remembered_author
# ===========================================================================
def test_remembered_author_is_none_before_anything_is_recorded(tmp_path):
    env = _home_env(tmp_path)
    assert task_sessions.remembered_author(env, cwd=str(tmp_path)) is None


def test_record_author_then_remembered_author_round_trips(tmp_path):
    env = _home_env(tmp_path)
    assert task_sessions.record_author("agent · x", env=env, cwd=str(tmp_path)) is True
    assert task_sessions.remembered_author(env, cwd=str(tmp_path)) == "agent · x"


def test_record_started_also_remembers_the_author_when_given(tmp_path):
    env = _home_env(tmp_path)
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))
    assert task_sessions.remembered_author(env, cwd=str(tmp_path)) == "agent · x"


def test_record_started_with_no_author_leaves_remembered_author_alone(tmp_path):
    env = _home_env(tmp_path)
    task_sessions.record_author("agent · earlier", env=env, cwd=str(tmp_path))
    task_sessions.record_started("/proj", 5, "X5", None, env=env, cwd=str(tmp_path))
    assert task_sessions.remembered_author(env, cwd=str(tmp_path)) == "agent · earlier"


def test_remembered_author_without_a_session_is_none(tmp_path):
    env = _home_env(tmp_path, CLAUDE_CODE_SESSION_ID="")
    assert task_sessions.record_author("agent · x", env=env, cwd=str(tmp_path)) is False
    assert task_sessions.remembered_author(env, cwd=str(tmp_path)) is None


# ===========================================================================
# state file shape and pruning
# ===========================================================================
def test_state_file_holds_current_and_authors_as_plain_json(tmp_path):
    import json

    env = _home_env(tmp_path)
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))
    with open(task_sessions.state_path(env), encoding="utf-8") as fh:
        raw = json.load(fh)
    assert set(raw) == {"current", "authors"}
    assert len(raw["current"]) == 1
    assert len(raw["authors"]) == 1


def test_an_unwritable_home_is_a_silent_convenience_miss_not_a_crash(tmp_path):
    """Matches noddit's own try/catch around the whole update: refs/author
    just have to be spelled out, this must never be the reason a real
    write (the node itself) fails."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a plain file, not a directory")
    env = {"NODDIT_HOME": str(blocked / "home"), "CLAUDE_CODE_SESSION_ID": "sess-1", "NODDIT_WORKTREE": "/w"}
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))  # must not raise
    assert task_sessions.current_node(env, cwd=str(tmp_path)) is None


def test_old_entries_are_pruned_past_keep_seconds(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    env = _home_env(tmp_path)
    task_sessions.record_started("/proj", 5, "X5", "agent · x", env=env, cwd=str(tmp_path))
    path = task_sessions.state_path(env)
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    # Back-date the entry past KEEP_SECONDS, bypassing the module's own
    # clock rather than sleeping a real 14 days.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=task_sessions.KEEP_SECONDS + 10)).isoformat()
    key = next(iter(raw["current"]))
    raw["current"][key]["at"] = stale
    raw["authors"][key]["at"] = stale
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)

    # Any update triggers a prune pass (noddit's own design: prune on write)
    # — a DIFFERENT session's write, so this one doesn't just refresh the
    # very key being checked for staleness.
    other_env = _home_env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-2")
    task_sessions.record_author("agent · someone-else", env=other_env, cwd=str(tmp_path))
    with open(path, encoding="utf-8") as fh:
        pruned = json.load(fh)
    assert key not in pruned["current"]
    assert key not in pruned["authors"]
