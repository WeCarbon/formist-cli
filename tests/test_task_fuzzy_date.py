"""``task_fuzzy_date`` — a port of noddit's ``core/fuzzy-date.ts``.
Every case here was cross-checked against the real `nod date` command
against the same "today" (2026-11-06, a Friday); see the C1052 lane's
report for the exact transcript."""

from __future__ import annotations

from datetime import date

import pytest

from formist_cli import task_fuzzy_date as fd

TODAY = date(2026, 11, 6)  # a Friday


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("today", "2026-11-06"), ("tomorrow", "2026-11-07"), ("yesterday", "2026-11-05"),
        ("3d", "2026-11-09"), ("2w", "2026-11-20"), ("in 3 days", "2026-11-09"),
        ("3 days ago", "2026-11-03"), ("next week", "2026-11-09"), ("last month", "2026-10-01"),
        ("this quarter", "2026-10-01"), ("next mon", "2026-11-09"), ("last fri", "2026-10-30"),
        ("fri", "2026-11-06"), ("q3", "2026-07-01"), ("q1 2027", "2027-01-01"),
        ("2026-11-15", "2026-11-15"), ("11/15", "2026-11-15"), ("11/15/2026", "2026-11-15"),
        ("11-15", "2026-11-15"), ("nov 15", "2026-11-15"), ("15th nov", "2026-11-15"),
        ("nov 2027", "2027-11-01"), ("2027", "2027-01-01"), ("15", "2026-11-15"),
        ("明天", "2026-11-07"), ("后天", "2026-11-08"), ("大后天", "2026-11-09"),
        ("昨天", "2026-11-05"), ("前天", "2026-11-04"), ("下周", "2026-11-09"),
        ("上周", "2026-10-26"), ("下个月", "2026-12-01"), ("月初", "2026-11-01"),
        ("月底", "2026-11-30"), ("明年", "2027-01-01"), ("周三", "2026-11-11"),
        ("下周三", "2026-11-11"), ("上周五", "2026-10-30"), ("3天后", "2026-11-09"),
        ("3天前", "2026-11-03"), ("2周后", "2026-11-20"), ("2个月后", "2027-01-01"),
        ("11月15日", "2026-11-15"), ("11.15", "2026-11-15"), ("15号", "2026-11-15"),
    ],
)
def test_parse_fuzzy_date_matches_real_nod(raw, expected):
    assert fd.parse_fuzzy_date(raw, TODAY) == expected


def test_unparseable_returns_none():
    assert fd.parse_fuzzy_date("not a date at all", TODAY) is None
    assert fd.parse_fuzzy_date("", TODAY) is None


def test_invalid_calendar_day_is_rejected():
    assert fd.parse_fuzzy_date("2026-11-31", TODAY) is None  # November has 30 days
    assert fd.parse_fuzzy_date("2026-02-30", TODAY) is None


def test_resolve_due_is_end_of_day_resolve_until_is_start_of_day():
    due = fd.resolve_due("next week", TODAY)
    assert due.startswith("2026-11-09T23:59:59")
    until = fd.resolve_until("next week", TODAY)
    assert until.startswith("2026-11-09T00:00:00")


def test_parse_when_keeps_an_explicit_time():
    when = fd.parse_when("fri 2pm", TODAY)
    assert when.day == "2026-11-06"
    assert when.timed is True
    assert when.at.hour == 14


def test_split_when_separates_the_date_from_the_rest():
    when, rest = fd.split_when("fri Carol to confirm the invoice", TODAY)
    assert when.day == "2026-11-06"
    assert rest == "Carol to confirm the invoice"


def test_split_when_with_no_leading_date():
    when, rest = fd.split_when("Carol to confirm the invoice", TODAY)
    assert when is None
    assert rest == "Carol to confirm the invoice"


@pytest.mark.parametrize("raw,expected", [("3d", "3d"), ("2w", "2w"), ("3", "3d"), ("3天", "3d"), ("2周", "2w")])
def test_parse_every(raw, expected):
    assert fd.parse_every(raw) == expected


def test_parse_every_rejects_zero_or_garbage():
    assert fd.parse_every("0") is None
    assert fd.parse_every("abc") is None
