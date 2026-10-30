"""Host selection (``--host``) and per-host credential storage."""

from __future__ import annotations

import pytest

from formist_cli import cli, prefs, tokens
from formist_cli.targets import (
    DEFAULT_HOST,
    REGION_AE,
    REGION_CUSTOM,
    REGION_FORMIST,
    REGION_GLOBAL,
    REGION_LOCAL,
    TargetError,
    require_space_id,
    resolve,
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("FORMIST_TOKEN", raising=False)


class TestNamedHosts:
    def test_no_host_means_formist(self):
        target = resolve(None)
        assert DEFAULT_HOST == "formist"
        assert target.api_base == "https://api.formist.ai"
        assert target.name == "formist"
        assert target.env == REGION_FORMIST

    @pytest.mark.parametrize(
        "name,api_base,env",
        [
            ("formist", "https://api.formist.ai", REGION_FORMIST),
            ("evane", "https://api.evane.ai", REGION_GLOBAL),
            ("evane-ae", "https://ae.api.evane.ai", REGION_AE),
            ("formist-cn", "https://formist-api.we-carbon.com", REGION_FORMIST),
        ],
    )
    def test_each_named_host(self, name, api_base, env):
        target = resolve(name)
        assert (target.api_base, target.env, target.name) == (api_base, env, name)

    def test_names_are_case_insensitive(self):
        assert resolve("Evane-AE").name == "evane-ae"


class TestCustomHosts:
    def test_loopback_without_scheme_is_http_and_local(self):
        target = resolve("127.0.0.1:8160")
        assert target.api_base == "http://127.0.0.1:8160"
        assert target.env == REGION_LOCAL
        assert target.name == "127.0.0.1:8160"
        assert not target.is_prod

    def test_a_remote_host_without_scheme_is_https(self):
        target = resolve("api.example.com")
        assert target.api_base == "https://api.example.com"
        assert target.env == REGION_CUSTOM

    def test_a_full_url_keeps_its_scheme_and_port(self):
        target = resolve("http://10.0.0.5:9000")
        assert target.api_base == "http://10.0.0.5:9000"
        assert target.name == "10.0.0.5:9000"

    def test_a_trailing_slash_is_dropped(self):
        assert resolve("https://api.example.com/").api_base == "https://api.example.com"

    @pytest.mark.parametrize(
        "value", ["https://api.example.com/v1", "ftp://api.example.com", "https://"]
    )
    def test_values_that_are_not_an_origin_are_refused(self, value):
        with pytest.raises(TargetError):
            resolve(value)


class TestHostFlagPlacement:
    def test_before_the_command(self):
        args = cli.build_parser().parse_args(["--host", "evane", "spaces"])
        assert args.host == "evane"

    def test_after_the_command(self):
        args = cli.build_parser().parse_args(["spaces", "--host", "evane-ae"])
        assert args.host == "evane-ae"

    def test_absent(self):
        assert cli.build_parser().parse_args(["whoami"]).host is None

    def test_no_env_flag_remains(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["whoami", "--env", "global"])


class TestCredentialsArePerHost:
    def test_logging_into_one_host_does_not_touch_another(self):
        tokens.store("formist", "crst_at_formist")
        tokens.store("evane", "crst_at_evane")
        assert tokens.load("formist") == "crst_at_formist"
        assert tokens.load("evane") == "crst_at_evane"
        assert tokens.token_path("formist") != tokens.token_path("evane")

    def test_a_custom_host_gets_its_own_file(self):
        path = tokens.token_path(resolve("127.0.0.1:8160").name)
        assert path.name == "token-127.0.0.1_8160.json"

    def test_the_evane_regions_share_a_credential(self):
        tokens.store("evane", "crst_at_evane")
        assert tokens.load_for("evane-ae") == ("crst_at_evane", "evane")

    def test_formist_never_borrows_an_evane_credential(self):
        tokens.store("evane", "crst_at_evane")
        assert tokens.load_for("formist") == (None, None)

    def test_the_resolver_reads_the_credential_of_the_named_host(self, capsys):
        tokens.store("formist", "crst_at_formist")
        tokens.store("evane", "crst_at_evane")

        class Args:
            host = "evane"
            space = None

        from formist_cli import context

        assert context.resolve(Args()).token == "crst_at_evane"
        Args.host = None
        assert context.resolve(Args()).token == "crst_at_formist"


class TestRememberedHost:
    def test_a_read_inherits_a_remembered_host(self):
        prefs.store(env="evane")
        eff = prefs.effective(env_flag=None, space_flag=None, for_write=False)
        assert eff.target.name == "evane"

    def test_a_write_refuses_to_inherit_it(self):
        prefs.store(env="evane")
        with pytest.raises(prefs.PrefsError, match="--host evane"):
            prefs.effective(env_flag=None, space_flag=None, for_write=True)


def test_a_space_is_never_looked_up_by_name():
    with pytest.raises(TargetError, match="never looks a space up by name"):
        require_space_id("Acme Summit")
    assert require_space_id("452093ea-7b91-491e-88b6-3285edd48a31")
