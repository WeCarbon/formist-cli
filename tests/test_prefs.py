"""Remembered defaults, and the rule that keeps them from writing production.

``formist_cli/targets.py:12`` carries a deliberate refusal by its author:

    **1. Default to local, and never infer prod.** There is no state file and no
    "current environment" [...] A remembered target is a target someone forgets
    they set.

That diagnosis is right. The conclusion: that the only cure is having no memory
at all: is not. A remembered value is dangerous when it silently supplies the
DANGEROUS HALF of a decision, and the two halves are not equally dangerous:

    a space is a WHAT: 36 characters nobody verifies by eye, and the half the
      transcribed-id failure lives in;
    an env is a WHERE: one word, and the half that decides whether this is
      production.

So ``fm`` remembers the what and requires the where. A write cannot happen unless
the operator typed the environment IN THE COMMAND THEY JUST RAN. That is not a
prompt, so ``yes |`` does not defeat it, and it survives scripting: a script that
writes production contains the string ``ae`` in its source where a reviewer sees it.

Every test here is a refusal or a provenance assertion. Those are the product.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formist_cli import prefs, tokens
from formist_cli.prefs import (
    PrefsError,
    clear,
    effective,
    load,
    prefs_path,
    store,
)

SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
OTHER_SPACE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never touch the operator's real ~/.config/evane during a test run."""
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "evane"))
    yield


class TestAWriteNeverReadsTheRememberedEnv:
    """THE class in this file. Everything else is ergonomics."""

    def test_write_without_env_flag_refuses_when_a_default_is_remembered(self):
        store(space=SPACE, env="evane-ae")
        with pytest.raises(PrefsError) as exc:
            effective(env_flag=None, space_flag=None, for_write=True)
        message = str(exc.value)
        assert "--host" in message, "the refusal must name the fix"
        assert "ae" in message, "and must say which remembered value it refused"

    def test_the_refusal_explains_that_a_write_never_inherits_the_env(self):
        store(space=SPACE, env="evane-ae")
        with pytest.raises(PrefsError) as exc:
            effective(env_flag=None, space_flag=None, for_write=True)
        assert "never inherits" in str(exc.value).lower()

    def test_write_with_an_explicit_env_flag_is_allowed(self):
        store(space=SPACE, env="evane-ae")
        eff = effective(env_flag="evane-ae", space_flag=None, for_write=True)
        assert eff.target.env == "ae"

    def test_a_remembered_LOCAL_env_is_refused_on_a_write_too(self):
        """Uniform, deliberately. If the habit is 'sometimes I must name the env'
        it is not a habit. A remembered `local` today is a remembered `ae` after
        one `fm use`, and the operator's fingers must already know."""
        store(space=SPACE, env="127.0.0.1:8160")
        with pytest.raises(PrefsError, match="--host"):
            effective(env_flag=None, space_flag=None, for_write=True)

    def test_write_with_no_remembered_env_at_all_falls_back_to_the_default_host(self):
        """With nothing remembered there is no belief to contradict, so the
        documented default stands: and it cannot be prod."""
        store(space=SPACE)
        eff = effective(env_flag=None, space_flag=None, for_write=True)
        assert eff.target.name == "formist"
        assert eff.target.api_base == "https://api.formist.ai"

    def test_reads_DO_inherit_the_remembered_env(self):
        """The whole point of `fm use`. A read against the wrong env is a wrong
        answer; a write against the wrong env is a wrong database."""
        store(space=SPACE, env="evane-ae")
        eff = effective(env_flag=None, space_flag=None, for_write=False)
        assert eff.target.env == "ae"


class TestTheRememberedSpaceIsInheritedByBoth:
    def test_write_inherits_the_remembered_space(self):
        store(space=SPACE, env="evane-ae")
        eff = effective(env_flag="evane-ae", space_flag=None, for_write=True)
        assert eff.space == SPACE

    def test_an_explicit_space_flag_wins(self):
        store(space=SPACE, env="evane-ae")
        eff = effective(env_flag="evane-ae", space_flag=OTHER_SPACE, for_write=True)
        assert eff.space == OTHER_SPACE

    def test_a_remembered_space_must_still_be_a_uuid(self):
        """`require_space_id` is not bypassed by coming from a file."""
        with pytest.raises(PrefsError):
            store(space="Acme Summit")


class TestProvenanceIsInTheBanner:
    """Printing the target alone is close to useless: the operator already
    believes they know it. Printing WHERE EACH HALF CAME FROM and HOW OLD it is
    is what breaks a wrong belief."""

    def test_banner_names_the_source_of_the_env(self):
        store(space=SPACE, env="evane-ae")
        assert "--host flag" in effective(
            env_flag="evane-ae", space_flag=None, for_write=True
        ).banner()

    def test_banner_names_the_source_of_the_space(self):
        store(space=SPACE, env="evane-ae")
        banner = effective(env_flag="evane-ae", space_flag=None, for_write=True).banner()
        assert "fm use" in banner, "a remembered space must be labelled as remembered"

    def test_banner_shows_how_old_a_remembered_value_is(self):
        """Assert against the age the prefs themselves report, not against a
        literal spelling of it: a test that hardcodes "ago" fails on "just now"
        while the property it cares about holds perfectly well."""
        store(space=SPACE, env="evane-ae")
        banner = effective(env_flag="evane-ae", space_flag=None, for_write=True).banner()
        assert load().age in banner

    def test_banner_distinguishes_a_flag_from_a_default(self):
        store(space=SPACE, env="evane-ae")
        banner = effective(
            env_flag="evane-ae", space_flag=OTHER_SPACE, for_write=True
        ).banner()
        assert "fm use" not in banner.split("space")[1], (
            "an explicitly passed space must not be labelled as remembered"
        )

    def test_banner_is_produced_for_reads_too(self):
        """A read banner is where a stale default is noticed BEFORE it matters."""
        store(space=SPACE, env="evane-ae")
        assert effective(env_flag=None, space_flag=None, for_write=False).banner()


class TestRegionDisagreementRefusesBeforeASocket:
    """`fm use` records the region `/admin/spaces` reported for the space. A write
    whose `--env` contradicts it is refused locally: no request is made.

    This is the cheapest possible catch for a write aimed at the wrong
    region, and it costs one comparison.
    """

    def test_write_to_the_wrong_region_is_refused(self):
        store(space=SPACE, env="evane-ae", space_region="ae")
        with pytest.raises(PrefsError) as exc:
            effective(env_flag="evane", space_flag=None, for_write=True)
        assert "'ae'" in str(exc.value) and "'evane'" in str(exc.value)
        assert "--host evane-ae" in str(exc.value), "the fix names a host, not a region"

    def test_write_to_the_recorded_region_is_allowed(self):
        store(space=SPACE, env="evane-ae", space_region="ae")
        assert effective(env_flag="evane-ae", space_flag=None, for_write=True).target.env == "ae"

    def test_no_recorded_region_does_not_block(self):
        """Absence of knowledge is not evidence of disagreement."""
        store(space=SPACE, env="evane-ae")
        assert effective(env_flag="evane", space_flag=None, for_write=True)

    def test_an_explicit_space_flag_does_not_inherit_the_recorded_region(self):
        """The recorded region describes the REMEMBERED space. Applying it to a
        different space passed on the command line would refuse a correct write,
        or worse, permit a wrong one."""
        store(space=SPACE, env="evane-ae", space_region="ae")
        assert effective(
            env_flag="evane", space_flag=OTHER_SPACE, for_write=True
        ).target.env == "global"


class TestStorage:
    def test_store_then_load_round_trips(self):
        store(space=SPACE, env="evane-ae", space_region="ae")
        prefs = load()
        assert prefs.space == SPACE and prefs.env == "evane-ae" and prefs.space_region == "ae"

    def test_clear_removes_the_file(self):
        store(space=SPACE, env="evane-ae")
        assert clear() is True
        assert load().space is None

    def test_clear_on_nothing_is_not_an_error(self):
        assert clear() is False

    def test_store_updates_only_what_it_is_given(self):
        store(space=SPACE, env="evane-ae")
        store(env="127.0.0.1:8160")
        assert load().space == SPACE, "setting the env must not forget the space"
        assert load().env == "127.0.0.1:8160"

    def test_a_corrupt_prefs_file_is_a_legible_error_not_a_traceback(self):
        store(space=SPACE, env="evane-ae")
        prefs_path().write_text("{not json")
        with pytest.raises(PrefsError, match="could not read"):
            load()

    def test_a_custom_origin_is_stored_as_its_host_and_port(self):
        """Both `dev` and `local` are accepted as spellings for the same
        target, but only one is stored: `local` wins because it is what the
        allow-list key, the token filename and every existing string
        already say, and because 'dev' is ambiguous elsewhere in this
        project."""
        store(env="localhost:8160")
        assert load().env == "localhost:8160"

    def test_stored_env_must_be_valid(self):
        with pytest.raises(PrefsError, match="neither a known deployment"):
            store(env="https://api.example.com/some/path")


class TestClearedDefaultsStopApplying:
    def test_after_clear_a_write_no_longer_refuses(self):
        store(space=SPACE, env="evane-ae")
        clear()
        eff = effective(env_flag=None, space_flag=None, for_write=True)
        assert eff.target.name == "formist" and eff.space is None


class TestResidencyRegionIsNotADeploymentTarget:
    """Found end-to-end, not by unit test: `space_region` and `env` are DIFFERENT
    VOCABULARIES that share two of three tokens.

    `space_region` is data residency: `global` or `ae`, a property of the row.
    `env` is a deployment target: `local`, `ae` or `global`, a property of the
    host being talked to. A local dev server reports every one of its spaces as
    `region=global` (that is its HOME_REGION), so recording that and then comparing
    it against `env` refused every local write with:

        the remembered space lives in 'global', but this command targets 'local'

    and, far worse, the refusal's own remediation line read `--host evane`,
    which aims a local-dev payload at PRODUCTION. A safety check whose advice
    points at prod is worse than no check.

    So the comparison is meaningful only when the target IS a prod region.
    """

    def test_a_local_write_is_not_refused_by_a_recorded_residency_region(self):
        store(space=SPACE, env="127.0.0.1:8160", space_region="global")
        eff = effective(env_flag="127.0.0.1:8160", space_flag=None, for_write=True)
        assert eff.target.env == "local"

    def test_a_local_write_is_not_refused_when_the_space_is_ae_resident(self):
        store(space=SPACE, env="127.0.0.1:8160", space_region="ae")
        assert effective(
            env_flag="127.0.0.1:8160", space_flag=None, for_write=True
        ).target.env == "local"

    def test_the_prod_cross_region_refusal_still_fires(self):
        """The check must keep working where it is actually meaningful."""
        store(space=SPACE, env="evane-ae", space_region="ae")
        with pytest.raises(PrefsError, match="resident"):
            effective(env_flag="evane", space_flag=None, for_write=True)


class TestOneCredentialDirectory:
    """Prefs and tokens must resolve to ONE directory, by construction.

    They did not, once. `formist_cli/prefs.py` carried a line-for-line copy
    of the resolver whose docstring read "same directory as the tokens,
    resolved the same way", a promise held by nothing but the two copies
    happening to agree. Merged into one implementation.

    The reason this one was deduplicated while other same-named helpers in the
    package were left alone: what drifts here is WHERE THE CREDENTIALS LIVE. Two
    copies means `fm logout` can one day clear a directory `fm login` did not
    write to, and the symptom of that is a token that survives a logout: a
    security failure that looks like nothing at all.
    """

    def test_prefs_and_tokens_share_one_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
        assert prefs.prefs_path().parent == tokens.token_path("local").parent

    def test_they_agree_under_every_resolution_branch(self, tmp_path, monkeypatch):
        """All three branches, because a copy would most plausibly drift on the
        fallback nobody sets by hand."""
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "explicit"))
        assert prefs.prefs_path().parent == tokens.token_path("evane-ae").parent

        monkeypatch.delenv("FORMIST_CONFIG_DIR")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert prefs.prefs_path().parent == tokens.token_path("evane-ae").parent
        assert prefs.prefs_path().parent == tmp_path / "xdg" / "formist"

        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert prefs.prefs_path().parent == tokens.token_path("evane-ae").parent

    def test_prefs_resolves_THROUGH_tokens_not_merely_alongside_it(self, monkeypatch):
        """The property, tested by WIRING rather than by name.

        The first version of this test asserted `not hasattr(prefs, "_config_dir")`
       : and a mutation that re-copied the resolver under the name
        `_regrown_config_dir` SURVIVED it. A name check can only catch a copy that
        reuses the old name, and the whole failure mode is two people inventing the
        same thing and calling it different things.

        So: move `tokens.config_dir` and require `prefs_path()` to move with it. A
        private copy in prefs, under any name, however perfectly it agrees
        today, cannot follow, because it is not reading this function.
        """
        monkeypatch.setattr(tokens, "config_dir", lambda: Path("/moved/elsewhere"))
        assert prefs.prefs_path() == Path("/moved/elsewhere/prefs.json")
