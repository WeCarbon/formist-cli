"""Loose date input -> a calendar day — a port of noddit's
``core/fuzzy-date.ts``. Used by ``fm task add/ask --due``, ``defer/hold
--until``, ``edit --remind``, and ``fm task date``'s preview.

Only the day matters here (local time): a month, quarter or year alone
means its first day. See the TS source's module docstring for the full
list of accepted English/Chinese forms — transcribed as regexes below,
not re-derived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_WEEKDAYS = {
    "sun": 0, "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 3, "weds": 3, "wednesday": 3, "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fri": 5, "friday": 5, "sat": 6, "saturday": 6,
}
_MONTHS = {
    "jan": 0, "january": 0, "feb": 1, "february": 1, "mar": 2, "march": 2, "apr": 3, "april": 3,
    "may": 4, "jun": 5, "june": 5, "jul": 6, "july": 6, "aug": 7, "august": 7, "sep": 8, "sept": 8,
    "september": 8, "oct": 9, "october": 9, "nov": 10, "november": 10, "dec": 11, "december": 11,
}
_ZH_WEEKDAY = {"日": 0, "天": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_ZH_DIGIT = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WD = "|".join(_WEEKDAYS)
_MO = "|".join(_MONTHS)
_ORD = r"(?:st|nd|rd|th)?"
_ZH_N = r"(\d+|[一二两三四五六七八九十]+)"
_TRAD_MAP = {"週": "周", "後": "后", "個": "个", "禮": "礼", "兩": "两", "號": "号", "這": "这"}

#: Distinguishes "not a Chinese-shaped token at all" from "looked Chinese
#: but didn't parse" — the TS source's three-valued `string | null |
#: undefined` return from `chinese()`.
_NOT_CHINESE = object()


def _day_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def _add_months(d: date, n: int) -> date:
    total = d.month - 1 + n
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _valid(y: int, mo0: int, da: int) -> str | None:
    """``mo0`` is 0-indexed (JS convention); ``None`` for an impossible day
    (no Nov 31) — Python's own ``date()`` already refuses it, so there is
    no rollover to detect the way the JS ``Date`` constructor needs."""
    try:
        return _day_key(date(y, mo0 + 1, da))
    except ValueError:
        return None


def _weekday_sun0(d: date) -> int:
    """Python's ``date.weekday()`` is Monday=0; the TS source's
    ``getDay()`` is Sunday=0 — this is that conversion."""
    return (d.weekday() + 1) % 7


def _until_weekday(now: date, target: int, inclusive: bool) -> int:
    delta = (target - _weekday_sun0(now)) % 7
    return 0 if (delta == 0 and inclusive) else (delta or 7)


def _since_weekday(now: date, target: int) -> int:
    delta = (_weekday_sun0(now) - target) % 7
    return delta or 7


def _upcoming_year(now: date, mo0: int) -> int:
    return now.year + 1 if mo0 < (now.month - 1) else now.year


def _zh_number(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    m = re.match(r"^([一二两三四五六七八九])?十([一二三四五六七八九])?$", s)
    if m:
        return (_ZH_DIGIT[m.group(1)] if m.group(1) else 1) * 10 + (_ZH_DIGIT[m.group(2)] if m.group(2) else 0)
    return _ZH_DIGIT.get(s) if len(s) == 1 else None


def _start_of_week(now: date) -> date:
    return _add_days(now, -((_weekday_sun0(now) + 6) % 7))


def _from_monday(weekday: int) -> int:
    return (weekday + 6) % 7


def _chinese(s: str, now: date):
    words = {
        "今天": now, "今日": now,
        "明天": _add_days(now, 1), "明日": _add_days(now, 1),
        "后天": _add_days(now, 2), "大后天": _add_days(now, 3),
        "昨天": _add_days(now, -1), "昨日": _add_days(now, -1), "前天": _add_days(now, -2),
        "下周": _add_days(_start_of_week(now), 7), "下星期": _add_days(_start_of_week(now), 7),
        "上周": _add_days(_start_of_week(now), -7), "上星期": _add_days(_start_of_week(now), -7),
        "下个月": _add_months(now, 1), "下月": _add_months(now, 1),
        "月初": date(now.year, now.month, 1),
        "月底": _add_days(_add_months(now, 1), -1),
        "月末": _add_days(_add_months(now, 1), -1),
        "明年": date(now.year + 1, 1, 1),
    }
    if s in words:
        return _day_key(words[s])

    m = re.match(r"^(下|上|这|本)?\s*(?:周|星期|礼拜)([一二三四五六日天])$", s)
    if m:
        offset = _from_monday(_ZH_WEEKDAY[m.group(2)])
        if m.group(1) == "下":
            return _day_key(_add_days(_start_of_week(now), 7 + offset))
        if m.group(1) == "上":
            return _day_key(_add_days(_start_of_week(now), -7 + offset))
        if m.group(1):
            return _day_key(_add_days(_start_of_week(now), offset))
        return _day_key(_add_days(now, _until_weekday(now, _ZH_WEEKDAY[m.group(2)], True)))

    relative = [
        (re.compile(rf"^{_ZH_N}\s*(?:天|日)(?:后|之后|以后)$"), lambda n: _add_days(now, n)),
        (re.compile(rf"^{_ZH_N}\s*(?:天|日)前$"), lambda n: _add_days(now, -n)),
        (re.compile(rf"^{_ZH_N}\s*(?:周|个?星期|个?礼拜)(?:后|之后|以后)$"), lambda n: _add_days(now, n * 7)),
        (re.compile(rf"^{_ZH_N}\s*个?月(?:后|之后|以后)$"), lambda n: _add_months(now, n)),
    ]
    for pat, at in relative:
        m = pat.match(s)
        if m:
            n = _zh_number(m.group(1))
            return None if n is None else _day_key(at(n))

    m = re.match(r"^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?$", s)
    if m:
        mo = int(m.group(2)) - 1
        return _valid(int(m.group(1)) if m.group(1) else _upcoming_year(now, mo), mo, int(m.group(3)))
    m = re.match(r"^(\d{1,2})[日号]$", s)
    if m:
        return _valid(now.year, now.month - 1, int(m.group(1)))
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        mo = int(m.group(1)) - 1
        return _valid(_upcoming_year(now, mo), mo, int(m.group(2)))
    return _NOT_CHINESE


def _english(s: str, now: date) -> str | None:
    if s in ("today", "tod", "now"):
        return _day_key(now)
    if s in ("tomorrow", "tom", "tmr", "tmrw"):
        return _day_key(_add_days(now, 1))
    if s in ("yesterday", "yest", "yda"):
        return _day_key(_add_days(now, -1))
    m = re.match(r"^\+?(?:in )?(\d+) ?(d|w)$", s)
    if m:
        return _day_key(_add_days(now, int(m.group(1)) * (7 if m.group(2) == "w" else 1)))
    m = re.match(r"^(?:in )?(\d+) days?(?: (?:later|from now|ahead))?$", s)
    if m:
        return _day_key(_add_days(now, int(m.group(1))))
    m = re.match(r"^(\d+) days? ago$", s)
    if m:
        return _day_key(_add_days(now, -int(m.group(1))))
    m = re.match(r"^(?:in )?(\d+) weeks?(?: (?:later|from now))?$", s)
    if m:
        return _day_key(_add_days(now, 7 * int(m.group(1))))
    m = re.match(r"^(\d+) weeks? ago$", s)
    if m:
        return _day_key(_add_days(now, -7 * int(m.group(1))))
    m = re.match(r"^(?:in )?(\d+) months?(?: (?:later|from now))?$", s)
    if m:
        return _day_key(_add_months(now, int(m.group(1))))
    m = re.match(r"^(\d+) months? ago$", s)
    if m:
        return _day_key(_add_months(now, -int(m.group(1))))
    m = re.match(r"^(next|last|this) (week|month|quarter|year)$", s)
    if m:
        dir_ = 1 if m.group(1) == "next" else (-1 if m.group(1) == "last" else 0)
        unit = m.group(2)
        if unit == "week":
            return _day_key(_add_days(_start_of_week(now), dir_ * 7))
        if unit == "month":
            return _day_key(_add_months(now, dir_))
        if unit == "quarter":
            return _day_key(date(now.year, (now.month - 1) // 3 * 3 + dir_ * 3 + 1, 1))
        return _day_key(date(now.year + dir_, 1, 1))
    m = re.match(rf"^(next|last|this) ({_WD})$", s)
    if m:
        target = _WEEKDAYS[m.group(2)]
        if m.group(1) == "next":
            return _day_key(_add_days(now, _until_weekday(now, target, False)))
        if m.group(1) == "last":
            return _day_key(_add_days(now, -_since_weekday(now, target)))
        return _day_key(_add_days(_start_of_week(now), _from_monday(target)))
    if s in _WEEKDAYS:
        return _day_key(_add_days(now, _until_weekday(now, _WEEKDAYS[s], True)))
    m = re.match(r"^q([1-4])(?: (\d{4}))?$", s)
    if m:
        year = int(m.group(2)) if m.group(2) else now.year
        return _day_key(date(year, (int(m.group(1)) - 1) * 3 + 1, 1))
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", s)
    if m:
        mo = int(m.group(1)) - 1
        if m.group(3):
            y = int(f"20{m.group(3)}") if len(m.group(3)) == 2 else int(m.group(3))
        else:
            y = _upcoming_year(now, mo)
        return None if mo < 0 or mo > 11 else _valid(y, mo, int(m.group(2)))
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m:
        mo = int(m.group(1)) - 1
        return None if mo < 0 or mo > 11 else _valid(_upcoming_year(now, mo), mo, int(m.group(2)))
    m = re.match(rf"^({_MO})\.? (\d{{1,2}}){_ORD}(?:,? (\d{{4}}))?$", s)
    if m:
        mo = _MONTHS[m.group(1)]
        return _valid(int(m.group(3)) if m.group(3) else _upcoming_year(now, mo), mo, int(m.group(2)))
    m = re.match(rf"^(\d{{1,2}}){_ORD} ({_MO})\.?(?:,? (\d{{4}}))?$", s)
    if m:
        mo = _MONTHS[m.group(2)]
        return _valid(int(m.group(3)) if m.group(3) else _upcoming_year(now, mo), mo, int(m.group(1)))
    m = re.match(rf"^({_MO})(?: (\d{{4}}))?$", s)
    if m:
        mo = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else _upcoming_year(now, mo)
        return _day_key(date(year, mo + 1, 1))
    m = re.match(r"^(\d{4})$", s)
    if m:
        return _day_key(date(int(m.group(1)), 1, 1))
    m = re.match(rf"^(\d{{1,2}}){_ORD}$", s)
    if m:
        return _valid(now.year, now.month - 1, int(m.group(1)))
    return None


def parse_fuzzy_date(raw: str, now: date | None = None) -> str | None:
    """The day ``raw`` means, as ``YYYY-MM-DD``; ``None`` when it can't be
    read."""
    now = now or date.today()
    s = re.sub(r"\s+", " ", raw.strip().lower())
    s = "".join(_TRAD_MAP.get(c, c) for c in s)
    if not s:
        return None
    zh = _chinese(s, now)
    return _english(s, now) if zh is _NOT_CHINESE else zh


def until_day(stamp: str) -> str | None:
    """The local day an ISO stamp falls on; ``None`` if it doesn't parse."""
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _day_key(at.date())


def start_of_day(key: str) -> datetime:
    y, mo, da = (int(x) for x in key.split("-"))
    return datetime(y, mo, da)


def days_from_today(key: str, now: date | None = None) -> int:
    now = now or date.today()
    return (start_of_day(key).date() - now).days


_TIME_PM_WORDS = ("下午", "晚上")


def _parse_time_token(tok: str) -> tuple[int, int] | None:
    m = re.match(r"^([01]?\d|2[0-3])[:：]([0-5]\d)$", tok)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)$", tok, re.IGNORECASE)
    if m:
        h = int(m.group(1)) % 12
        return (h + 12 if m.group(3).lower() == "pm" else h), (int(m.group(2)) if m.group(2) else 0)
    m = re.match(r"^(上午|下午|早上|晚上)?([01]?\d|2[0-3])点([0-5]?\d)?分?$", tok)
    if m:
        h = int(m.group(2))
        if h > 23:
            return None
        pm = m.group(1) in _TIME_PM_WORDS
        return (h + 12 if (pm and h < 12) else h), (int(m.group(3)) if m.group(3) else 0)
    return None


def _split_time_of_day(raw: str) -> tuple[str, tuple[int, int]] | None:
    sp = raw.rfind(" ")
    if sp > 0:
        time = _parse_time_token(raw[sp + 1:].strip())
        if time:
            return raw[:sp].strip(), time
    glued = re.match(
        r"^(.+?)((?:上午|下午|早上|晚上)?(?:[01]?\d|2[0-3])(?:点[0-5]?\d?分?|[：:][0-5]\d))$", raw,
    )
    if glued:
        time = _parse_time_token(glued.group(2))
        if time:
            return glued.group(1), time
    return None


@dataclass
class When:
    day: str
    at: datetime
    timed: bool


def parse_when(raw: str, now: date | None = None) -> When | None:
    """A full ISO date-time is taken as given; a loose day with a time of
    day on it keeps that time; a bare loose day means its start."""
    now = now or date.today()
    s = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}", s):
        try:
            at = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
        return When(day=_day_key(at.date()), at=at, timed=True)

    split = _split_time_of_day(s)
    if split:
        date_part, (h, mi) = split
        day = parse_fuzzy_date(date_part, now)
        if day:
            base = start_of_day(day)
            return When(day=day, at=base.replace(hour=h, minute=mi), timed=True)

    day = parse_fuzzy_date(s, now)
    return When(day=day, at=start_of_day(day), timed=False) if day else None


def split_when(raw: str, now: date | None = None) -> tuple[When | None, str]:
    """``"fri Carol to confirm"`` -> that day, then what it waits on."""
    words = raw.strip().split()
    for n in range(min(len(words), 4), 0, -1):
        when = parse_when(" ".join(words[:n]), now)
        if when:
            return when, " ".join(words[n:])
    return None, " ".join(words)


def parse_every(raw: str) -> str | None:
    """``--every``: ``3d``/``2w``/``3``/``3天``/``2周`` -> ``3d``/``2w``."""
    m = re.match(r"^\s*(\d+)\s*(d|days?|天|w|weeks?|周|週)?\s*$", raw, re.IGNORECASE)
    if not m or int(m.group(1)) < 1:
        return None
    is_week = bool(re.match(r"^(w|week|weeks|周|週)$", m.group(2) or "", re.IGNORECASE))
    return f"{int(m.group(1))}w" if is_week else f"{int(m.group(1))}d"


def every_until(every: str, now: date | None = None) -> date:
    """「延迟 R 天」: the start of the day R after today."""
    now = now or date.today()
    r = parse_every(every)
    n = (int(r[:-1]) * (7 if r.endswith("w") else 1)) if r else 1
    return _add_days(now, n)


#: Ready-made picks offered while the owner types; each is a phrase
#: `parse_fuzzy_date` reads.
DATE_SUGGESTIONS = [
    {"key": "tomorrow", "phrase": "tomorrow", "hints": ["tomorrow", "tom", "tmr", "明天", "明", "demain", "غدا"]},
    {"key": "dayAfter", "phrase": "后天", "hints": ["后天", "後天", "day after", "après-demain"]},
    {"key": "nextMonday", "phrase": "下周一", "hints": ["next mon", "next week", "monday", "下周", "下周一", "周一", "下週", "lundi"]},
    {"key": "inAWeek", "phrase": "7d", "hints": ["week", "1 week", "7", "7d", "一周", "一週", "semaine"]},
    {"key": "nextMonth", "phrase": "next month", "hints": ["next month", "month", "下个月", "下月", "下個月", "mois"]},
]


def to_iso_with_offset(dt: datetime) -> str:
    """A naive local datetime, stamped with this machine's current UTC
    offset — the same convention ``task_store.now_stamp`` uses, so a
    resolved ``--due``/``--until``/``--remind`` reads back exactly like
    any other timestamp this tool writes."""
    import time as _time

    offset_seconds = -_time.timezone if _time.localtime().tm_isdst == 0 else -_time.altzone
    sign = "+" if offset_seconds >= 0 else "-"
    off = abs(offset_seconds)
    oh, om = divmod(off // 60, 60)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}{sign}{oh:02d}:{om:02d}"


def resolve_due(raw: str, now: date | None = None) -> str | None:
    """``--due``: a bare day means the END of that day (23:59:59) — an
    explicit time on it is kept as given."""
    when = parse_when(raw, now)
    if when is None:
        return None
    at = when.at if when.timed else when.at.replace(hour=23, minute=59, second=59)
    return to_iso_with_offset(at)


def resolve_until(raw: str, now: date | None = None) -> str | None:
    """``--until``/``--remind``: a bare day means the START of that day —
    an explicit time on it is kept as given."""
    when = parse_when(raw, now)
    return None if when is None else to_iso_with_offset(when.at)


def suggest_dates(query: str, now: date | None = None, limit: int = 5) -> list[dict]:
    now = now or date.today()
    q = query.strip().lower()
    out: list[dict] = []
    parsed = parse_fuzzy_date(q, now)
    if parsed:
        out.append({"key": "parsed", "date": parsed, "parsed": True})
    for s in DATE_SUGGESTIONS:
        if len(out) >= limit:
            break
        if q and not any(h in q or q in h for h in s["hints"]):
            continue
        d = parse_fuzzy_date(s["phrase"], now)
        if d and not any(o["date"] == d for o in out):
            out.append({"key": s["key"], "date": d, "parsed": False})
    return out
