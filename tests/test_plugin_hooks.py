"""``plugin_hooks.py``: the testable half of `fm`'s Claude Code plugin
hooks (C1053/C1139) — a port of noddit's ``plugin/session-start.mjs`` and
``plugin/prompt-refs.mjs``. The thin subcommands that actually read
stdin/print to stdout (``_cmd_plugin_session_start``/
``_cmd_plugin_prompt_refs`` in ``tasks.py``) are covered end to end in
``test_task_cli.py``; this file drives the pure functions directly.
"""

from __future__ import annotations

import json
import os

from formist_cli import plugin_hooks


# ===========================================================================
# detect_refs / known_letters_for
# ===========================================================================
def test_detect_refs_accepts_multi_digit_under_any_letter():
    assert plugin_hooks.detect_refs("please check C1052 today", "XD") == ["C1052"]


def test_detect_refs_rejects_a_single_digit_with_an_unknown_letter():
    """Q1/F1 are common prose ("Q1 revenue"), not refs, unless the
    project's own letter happens to be Q or F."""
    assert plugin_hooks.detect_refs("Q1 numbers look fine", "XD") == []


def test_detect_refs_accepts_a_single_digit_when_the_letter_is_known():
    assert plugin_hooks.detect_refs("see W5 for context", "XDW") == ["W5"]


def test_detect_refs_rejects_letters_or_digits_touching_the_match():
    assert plugin_hooks.detect_refs("ABC1052 and C1052x and C1052", "XD") == ["C1052"]


def test_detect_refs_dedupes_preserving_first_order():
    assert plugin_hooks.detect_refs("C1052 ... later C1052 and C99", "XD") == ["C1052", "C99"]


def test_detect_refs_empty_known_letters_widens_to_any_single_digit_too():
    """No local project to read a letter from (cloud mode, no
    decisions/) — rather than reject every single-digit ref, which would
    silently drop real refs whenever there's no local decs.json, this
    widens to accept any letter."""
    assert plugin_hooks.detect_refs("see W5 please", "") == ["W5"]


def test_detect_refs_keeps_source_qualified_refs_including_their_source_and_stale_letter():
    """Finding (C1125): the old word scanner saw only `C1052`, losing the
    source which distinguishes mirrored node namespaces."""
    assert plugin_hooks.detect_refs("check local:Z5 then web:C1052", "XD") == ["local:Z5", "web:C1052"]


def test_known_letters_for_reads_the_project_letter(tmp_path):
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decs.json").write_text(json.dumps({"letter": "W"}))
    assert plugin_hooks.known_letters_for(str(tmp_path)) == "XDW"


def test_known_letters_for_walks_up_from_a_subdirectory(tmp_path):
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decs.json").write_text(json.dumps({"letter": "M"}))
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert plugin_hooks.known_letters_for(str(sub)) == "XDM"


def test_known_letters_for_defaults_to_xd_with_no_project(tmp_path):
    assert plugin_hooks.known_letters_for(str(tmp_path)) == "XD"


def test_known_letters_for_defaults_to_xd_at_the_filesystem_root():
    assert plugin_hooks.known_letters_for(os.sep) == "XD"


# ===========================================================================
# open_nodes_from_tree / format_open_nodes_block / build_session_start_context
# ===========================================================================
def _node(**over):
    base = {"ref": "X1", "status": "not_started", "title": "A task", "author": "agent · x", "created": "2026-01-01"}
    base.update(over)
    return base


def test_open_nodes_from_tree_filters_to_the_open_statuses():
    tree = [
        _node(ref="X1", status="not_started"),
        _node(ref="X2", status="completed"),
        _node(ref="X3", status="closed"),
        _node(ref="X4", status="needs_decision"),
        _node(ref="X5", status="waiting"),
    ]
    assert {n["ref"] for n in plugin_hooks.open_nodes_from_tree(tree)} == {"X1", "X4"}


def test_open_nodes_from_tree_handles_not_a_list():
    assert plugin_hooks.open_nodes_from_tree(None) == []
    assert plugin_hooks.open_nodes_from_tree({"nodes": []}) == []


def test_open_nodes_from_tree_sorts_by_created():
    tree = [_node(ref="X2", created="2026-02-01"), _node(ref="X1", created="2026-01-01")]
    assert [n["ref"] for n in plugin_hooks.open_nodes_from_tree(tree)] == ["X1", "X2"]


def test_format_open_nodes_block_empty_is_empty_string():
    assert plugin_hooks.format_open_nodes_block([]) == ""


def test_format_open_nodes_block_lists_each_node():
    text = plugin_hooks.format_open_nodes_block([_node(ref="X1", title="Ship it", author="agent · x")])
    assert "## Open work" in text
    assert "X1 · not_started · Ship it — agent · x" in text


def test_format_open_nodes_block_caps_and_counts_the_rest():
    nodes = [_node(ref=f"X{i}") for i in range(35)]
    text = plugin_hooks.format_open_nodes_block(nodes)
    assert text.count(" · not_started · ") == plugin_hooks.MAX_OPEN_NODES_SHOWN
    assert "and 5 more open." in text


def test_build_session_start_context_appends_the_open_work_block():
    text = plugin_hooks.build_session_start_context("RULES", [_node(ref="X1")])
    assert text.startswith("RULES")
    assert "## Open work" in text
    assert "X1" in text


def test_build_session_start_context_with_no_tree_is_rules_only():
    assert plugin_hooks.build_session_start_context("RULES", None) == "RULES"


def test_build_session_start_context_with_an_empty_tree_is_rules_only():
    assert plugin_hooks.build_session_start_context("RULES", []) == "RULES"


# ===========================================================================
# build_prompt_refs_reminder / hook-output shapes
# ===========================================================================
def test_build_prompt_refs_reminder_names_the_first_ref_in_the_example():
    text = plugin_hooks.build_prompt_refs_reminder(["C1052", "C9"])
    assert "C1052 C9" in text
    assert "fm task note C1052" in text


def test_session_start_result_hook_output_shape():
    out = plugin_hooks.SessionStartResult("hello").to_hook_output()
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hello"}}


def test_prompt_submit_result_none_context_has_no_hook_output():
    assert plugin_hooks.PromptSubmitResult(None).to_hook_output() is None


def test_prompt_submit_result_with_context_has_the_hook_output_shape():
    out = plugin_hooks.PromptSubmitResult("hi").to_hook_output()
    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "hi"}}


# ===========================================================================
# run_fm_json: the subprocess boundary, faked
# ===========================================================================
def test_run_fm_json_returns_none_when_fm_is_not_on_path(monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise FileNotFoundError("fm")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert plugin_hooks.run_fm_json(["task", "tree"]) is None


def test_run_fm_json_returns_none_on_a_nonzero_exit(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="error"),
    )
    assert plugin_hooks.run_fm_json(["task", "tree"]) is None


def test_run_fm_json_parses_the_stdout_json(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout='[{"ref": "X1"}]', stderr=""),
    )
    assert plugin_hooks.run_fm_json(["task", "tree"]) == [{"ref": "X1"}]
