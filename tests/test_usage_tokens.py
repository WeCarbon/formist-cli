"""``fm usage-tokens``: the CLI front end for ``GET /admin/usage/tokens``.

WHAT THIS FILE GUARDS. This module had ZERO test coverage before this file.
The module's own docstring names the defect class it exists to prevent: "5,458
runaway calls once read as costing nothing for five days because a meter's zero
meant unknown and nothing said so." ``_row`` is the renderer responsible for
that: it must print the LOWER BOUND / measured and priced / LOWER BOUND labels
on EVERY row, not only the ones that need it (an operator scanning a column for
the one bad row would miss a flag that only sometimes appears).

Run: uv run pytest tests/test_formist_cli_usage_tokens.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import usage_tokens
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")


def _bucket(**over) -> dict:
    base = {
        "calls": 10, "total_tokens": 5000, "estimated_cost_usd": 1.23,
        "unmeasured_calls": 0, "unpriced_calls": 0, "failed_calls": 0,
        "tokens_are_lower_bound": False, "cost_is_lower_bound": False,
    }
    return base | over


# ===========================================================================
# render / _row: shape 2: the label is a PROPERTY of every row, not a case
# ===========================================================================
@pytest.mark.parametrize("flag_field,label_present,label_absent", [
    ("tokens_are_lower_bound", "LOWER BOUND", "measured"),
])
def test_a_lower_bound_row_is_labelled_lower_bound_not_measured(flag_field, label_present, label_absent):
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": ["global"],
         "totals": _bucket(**{flag_field: True}), "groups": []}
    )
    assert label_present in out


def _total_line(out: str) -> str:
    return next(ln for ln in out.split("\n") if ln.startswith("TOTAL"))


def test_a_measured_row_is_labelled_measured_not_lower_bound():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": ["global"],
         "totals": _bucket(tokens_are_lower_bound=False), "groups": []}
    )
    assert "measured" in out
    assert "LOWER BOUND" not in _total_line(out)


def test_cost_lower_bound_is_independent_of_tokens_lower_bound():
    """The two flags are DIFFERENT facts (measured tokens, unpriced cost): a
    renderer that conflates them would call an unpriced-but-measured row
    entirely trustworthy or entirely not, both wrong."""
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": ["global"],
         "totals": _bucket(tokens_are_lower_bound=False, cost_is_lower_bound=True),
         "groups": []}
    )
    total_line = _total_line(out)
    assert "(measured)" in total_line
    assert "(LOWER BOUND)" in total_line  # the cost half


def test_unmeasured_and_unpriced_and_failed_counts_are_always_printed():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": ["global"],
         "totals": _bucket(unmeasured_calls=3, unpriced_calls=2, failed_calls=1),
         "groups": []}
    )
    assert "unmeasured=3" in out and "unpriced=2" in out and "failed=1" in out


def test_meter_disabled_warns_every_number_is_stale():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": [],
         "meter_enabled": False, "totals": _bucket(), "groups": []}
    )
    assert "TOKEN_METER_ENABLED is OFF" in out


def test_meter_enabled_prints_no_warning():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "source", "regions": [],
         "meter_enabled": True, "totals": _bucket(), "groups": []}
    )
    assert "TOKEN_METER_ENABLED" not in out


def test_no_rows_in_window_says_so_rather_than_printing_nothing():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "model", "regions": [],
         "totals": _bucket(calls=0, total_tokens=0), "groups": []}
    )
    assert "(no rows in this window)" in out


def test_a_group_row_prefers_label_falls_back_to_key_then_unattributed():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "model", "regions": [],
         "totals": _bucket(), "groups": [
             {"label": "gpt-4o", **_bucket()},
             {"key": "no-label-key", **_bucket()},
             {**_bucket()},
         ]}
    )
    assert "gpt-4o" in out
    assert "no-label-key" in out
    assert "(unattributed)" in out


def test_truncated_groups_name_the_flag_that_gets_the_rest():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "model", "regions": [],
         "totals": _bucket(), "groups": [{"label": "a", **_bucket()}],
         "truncated": True, "group_count": 50}
    )
    assert "MORE" in out and "EXIST" in out and "--limit" in out


def test_not_truncated_does_not_mention_more_groups():
    out = usage_tokens.render(
        {"since": "s", "until": "u", "group_by": "model", "regions": [],
         "totals": _bucket(), "groups": [{"label": "a", **_bucket()}],
         "truncated": False}
    )
    assert "MORE" not in out


# ===========================================================================
# run: every optional filter is forwarded, group_by defaults to source
# ===========================================================================
def _args(**overrides) -> argparse.Namespace:
    base = {
        "env": "global", "group_by": None, "since": None, "until": None,
        "space_id": None, "user_id": None, "source": None, "operation": None,
        "model": None, "provider": None, "limit": None, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(payload, **overrides):
        calls: list[tuple] = []

        def _get(target, path, *, token, params=None):
            calls.append((path, params))
            return payload

        monkeypatch.setattr(usage_tokens.api, "admin_get", _get)
        monkeypatch.setattr(
            usage_tokens.context, "resolve",
            lambda args, **kw: Context(target=GLOBAL, space=None, token="t", space_source="flag"),
        )
        rc = usage_tokens.run(_args(**overrides))
        return rc, calls

    return _drive


def test_group_by_defaults_to_source_when_unset(driven):
    rc, calls = driven({"since": "s", "until": "u", "group_by": "source", "regions": [],
                         "totals": _bucket(), "groups": []})
    assert rc == 0
    assert calls[0] == ("/admin/usage/tokens", {"group_by": "source"})


def test_every_optional_filter_is_forwarded(driven):
    _rc, calls = driven(
        {"since": "s", "until": "u", "group_by": "model", "regions": [], "totals": _bucket(), "groups": []},
        group_by="model", since="2026-09-01", space_id="sp-1", model="gpt-4o", limit=5,
    )
    assert calls[0] == (
        "/admin/usage/tokens",
        {"group_by": "model", "since": "2026-09-01", "space_id": "sp-1", "model": "gpt-4o", "limit": "5"},
    )


def test_a_non_dict_payload_is_a_refusal(driven):
    with pytest.raises(usage_tokens.UsageTokensError):
        driven("not a dict")
