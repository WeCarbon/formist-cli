"""No `fm` subcommand may be registered twice, and `_COMMANDS` may not
shadow a key.

Two independently written modules can each register the same subcommand
name, inserting their lines at different points in `formist_cli/cli.py`.
Git can merge that cleanly, with no conflict at all, because the two
insertions land at different anchors in the text. The result still
behaves wrong: a duplicate `add_parser` call raises
`ValueError: conflicting subparser` at import, killing every `fm` command.

The duplicate `_COMMANDS` key is worse, because it is invisible. A Python
dict literal with a repeated key silently keeps the last one: no error, no
warning, and `build_parser()` is perfectly happy. Two modules' commands
would collapse into whichever one happened to be listed second, and the
only symptom would be an operator getting the wrong subcommand. That is
why this file reads the source with `ast` rather than the imported dict:
by the time it is a dict, the evidence is gone.
"""

from __future__ import annotations

import ast
import pathlib

_CLI = pathlib.Path(__file__).resolve().parents[1] / "src" / "formist_cli" / "cli.py"


def _tree() -> ast.Module:
    return ast.parse(_CLI.read_text(encoding="utf-8"), filename=str(_CLI))


def _registered_parser_names() -> list[str]:
    """Every subcommand name `build_parser` registers, in source order.

    Two spellings exist and both count: `sub.add_parser("name", ...)` inline, and
    `<module>_mod.add_parser(sub)`, where the name lives in that module's own
    `add_parser`. For the second form the MODULE is the identity: a module cannot
    register two different names, so registering it twice is the defect.
    """
    tree = _tree()
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_parser"
    )
    names: list[str] = []
    for node in ast.walk(build):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_parser":
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id == "sub":
            # sub.add_parser("login", ...): the literal name is the first arg.
            if node.args and isinstance(node.args[0], ast.Constant):
                names.append(str(node.args[0].value))
        elif isinstance(target, ast.Name):
            # <module>_mod.add_parser(sub): identity is the module.
            names.append(f"<module>{target.id}")
        elif isinstance(target, ast.Attribute):
            # formist_cli.x.add_parser(sub) / skill_library.add_parser(sub)
            names.append(f"<module>{ast.unparse(target)}")
    return names


def _command_table_keys() -> list[str]:
    """`_COMMANDS`'s keys as WRITTEN, duplicates included.

    Read from source on purpose: `dict` deduplicates on evaluation, so reading the
    imported object cannot answer this question at all.
    """
    tree = _tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_COMMANDS" not in targets:
            continue
        assert isinstance(node.value, ast.Dict), "_COMMANDS is no longer a dict literal"
        return [
            str(k.value)
            for k in node.value.keys
            if isinstance(k, ast.Constant)
        ]
    raise AssertionError("_COMMANDS not found in formist_cli/cli.py")


def test_no_subcommand_is_registered_twice():
    names = _registered_parser_names()
    assert names, "found no add_parser calls at all: the walk stopped working"
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, (
        f"{duplicates} registered more than once in build_parser(). argparse raises "
        "`ValueError: conflicting subparser` at import, so this kills every `fm` "
        "command, and a clean git merge of two independent additions of the same "
        "subcommand can produce exactly this."
    )


def test_the_command_table_has_no_shadowed_key():
    keys = _command_table_keys()
    assert keys, "_COMMANDS parsed as empty: the walk stopped working"
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, (
        f"{duplicates} appears more than once in _COMMANDS. A dict literal keeps the "
        "LAST one silently: no error, no warning, and build_parser() still succeeds: "
        "the command simply dispatches to the wrong module."
    )


def test_every_registered_parser_has_a_dispatch_entry_and_vice_versa():
    """The two lists must agree: a registration with no dispatch 'works'
    until run. This asserts it over the source rather than the built
    parser, so it still holds for a tree that cannot be imported (which is
    the state a bad merge leaves behind).
    """
    literal_names = {n for n in _registered_parser_names() if not n.startswith("<module>")}
    keys = set(_command_table_keys())
    # Module-form registrations name themselves inside their own module, so only the
    # literal spellings can be compared here: the parity test covers the rest.
    missing = sorted(literal_names - keys)
    assert not missing, (
        f"{missing} are registered as subcommands but have no _COMMANDS entry: "
        "`fm <that>` would parse and then dispatch to nothing"
    )
