"""The view filter DSL — a port of noddit's ``core/view-defs.ts``'s
``parseFilter``/``filterText``/the ``Filter``/``Cond`` shape.

Terms separated by spaces all hold (AND); ``or`` separates alternatives;
a leading ``-`` negates a term. Examples: ``state:in_progress,waiting``,
``assignee:human``, ``is:startable``, ``-is:deferred``, ``due:7d``,
``title:"release notes"``.
"""

from __future__ import annotations

import re

COND_FIELDS = ("state", "assignee", "flag", "kind", "project", "author", "tag", "is", "due", "updated", "title")
IS_VALUES = (
    "startable", "deferred", "delayed", "blocks_ai", "brief", "touched",
    "finished", "archived", "stale", "root", "parent", "milestone",
)


class FilterError(ValueError):
    pass


#: ``Cond`` = ``{"field": str, "value": [str, ...]}``; ``Filter`` is that,
#: or ``{"any": [Filter, ...]}`` / ``{"all": [Filter, ...]}`` / ``{"not": Filter}``.
Filter = dict

_TOKEN_RE = re.compile(r'(-?[a-z_]+:"[^"]*"|\S+)', re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    return [m.group(1) for m in _TOKEN_RE.finditer(text.strip())]


def _parse_term(tok: str) -> Filter:
    negate = tok.startswith("-")
    body = tok[1:] if negate else tok
    i = body.find(":")
    if i <= 0:
        raise FilterError(f"not a filter term: {tok!r}")
    field = body[:i].lower()
    if field not in COND_FIELDS:
        raise FilterError(f"unknown filter field {field!r} (one of: {' '.join(COND_FIELDS)})")
    raw = body[i + 1:]
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1]
    value = [raw] if field == "title" else [v.strip() for v in raw.split(",") if v.strip()]
    if not value:
        raise FilterError(f"not a filter term: {tok!r}")
    if field == "is":
        for v in value:
            if v not in IS_VALUES:
                raise FilterError(f"is: has no {v!r} (one of: {' '.join(IS_VALUES)})")
    cond: Filter = {"field": field, "value": value}
    return {"not": cond} if negate else cond


def parse_filter(text: str) -> Filter | None:
    tokens = _tokenize(text)
    if not tokens:
        return None
    groups: list[list[Filter]] = [[]]
    for tok in tokens:
        if tok.lower() == "or":
            if not groups[-1]:
                raise FilterError('"or" with nothing before it')
            groups.append([])
            continue
        groups[-1].append(_parse_term(tok))
    if not groups[-1]:
        raise FilterError('"or" with nothing after it')
    ands = [g[0] if len(g) == 1 else {"all": g} for g in groups]
    return ands[0] if len(ands) == 1 else {"any": ands}


def filter_text(f: Filter | None) -> str:
    if not f:
        return ""
    if "any" in f:
        return " or ".join(filter_text(x) for x in f["any"])
    if "all" in f:
        return " ".join(f"({filter_text(x)})" if "any" in x else filter_text(x) for x in f["all"])
    if "not" in f:
        return f"-{filter_text(f['not'])}"
    v = f"\"{f['value'][0]}\"" if f["field"] == "title" else ",".join(f["value"])
    return f"{f['field']}:{v}"
