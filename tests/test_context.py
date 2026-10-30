"""One resolver for "which env, which space, which token", and a guard on it.

Every ``fm`` command that talks to the API has to answer the same three
questions and announce where each answer came from. A second,
independently maintained copy of that sequence is easy to miss (a grep for
a function name or a review of one file would not find a copy under a
different name), and the two can drift: one copy might refuse locally when
no token is stored, while another opens a socket, waits for a 401, and
only then translates it. Keeping this a property of the package, checked
by ``test_the_resolver_is_not_reimplemented`` counting every caller of
``prefs.effective``, is what survives a new command added later without
anyone remembering the rule.

A separate guard with the same message lives in ``api.py::admin_get``,
for a caller that did not come through the CLI resolver; that one is
defence in depth rather than duplication.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from formist_cli import context, prefs, tokens
from formist_cli.targets import resolve as resolve_target


class _Args:
    def __init__(self, host=None, space=None):
        self.host = host
        self.space = space


def _effective_callers(scan_dir: pathlib.Path) -> list[str]:
    """Every ``prefs.effective(...)`` call site under `scan_dir`, by AST.

    A module-level helper so the companion test can drive this exact code
    rather than a reimplementation of it.
    """
    callers = []
    for module in sorted(scan_dir.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "effective"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "prefs"
            ):
                callers.append(f"{module.name}:{node.lineno}")
    return callers


class TestTheResolverIsSingular:
    def test_the_resolver_is_not_reimplemented(self):
        """Counted by AST across the package, not by grepping a function
        name: two copies could have different names, so a name search
        could miss one. What is shared is the operation."""
        callers = _effective_callers(pathlib.Path(context.__file__).parent)
        assert len(callers) == 1 and callers[0].startswith("context.py:"), (
            f"prefs.effective must be called from context.py only; found {callers}"
        )

    def test_that_count_can_actually_fail(self, tmp_path):
        """Drives the real helper over a directory with two callers, so a
        broken helper would actually fail this test."""
        (tmp_path / "a.py").write_text("prefs.effective(env_flag=None)\n")
        (tmp_path / "b.py").write_text("prefs.effective(env_flag=None)\n")
        assert len(_effective_callers(tmp_path)) == 2

        (tmp_path / "b.py").write_text("pass\n")
        assert len(_effective_callers(tmp_path)) == 1, (
            "the helper must go quiet when a copy is removed, or it always fails"
        )


class TestItResolvesAndRefusesInOnePlace:
    def test_it_returns_target_space_and_token(self, monkeypatch):
        monkeypatch.setattr(
            prefs, "effective",
            lambda **k: _Eff(resolve_target("127.0.0.1:8160"), "sp", "flag"),
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: ("t0k", "local"))
        ctx = context.resolve(_Args(host="local"))
        assert ctx.target.env == "local"
        assert ctx.space == "sp"
        assert ctx.token == "t0k"

    def test_a_missing_space_is_refused_locally(self, monkeypatch):
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), None, "unset")
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: ("t0k", "local"))
        with pytest.raises(prefs.PrefsError) as exc:
            context.resolve(_Args(), need_space=True)
        assert "fm use --space" in str(exc.value)

    def test_a_missing_token_is_refused_LOCALLY_not_by_a_round_trip(self, monkeypatch):
        """The behaviour that existed in logs.py and nowhere else. Refusing here
        costs no socket and says what to do; a 401 from the server says the same
        thing after a round trip and only if the server was reachable."""
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("evane-ae"), "sp", "flag")
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
        with pytest.raises(tokens.TokenError) as exc:
            context.resolve(_Args(host="ae"), need_token=True)
        assert "fm login --host evane-ae" in str(exc.value)

    def test_a_missing_token_is_TOLERATED_when_not_required(self, monkeypatch):
        """`fm health` is public. A resolver that always demanded a token would
        break the one command that must work before you have one."""
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), None, "unset")
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
        assert context.resolve(_Args()).token is None

    def test_a_borrowed_token_is_ANNOUNCED(self, monkeypatch, capsys):
        """A credential silently sourced from another region's file is exactly the
        invisible state this tool refuses everywhere else."""
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("evane-ae"), "sp", "flag")
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: ("t0k", "global"))
        context.resolve(_Args(host="ae"))
        assert "using the global token" in capsys.readouterr().err

    def test_the_banner_is_printed_to_STDERR_so_stdout_stays_pipeable(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), "sp", "flag")
        )
        monkeypatch.setattr(tokens, "load_for", lambda env: ("t0k", "local"))
        context.resolve(_Args())
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "BANNER" in captured.err

    def test_an_env_sourced_token_is_used_as_is_never_refreshed_and_announced(
        self, monkeypatch, capsys
    ):
        """$FORMIST_TOKEN wins over the file for this call, and must not be
        refreshed: refreshing would call `login.store_payload`, i.e. write
        the env-sourced credential to disk, exactly what the design
        promises never to do."""
        monkeypatch.setattr(
            prefs, "effective", lambda **k: _Eff(resolve_target("127.0.0.1:8160"), "sp", "flag")
        )
        monkeypatch.setattr(
            tokens, "load_for", lambda env: ("envtok", tokens.ENV_TOKEN_ORIGIN)
        )

        def _must_not_be_called(*a, **k):
            raise AssertionError("an env-sourced token must never be refreshed")

        monkeypatch.setattr(context, "_refresh_if_stale", _must_not_be_called)
        ctx = context.resolve(_Args(host="local"))
        assert ctx.token == "envtok"
        assert f"environment ({tokens.ENV_TOKEN_VAR})" in capsys.readouterr().err


class _Eff:
    """Stand-in for prefs.Effective: only the attributes context.resolve reads."""

    def __init__(self, target, space, space_source):
        self.target = target
        self.space = space
        self.space_source = space_source

    def banner(self) -> str:
        return "BANNER"


class TestNoCommandPaysARoundTripToLearnItHasNoToken:
    """A command with `need_token=True` wired in must refuse locally before
    opening any socket when no token is stored. With no stored token,
    `fm chats --host evane` previously did open a connection and only
    learned there was no token from the server's 401. The ``/admin``
    commands never had this gap, since ``api.admin_get`` guards them, which
    is why the commands below are named individually rather than swept.
    """

    #: ``OUT`` is substituted with a path inside tmp_path. The export case
    #: needs a path that does not already exist, or a different guard
    #: (refusing to overwrite an existing file) would make the command exit
    #: non-zero with no socket opened regardless of whether the token guard
    #: runs, masking a missing token check.
    COMMANDS = [
        ["chats", "--host", "evane", "--space", "452093ea-7b91-491e-88b6-3285edd48a31"],
        ["get", "--host", "evane", "--space", "452093ea-7b91-491e-88b6-3285edd48a31",
         "task", "--name", "x"],
        ["export", "--host", "evane", "--space", "452093ea-7b91-491e-88b6-3285edd48a31",
         "--out", "OUT"],
        ["users", "--host", "evane"],
    ]

    @pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: a[0])
    def test_it_refuses_locally_without_opening_a_socket(self, argv, monkeypatch, tmp_path):
        import urllib.request

        from formist_cli import cli, tokens

        opened: list[str] = []

        def _traced(request, **kwargs):
            opened.append(getattr(request, "full_url", str(request)))
            raise AssertionError("a socket was opened before the token was checked")

        monkeypatch.setattr(urllib.request, "urlopen", _traced)
        # No stored credential for any env: the situation under test.
        monkeypatch.setattr(tokens, "load_for", lambda env: (None, None))
        monkeypatch.setattr(cli.context.tokens, "load_for", lambda env: (None, None))

        argv = [str(tmp_path / "out.json") if a == "OUT" else a for a in argv]
        rc = cli.main(argv)
        assert opened == [], f"{argv[0]} reached the network: {opened}"
        assert rc == 2, f"{argv[0]} must exit non-zero, got {rc}"
