"""Tests for ``fm alerts``: the CLI front end for ``GET /admin/alerts*``.

Covers ``canonical_subject``, ``render_alerts`` and ``render_conditions``'
own rendering rules: the pagination "MORE EXIST" notice, the "(resolved)"
tag, and the "NO (needs X)" unavailable-condition line.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import alerts
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")


# ===========================================================================
# canonical_subject
# ===========================================================================
def test_default_subject_is_list():
    assert alerts.canonical_subject(None) == "list"


def test_conditions_is_case_insensitive():
    assert alerts.canonical_subject(" Conditions ") == "conditions"


def test_an_unknown_subject_names_both_valid_ones():
    with pytest.raises(alerts.AlertsError) as exc:
        alerts.canonical_subject("history")
    msg = str(exc.value)
    assert "list" in msg and "conditions" in msg


# ===========================================================================
# render_alerts
# ===========================================================================
def test_render_alerts_with_none_matching_says_so_rather_than_printing_nothing():
    out = alerts.render_alerts({"counts": {"open": 0}, "alerts": []})
    assert "(no alerts match this filter)" in out


def test_render_alerts_counts_header_always_prints_all_four_severities():
    out = alerts.render_alerts({"counts": {"open": 2, "critical": 1}, "alerts": []})
    assert "open=2" in out and "critical=1" in out and "warning=0" in out and "info=0" in out


def test_render_alerts_tags_a_resolved_row_and_shows_its_title():
    out = alerts.render_alerts(
        {
            "counts": {"open": 0},
            "alerts": [
                {
                    "severity": "critical", "condition": "usage_over_limit",
                    "occurrences": 3, "last_seen_at": "2026-09-06T00:00:00Z",
                    "id": "a-1", "resolved": True, "title": "Space X over quota",
                }
            ],
        }
    )
    assert "(resolved)" in out
    assert "Space X over quota" in out


def test_render_alerts_an_open_row_is_not_tagged_resolved():
    out = alerts.render_alerts(
        {
            "counts": {"open": 1},
            "alerts": [
                {"severity": "warning", "condition": "c", "occurrences": 1,
                 "last_seen_at": "x", "id": "a-2", "resolved": False},
            ],
        }
    )
    assert "(resolved)" not in out


def test_render_alerts_more_exist_notice_names_the_paging_flags():
    alerts_rows = [
        {"severity": "info", "condition": "c", "occurrences": 1, "last_seen_at": "x", "id": f"a-{i}"}
        for i in range(5)
    ]
    out = alerts.render_alerts({"counts": {"open": 12}, "alerts": alerts_rows, "total": 12})
    assert "MORE EXIST" in out
    assert "--limit" in out and "--offset" in out


def test_render_alerts_a_complete_page_does_not_cry_wolf():
    alerts_rows = [
        {"severity": "info", "condition": "c", "occurrences": 1, "last_seen_at": "x", "id": "a-1"},
    ]
    out = alerts.render_alerts({"counts": {"open": 1}, "alerts": alerts_rows, "total": 1})
    assert "MORE EXIST" not in out


# ===========================================================================
# render_conditions
# ===========================================================================
def test_render_conditions_marks_an_unavailable_condition_with_its_requirement():
    out = alerts.render_conditions(
        {
            "deployment": {"region": "global", "env_name": "prod"},
            "alerts_enabled": True, "email_enabled": True, "admin_email_configured": True,
            "evaluation_interval_seconds": 300, "max_emails_per_hour": 10,
            "conditions": [
                {"key": "usage_over_limit", "category": "billing", "severity": "critical",
                 "available": True},
                {"key": "attendee_unreachable", "category": "infra", "severity": "warning",
                 "available": False, "requires": "ATTENDEE_API_BASE"},
            ],
        }
    )
    assert "yes" in out
    assert "NO (needs ATTENDEE_API_BASE)" in out


def test_render_conditions_shows_deployment_origin():
    out = alerts.render_conditions(
        {"deployment": {"region": "ae", "env_name": "prod-ae"}, "conditions": []}
    )
    assert "ae/prod-ae" in out


# ===========================================================================
# run: dispatch to the right endpoint + payload contract
# ===========================================================================
def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": None, "env": "global", "status": None, "severity": None,
        "condition": None, "region": None, "limit": None, "offset": None, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(payload, **overrides):
        calls: list[tuple] = []

        def _get(target, path, *, token, params=None):
            calls.append((path, params))
            return payload

        monkeypatch.setattr(alerts.api, "admin_get", _get)
        monkeypatch.setattr(
            alerts.context, "resolve",
            lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
        )
        rc = alerts.run(_args(**overrides))
        return rc, calls

    return _drive


def test_list_hits_the_plain_alerts_endpoint_with_filters(driven):
    rc, calls = driven(
        {"counts": {"open": 0}, "alerts": []},
        status="open", severity="critical", limit=10,
    )
    assert rc == 0
    assert calls == [("/admin/alerts", {"status": "open", "severity": "critical", "limit": "10"})]


def test_conditions_hits_the_conditions_endpoint(driven):
    rc, calls = driven({"conditions": []}, subject="conditions")
    assert rc == 0
    assert calls == [("/admin/alerts/conditions", None)]


def test_a_non_dict_payload_is_a_refusal(driven):
    with pytest.raises(alerts.AlertsError):
        driven(["not", "a", "dict"])
