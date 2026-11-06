"""Tests for ``fm skill``: the noun group ``skills-add``/``skills-update``/
``skill-attach``/``skill-library-install`` folded into, plus the four old
top-level names kept as deprecated-for-one-release aliases.

``cli.cmd_skill`` is pure routing (adapt the two generic positionals,
``arg1``/``arg2``, into the names the ORIGINAL handler reads, then call it
unchanged); these tests assert the routing, not the underlying writes,
which already have their own tests (``test_skills.py``,
``test_skill_library_install.py``, the ``skill-attach`` cases in
``test_uploads*.py``).
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import cli, skill_library, skills
from formist_cli import uploads as uploads_mod


def _args(**over):
    base = {"subject": None, "arg1": None, "arg2": None, "skill": None}
    base.update(over)
    return argparse.Namespace(**base)


def test_add_calls_skills_add_with_source_from_arg1(monkeypatch):
    seen = {}
    monkeypatch.setattr(skills, "cmd_skills_add", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_skill(_args(subject="add", arg1="/tmp/src"))
    assert rc == 0
    assert seen["args"].source == "/tmp/src"


def test_update_calls_skills_update_with_source_from_arg1_and_keeps_skill_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(skills, "cmd_skills_update", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_skill(_args(subject="update", arg1="/tmp/src", skill="my-skill"))
    assert rc == 0
    assert seen["args"].source == "/tmp/src"
    assert seen["args"].skill == "my-skill"


def test_attach_calls_skill_attach_with_skill_from_arg1_and_file_from_arg2(monkeypatch):
    seen = {}
    monkeypatch.setattr(uploads_mod, "cmd_skill_attach", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_skill(_args(subject="attach", arg1="my-skill", arg2="/tmp/file.pdf"))
    assert rc == 0
    assert seen["args"].skill == "my-skill"
    assert seen["args"].file == "/tmp/file.pdf"


def test_install_package_calls_run_install_with_package_from_arg1(monkeypatch):
    seen = {}
    monkeypatch.setattr(skill_library, "run_install", lambda a: seen.update(args=a) or 0)
    rc = cli.cmd_skill(_args(subject="install-package", arg1="com.wecarbon.esg-report"))
    assert rc == 0
    assert seen["args"].package == "com.wecarbon.esg-report"


def test_unknown_subject_is_refused_locally_naming_the_valid_ones():
    with pytest.raises(skills.SkillGroupError, match="add, update, attach, install-package"):
        cli.cmd_skill(_args(subject="delete"))


def _alias_handler(fn):
    """The real function `_COMMANDS[old]` calls, read out of its closure.

    ``_COMMANDS`` is a dict LITERAL evaluated once at import, so
    ``_deprecated_alias(old, new, skills.cmd_skills_add)`` binds that
    specific function OBJECT into the wrapper's closure right then.
    Monkeypatching ``skills.cmd_skills_add`` afterwards changes the
    MODULE's attribute, not the object already captured -- so asserting
    identity against the closure cell is what actually proves "the old
    command still reaches the real handler", rather than re-running a
    stale reference.
    """
    i = fn.__code__.co_freevars.index("handler")
    return fn.__closure__[i].cell_contents


@pytest.mark.parametrize(
    "old_command,new_group,module,fn_name",
    [
        ("skills-add", "skill add", skills, "cmd_skills_add"),
        ("skills-update", "skill update", skills, "cmd_skills_update"),
        ("skill-attach", "skill attach", uploads_mod, "cmd_skill_attach"),
        ("skill-library-install", "skill install-package", skill_library, "run_install"),
    ],
)
def test_old_top_level_name_is_wired_to_the_real_handler(old_command, new_group, module, fn_name):
    assert _alias_handler(cli._COMMANDS[old_command]) is getattr(module, fn_name)


@pytest.mark.parametrize(
    "old_command,new_group",
    [
        ("skills-add", "skill add"),
        ("skills-update", "skill update"),
        ("skill-attach", "skill attach"),
        ("skill-library-install", "skill install-package"),
    ],
)
def test_old_top_level_name_warns_then_runs_the_handler(capsys, old_command, new_group):
    """The wrapping mechanism itself (`cli._deprecated_alias`), exercised
    with a controlled fake handler so this does not depend on which real
    function the production `_COMMANDS` entry happens to hold (that part
    is `test_old_top_level_name_is_wired_to_the_real_handler`, above).
    Prints to STDERR, so a script parsing stdout (``--json`` included)
    never sees it.
    """
    calls = []
    wrapped = cli._deprecated_alias(old_command, new_group, lambda a: calls.append(a) or 0)
    rc = wrapped(argparse.Namespace())
    assert rc == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert f"`fm {old_command}`" in err
    assert new_group in err


def test_skill_is_registered_and_the_four_old_names_still_are_too():
    for name in ("skill", "skills-add", "skills-update", "skill-attach", "skill-library-install"):
        assert name in cli._COMMANDS, f"{name!r} missing from _COMMANDS"
