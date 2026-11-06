"""The Claude Code plugin manifests (``.claude-plugin/plugin.json``,
``hooks/hooks.json``, ``monitors/monitors.json``) at the repo root
(C1053/C1139) — not Python, so nothing else in the gate would notice a
command drifting out of sync with ``tasks.py``'s own ``_VERBS`` if one
of these files' ``command`` string were, say, mistyped or orphaned by a
later rename. This file is that notice.
"""

from __future__ import annotations

import json
import pathlib
import re

from formist_cli import tasks

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(rel_path: str) -> object:
    with open(REPO_ROOT / rel_path, encoding="utf-8") as fh:
        return json.load(fh)


def _verbs_used_in(command: str) -> list[str]:
    """Pull every ``task <verb>`` out of a shell command string — these
    manifests only ever call ``fm task <verb> [flags]``, never a bare
    ``fm <verb>``, so this is deliberately narrow rather than a general
    shell parser."""
    return re.findall(r"\bfm task ([a-z][a-z-]*)", command)


def test_plugin_manifest_is_valid_json():
    manifest = _load(".claude-plugin/plugin.json")
    assert manifest["name"]
    assert manifest["license"] == "MIT"


def test_hooks_json_commands_call_real_verbs():
    hooks = _load("hooks/hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit"}
    for event, entries in hooks.items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                for verb in _verbs_used_in(hook["command"]):
                    assert verb in tasks._VERBS, f"{event}'s command names an unknown verb: {verb!r}"


def test_hooks_json_points_at_the_plugin_subcommands():
    hooks = _load("hooks/hooks.json")["hooks"]
    session_start_cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    prompt_submit_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "plugin-session-start" in session_start_cmd
    assert "plugin-prompt-refs" in prompt_submit_cmd


def test_monitors_json_command_calls_a_real_verb():
    monitors = _load("monitors/monitors.json")
    assert len(monitors) == 1
    entry = monitors[0]
    assert entry["name"]
    assert entry["description"]
    verbs = _verbs_used_in(entry["command"])
    assert verbs == ["monitor"]
    assert "--here" in entry["command"]
