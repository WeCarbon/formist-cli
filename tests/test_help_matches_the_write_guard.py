"""``fm``'s --help must describe the write guard that exists, not a
stricter one.

The guard (``formist_cli/prefs.py::effective``) refuses to inherit a
remembered environment for a write, but with nothing remembered by
``fm use`` that refusal is never reached and the write proceeds against
the built-in default host. So ``--env`` is required for a write to
anywhere but that default, and required unconditionally once ``fm use``
has remembered one. A help string saying flatly that a write "requires
``--env`` on the command line" overstates that, and the same overstated
sentence can end up copied across several commands with nothing comparing
the copies against the actual guard, so a per-file fix cannot see the
next copy added later.

This file does not re-test the guard itself; that is covered elsewhere.
What was missing is not coverage of the behaviour or coverage of the
prose, it is the comparison between them: nothing read the two against
each other, so an overstated help string could describe a guard the code
had never implemented while every test stayed green. So the main test
below establishes the behaviour and checks the prose against it in one
place: if ``--env`` is ever made genuinely required, that test reds on
its own first line and points at the help text that must change with it.
"""

from __future__ import annotations

import argparse
import re

import pytest

from formist_cli import prefs
from formist_cli.cli import build_parser
from formist_cli.targets import DEFAULT_HOST

#: Ways the overstated claim was actually spelled, plus the near-misses.
#: Deliberately NOT a loose "--env" search: most of the tool's help mentions
#: --env correctly, and a check that fires on all of them is a check somebody
#: turns off.
FALSE_CLAIM_PATTERNS = (
    re.compile(r"requires?\s+--env", re.IGNORECASE),
    re.compile(r"--env\s+is\s+required", re.IGNORECASE),
    re.compile(r"REQUIRED\s+for\s+a\s+write", re.IGNORECASE),
    re.compile(r"always\s+(?:needs?|requires?)\s+--env", re.IGNORECASE),
    re.compile(r"must\s+(?:pass|give|supply)\s+--env", re.IGNORECASE),
)

#: Subcommands the walk must reach. Not the full list on purpose: this exists to
#: prove the WALK LANDED, so it names the four that carried the false claim.
MUST_REACH = {"import", "airtable", "export", "use"}

#: Below this, the walk collected option names and no prose.
MIN_CORPUS_CHARS = 20_000


def _walk(parser: argparse.ArgumentParser, path: str = "fm"):
    """Yield ``(where, text)`` for every help/description string in the tree."""
    for attr in ("description", "epilog", "usage"):
        text = getattr(parser, attr, None)
        if text:
            yield f"{path} [{attr}]", str(text)
    for action in parser._actions:
        if action.help:
            name = "/".join(action.option_strings) or action.dest
            yield f"{path} {name}", str(action.help)
        if isinstance(action, argparse._SubParsersAction):
            for sub_name, sub in action.choices.items():
                yield from _walk(sub, f"{path} {sub_name}")


def test_the_walk_actually_reaches_the_help_it_claims_to_check():
    """An absence assertion over an empty corpus passes. This is what stops that.

    Not a nicety: the check below is the only thing between this tool's help and a
    claim nobody re-reads, and a silently-empty walk would report clean forever.
    """
    entries = list(_walk(build_parser()))
    reached = {where.split()[1] for where, _ in entries if len(where.split()) > 1}
    missing = MUST_REACH - reached

    assert not missing, f"the parser walk never reached: {sorted(missing)}"
    corpus = "".join(text for _, text in entries)
    assert len(corpus) >= MIN_CORPUS_CHARS, (
        f"the walk collected only {len(corpus)} characters of help text: too "
        "little to be the real tree; it probably stopped at the top level instead "
        "of descending into the subparsers."
    )


def test_no_help_string_claims_env_is_required_for_a_write(tmp_path, monkeypatch):
    """The comparison that was missing: what the code does, then what the help says.

    Both halves in ONE test deliberately. The behaviour and the prose were each
    covered and each green while contradicting each other for five help strings;
    what nobody had written down is that they answer the same question.
    """
    # An isolated, empty config: `fm use` has remembered nothing. Never the
    # operator's real ~/.config/evane.
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "evane"))

    # (1) What the guard actually does. If this ever raises instead, `--env` HAS
    # become required, the help strings below become true, and this test is the
    # thing that is wrong: which is the point of pinning it here rather than
    # leaving the prose rule to police itself.
    eff = prefs.effective(env_flag=None, space_flag=None, for_write=True)
    assert eff.for_write is True
    assert eff.target.name == DEFAULT_HOST, (
        "a write with nothing remembered no longer falls back to the default host. If that "
        "was deliberate, the help strings this test polices may now say --env is "
        "required: update FALSE_CLAIM_PATTERNS and this assertion together."
    )

    # (2) Therefore no help string anywhere may say --env is required for a write.
    # The message names WHERE, because the whole value of doing this over the tree
    # instead of per file is finding the copy the fixer did not know about.
    offenders = [
        (where, pattern.pattern, text.strip())
        for where, text in _walk(build_parser())
        for pattern in FALSE_CLAIM_PATTERNS
        if pattern.search(text)
    ]

    assert not offenders, (
        "help text claims --env is required for a write. The guard only refuses to "
        "INHERIT a remembered one (formist_cli/prefs.py::effective); with nothing "
        "remembered a write targets the default host, as asserted above. Say what it does:\n"
        + "\n".join(f"  {where}: /{pat}/ in {text!r}" for where, pat, text in offenders)
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "--apply requires --env on the command line",
        "target environment: REQUIRED for a write",
        "persist the changes this run reports (requires --env)",
    ],
)
def test_the_patterns_catch_the_sentences_that_were_actually_shipped(sentence):
    """The detector, tested against the real strings: not against a specimen.

    A pattern set is exactly as good as the text it was written from, and
    the way this check dies is somebody rewording a help string past it
    while believing the guard still holds. These three are verbatim what
    the tree printed before the fix; if a pattern stops matching one, the
    pattern was narrowed, not the problem.
    """
    assert any(p.search(sentence) for p in FALSE_CLAIM_PATTERNS)
