"""`fm doctor`: the readable rollup, and the distinction it must never blur.

`fm health` answers about one environment. `doctor` answers "is the deployment
all right", which means talking to several and surviving the ones that do not
answer.

The property under test is honesty about WHY a check is not green. "Could not
reach ae" and "reached ae and it is unhealthy" are different facts with different
next actions, and a rollup that renders both as a red dot has destroyed the only
information the operator needed. This is the same failure as reporting an exit
code instead of test counts: the summary that loses the distinction is the one
that gets believed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from formist_cli import cli, login, tokens
from formist_cli.doctor import EnvReport, Status, rollup


def test_a_healthy_env_is_ok():
    report = EnvReport.from_health("local", {"status": "ok", "checks": []})
    assert report.status is Status.OK


def test_an_unhealthy_env_is_degraded_not_unreachable():
    report = EnvReport.from_health(
        "ae", {"status": "degraded", "checks": [
            {"name": "attendee", "status": "error", "detail": "timeout"}
        ]},
    )
    assert report.status is Status.DEGRADED
    assert "attendee" in report.detail


def test_an_unreachable_env_is_UNREACHABLE_and_says_so():
    """The distinction this file exists for."""
    report = EnvReport.unreachable("ae", "connection refused")
    assert report.status is Status.UNREACHABLE
    assert report.status is not Status.DEGRADED
    assert "connection refused" in report.detail


def test_a_fatal_check_is_worse_than_a_non_fatal_one():
    non_fatal = EnvReport.from_health("ae", {"status": "degraded", "checks": [
        {"name": "attendee", "status": "error", "fatal": False}]})
    fatal = EnvReport.from_health("ae", {"status": "degraded", "checks": [
        {"name": "database", "status": "error", "fatal": True}]})
    assert fatal.status is Status.FAILED
    assert non_fatal.status is Status.DEGRADED


def test_rollup_reports_every_env_even_when_one_is_unreachable():
    """One region down must not hide the others. A rollup that aborts on the first
    failure answers a different question than the one asked."""
    reports = [
        EnvReport.from_health("global", {"status": "ok", "checks": []}),
        EnvReport.unreachable("ae", "timed out"),
    ]
    text = rollup(reports)
    assert "global" in text and "ae" in text
    assert "timed out" in text


def test_rollup_exit_code_is_worst_status_not_first():
    ok = EnvReport.from_health("global", {"status": "ok", "checks": []})
    bad = EnvReport.unreachable("ae", "timed out")
    assert Status.worst([ok.status, bad.status]) is Status.UNREACHABLE
    assert Status.worst([ok.status]) is Status.OK


def test_a_health_payload_that_is_not_a_dict_is_not_reported_as_healthy():
    """A shape ev did not expect is a fact about the deployment, not a pass. The
    retired we-carbon stack answers 200 with a DIFFERENT health schema, and a
    rollup that shrugs and prints ok would launder exactly that."""
    report = EnvReport.from_health("ae", "OK")
    assert report.status is not Status.OK


# --- the credential annotation --------------------------------------------------
#
# Found 2026-09-09: `_with_credential` (formist_cli/cli.py) called `tokens.describe`
# on the STORED credential as though it were a JWT. Since `fm login` moved to the
# OAuth flow (formist_cli/login.py), every stored access token is opaque
# (``crst_at_…``, no dots: mcp_app/oauth/provider.py::ACCESS_PREFIX), so
# `describe` raised `TokenError("not a JWT")` on every real login and `fm doctor`
# reported "unreadable" for every logged-in user, with no test catching it.
#
# These store a credential through the SAME path `fm login` uses
# (`login.store_payload`, mirroring tests/test_formist_cli_login.py) rather than
# hand-writing a JSON fixture, so the test fails the way a real login would.


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never the operator's ~/.config/evane. Also never a real operator's
    $FORMIST_TOKEN leaking from the runner's own shell into the "environment
    origin" tests below."""
    monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "ev-config"))
    monkeypatch.delenv(tokens.ENV_TOKEN_VAR, raising=False)


def _blank_report(env: str) -> EnvReport:
    return EnvReport.from_health(env, {"status": "ok", "checks": []})


def test_a_real_stored_credential_is_readable_not_unreadable():
    """RED on the pre-fix code: `tokens.describe("crst_at_realtoken")` raises
    `TokenError: stored credential is not a JWT (expected three segments)`, and
    `_with_credential` turned that into `credential="unreadable: ..."`."""
    login.store_payload(
        "local",
        {"access_token": "crst_at_realtoken", "refresh_token": "crst_rt_y",
         "scope": "evane:kb evane:cli", "expires_in": 3600},
        client_id="cid-1", client_name="Evane CLI · box",
    )
    report = cli._with_credential(_blank_report("local"), "local")
    assert report.credential is not None
    assert "unreadable" not in report.credential
    assert "valid for" in report.credential
    assert "crst_at_realtoken" not in report.credential, "must describe, never reveal"


def test_expired_credential_is_flagged_expired_not_unreadable():
    login.store_payload(
        "local",
        {"access_token": "crst_at_old", "refresh_token": "crst_rt_y",
         "scope": "evane:kb", "expires_in": 1},
        client_id="cid-1",
    )
    path = tokens.token_path("local")
    data = json.loads(path.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))

    report = cli._with_credential(_blank_report("local"), "local")
    assert report.credential is not None
    assert "EXPIRED" in report.credential
    assert "unreadable" not in report.credential


def test_no_stored_credential_says_so_plainly():
    report = cli._with_credential(_blank_report("local"), "127.0.0.1:8160")
    assert report.credential == "none stored (fm login --host 127.0.0.1:8160)"


def test_env_token_origin_is_reported_without_reading_any_file(monkeypatch):
    """$FORMIST_TOKEN wins over the file and is never decoded either: it is
    opaque too, and `_with_credential` must not try `tokens.load_meta` on it."""
    monkeypatch.setenv(tokens.ENV_TOKEN_VAR, "crst_at_fromenv")
    report = cli._with_credential(_blank_report("local"), "local")
    assert report.credential == f"environment ({tokens.ENV_TOKEN_VAR})"


def test_with_migrations_is_attempted_for_formist_not_just_global(monkeypatch):
    """`/admin/instance` is admin-gated, and `_with_migrations` used to skip it
    for every env but Global (`env != REGION_GLOBAL`): refusing Formist too,
    even though Formist is its own single-region deployment whose one host
    serves `/admin` locally (`formist_cli.targets.is_admin_env`, also used by
    `formist_cli/api.py::_check_admin_target` and `formist_cli/logs.py::require_admin_region`
    so the three gates cannot drift from each other again). AE must still be
    skipped: it is a real EVANE region behind the Global-only gate."""
    from formist_cli.targets import resolve

    calls: list[tuple[str, str]] = []

    def fake_admin_get(target, path, *, token=None, **kwargs):
        calls.append((target.env, path))
        return {"regions": {}}

    monkeypatch.setattr(cli.tokens, "load_for", lambda env: ("tok", "file"))
    monkeypatch.setattr(cli.api, "admin_get", fake_admin_get)

    base = _blank_report("x")

    ae_report = cli._with_migrations(base, resolve("evane-ae"), "ae")
    assert calls == []
    assert ae_report == base

    formist_report = cli._with_migrations(base, resolve("formist"), "formist")
    assert calls == [("formist", "/admin/instance")]
    assert formist_report.migrations == "in sync"
