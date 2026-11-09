"""Views — a port of noddit's ``core/view-defs.ts``: the 6 built-ins, the
filter DSL's Node-aware evaluator, and ``$NODDIT_HOME/views.json``
storage shared with `nod` (same file, same format — a view either tool
adds shows up in the other).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from formist_cli import task_filter
from formist_cli.task_filter import Filter
from formist_cli.task_model import Node
from formist_cli.task_status import is_deferred, is_startable, is_terminal

LAYOUTS = ("tree", "list")
GROUPS = ("project", "state", "assignee", "none")
SHOW = ("hide", "show", "fold")
SORT_FIELDS = (
    "bench", "blocks_ai", "flag", "manual", "urgency", "due", "created", "updated",
    "check_back", "waiting_since", "touched", "changed", "id", "title", "state",
)


@dataclass
class ViewDef:
    id: str
    layout: str = "list"
    name: str | dict | None = None
    icon: str | None = None
    filter: Filter | None = None
    group: str | None = None
    sort: list[str] = field(default_factory=list)
    finished: str = "hide"
    deferred: str = "hide"
    window: dict | None = None
    count: bool = True


def _f(text: str) -> Filter | None:
    return task_filter.parse_filter(text)


#: The built-ins, in tab order — what `view reset` restores. Filter
#: strings transcribed verbatim from `view-defs.ts`'s `BUILTIN_VIEWS`.
BUILTIN_VIEWS: list[ViewDef] = [
    ViewDef(
        id="decide", icon="hand", layout="list", group="project", sort=["blocks_ai", "flag", "bench"],
        filter=_f(
            "state:needs_decision -is:milestone or assignee:human state:in_progress,waiting,decided "
            "or assignee:human state:not_started is:startable"
        ),
    ),
    ViewDef(
        id="available", icon="list-todo", layout="tree", deferred="show",
        filter=_f("state:in_progress,waiting,needs_decision,decided,not_started -is:milestone"),
    ),
    ViewDef(
        id="active", icon="activity", layout="tree",
        filter=_f("state:in_progress,needs_decision,decided -is:milestone or state:not_started is:startable -is:milestone"),
    ),
    ViewDef(
        id="waiting", icon="hourglass", layout="list", group="none",
        sort=["check_back", "flag", "waiting_since"], deferred="show",
        filter=_f("state:waiting or is:delayed"),
    ),
    ViewDef(
        id="live", icon="radio", layout="list", group="none", sort=["changed"],
        finished="show", deferred="show", window={"days": 30, "max": 200}, count=False,
    ),
    ViewDef(id="all", icon="list-tree", layout="tree", finished="fold", deferred="show", count=False),
    ViewDef(
        id="external", icon="send", layout="list", group="project", sort=["updated"],
        finished="show", filter=_f("tag:external"),
    ),
]
BUILTIN_IDS = [v.id for v in BUILTIN_VIEWS]
_RENAMED = {"recent": "live"}


def view_id(raw: str) -> str:
    return _RENAMED.get(raw, raw)


def builtin_view(id_: str) -> ViewDef | None:
    vid = view_id(id_)
    return next((v for v in BUILTIN_VIEWS if v.id == vid), None)


# -- the Node-aware evaluator --------------------------------------------------


def _parse_epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_span_seconds(raw: str) -> float | None:
    from formist_cli.task_duration import DurationError, parse_duration

    try:
        return parse_duration(raw) / 1000
    except DurationError:
        return None


def _is_value(node: Node, value: str, all_nodes: list[Node]) -> bool:
    from formist_cli import task_json
    from formist_cli.task_intake import brief_state
    from formist_cli.task_status import waiting_on_owner

    if value == "startable":
        return is_startable(node, all_nodes)
    if value == "deferred":
        # "itself or under a deferred parent" — walk up the tree.
        by_id = {n.id: n for n in all_nodes}
        cur = node
        depth = 0
        while cur is not None and depth < 64:
            if is_deferred(cur):
                return True
            cur = by_id.get(cur.parent) if cur.parent is not None else None
            depth += 1
        return False
    if value == "delayed":
        from formist_cli.task_status import delay_of

        return delay_of(node) is not None
    if value == "blocks_ai":
        return waiting_on_owner(node, all_nodes)
    if value == "brief":
        return brief_state(node) is not None
    if value == "touched":
        return node.updated_at is not None
    if value == "finished":
        return is_terminal(node.state)
    if value == "archived":
        return node.archived
    if value == "stale":
        return task_json.staleness_of(node) == "stale"
    if value == "root":
        return node.parent is None
    if value == "parent":
        return bool(node.children)
    if value == "milestone":
        return bool(node.milestone)
    return False


def matches(f: Filter | None, node: Node, all_nodes: list[Node], now: float | None = None) -> bool:
    if f is None:
        return True
    if "any" in f:
        return any(matches(x, node, all_nodes, now) for x in f["any"])
    if "all" in f:
        return all(matches(x, node, all_nodes, now) for x in f["all"])
    if "not" in f:
        return not matches(f["not"], node, all_nodes, now)

    field_, values = f["field"], f["value"]
    now = now if now is not None else datetime.now(timezone.utc).timestamp()

    if field_ == "state":
        return node.state in values
    if field_ == "assignee":
        want = {v.lower() for v in values}
        actual = "human" if (node.assignee and node.assignee != "ai") else "ai"
        return actual in want
    if field_ == "flag":
        important = node.urgency == "high"
        milestone = bool(node.milestone)
        for v in values:
            if v == "important" and important:
                return True
            if v == "milestone" and milestone:
                return True
            if v == "flagged" and (important or milestone):
                return True
            if v == "none" and not important and not milestone:
                return True
        return False
    if field_ == "kind":
        return node.kind in values
    if field_ == "project":
        return node.project in values
    if field_ == "author":
        return any(v.lower() in (node.author or "").lower() for v in values)
    if field_ == "tag":
        return any(v in node.tags for v in values)
    if field_ == "is":
        return any(_is_value(node, v, all_nodes) for v in values)
    if field_ == "due":
        if not node.due:
            return False
        due_t = _parse_epoch(node.due)
        span = _parse_span_seconds(values[0])
        return due_t is not None and span is not None and due_t <= now + span
    if field_ == "updated":
        at_t = _parse_epoch(node.updated_at or node.created)
        span = _parse_span_seconds(values[0])
        return at_t is not None and span is not None and at_t >= now - span
    if field_ == "title":
        return values[0].lower() in (node.title or "").lower()
    return False


def apply_view(nodes: list[Node], view: ViewDef | str) -> list[Node]:
    """``nod bench --view <id>`` — one of the 7 named views (the 6
    built-ins or a saved custom one), NOT `nod show`'s scope vocabulary
    (see :func:`apply_show_scope`, a different command with different
    words that happen to overlap on "active"/"all")."""
    v = view if isinstance(view, ViewDef) else find_view(view)
    if v is None:
        raise ValueError(f"unknown view: {view!r} (one of: {' '.join(BUILTIN_IDS)}, or a saved custom view)")
    out = [n for n in nodes if matches(v.filter, n, nodes)]
    if v.finished == "hide":
        out = [n for n in out if not is_terminal(n.state)]
    if v.deferred == "hide":
        out = [n for n in out if not is_deferred(n)]
    return out


#: `nod show`'s scope vocabulary (cli.json's `show` help) — a DIFFERENT,
#: older vocabulary from the 7 view ids above (`nod bench`'s tabs): a
#: scope here is "open" by default, and "active"/"all" mean something
#: specific to THIS command even though the words overlap with view ids.
SHOW_SCOPES = (
    "open", "answered", "active", "archived", "all", "fyi", "deferred",
    "flagged", "due", "remind",
)


def apply_show_scope(nodes: list[Node], scope: str = "open", stale: bool = False) -> list[Node]:
    """``nod show [scope] [--stale]`` — verified against the real command
    for every scope name (C1137)."""
    from formist_cli.task_model import ITEM_STATUSES

    if scope == "open":
        out = [n for n in nodes if n.state == "needs_decision"]
    elif scope == "answered":
        out = [n for n in nodes if n.state == "decided"]
    elif scope == "active":
        out = [n for n in nodes if not n.archived]
    elif scope == "archived":
        # Verified against real nod: an fyi node (always filed straight
        # into archived/) does NOT show up here — "archived" means done
        # real work, not a notice that was never active to begin with.
        out = [n for n in nodes if n.archived and n.kind != "fyi"]
    elif scope == "all":
        out = list(nodes)
    elif scope == "fyi":
        out = [n for n in nodes if n.kind == "fyi"]
    elif scope == "deferred":
        out = [n for n in nodes if is_deferred(n)]
    elif scope == "flagged":
        out = [n for n in nodes if n.urgency == "high" or n.milestone]
    elif scope == "due":
        out = [n for n in nodes if n.due and not n.archived]
    elif scope == "remind":
        out = [n for n in nodes if n.remind_at and not n.archived]
    elif scope in ITEM_STATUSES:
        out = [n for n in nodes if n.state == scope]
    else:
        raise ValueError(f"unknown scope: {scope!r} (one of: {' '.join(SHOW_SCOPES)}, or a status)")

    if stale:
        from formist_cli import task_json

        out = [n for n in out if n.state == "in_progress" and task_json.staleness_of(n) == "stale"]
    return out


def find_view(id_: str, path: str | None = None) -> ViewDef | None:
    vid = view_id(id_)
    return next((v for v in load_views(path) if v.id == vid), None)


# -- storage: $NODDIT_HOME/views.json, shared with `nod` -----------------------


def _noddit_env(name: str) -> str | None:
    return os.environ.get(f"NODDIT_{name}") or os.environ.get(f"DECS_{name}")


def views_path() -> str:
    explicit = _noddit_env("VIEWS")
    if explicit:
        return explicit
    home = _noddit_env("HOME") or os.path.join(os.path.expanduser("~"), ".decs")
    return os.path.join(home, "views.json")


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def validate_view(raw: dict) -> ViewDef:
    if not isinstance(raw, dict):
        raise ValueError("a view definition must be an object")
    vid = view_id(str(raw.get("id") or ""))
    if not _ID_RE.match(vid):
        raise ValueError(f"bad view id: {vid!r}")
    layout = raw.get("layout", "list")
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}")
    out = ViewDef(id=vid, layout=layout)
    if raw.get("name") is not None:
        out.name = raw["name"]
    if raw.get("icon") is not None:
        out.icon = str(raw["icon"])
    if raw.get("filter"):
        out.filter = task_filter.parse_filter(raw["filter"]) if isinstance(raw["filter"], str) else raw["filter"]
    if raw.get("group") is not None:
        if raw["group"] not in GROUPS:
            raise ValueError(f"group must be one of {GROUPS}")
        out.group = raw["group"]
    if raw.get("finished") is not None:
        if raw["finished"] not in SHOW:
            raise ValueError(f"finished must be one of {SHOW}")
        out.finished = raw["finished"]
    if raw.get("deferred") is not None:
        if raw["deferred"] not in SHOW:
            raise ValueError(f"deferred must be one of {SHOW}")
        out.deferred = raw["deferred"]
    if raw.get("sort") is not None:
        keys = raw["sort"] if isinstance(raw["sort"], list) else str(raw["sort"]).split(",")
        for k in keys:
            if k.lstrip("-") not in SORT_FIELDS:
                raise ValueError(f"sort key {k!r} is not one of {SORT_FIELDS}")
        out.sort = list(keys)
    if raw.get("window"):
        out.window = raw["window"]
    if raw.get("count") is not None:
        out.count = bool(raw["count"])
    return out


def _view_to_dict(v: ViewDef) -> dict:
    d: dict = {"id": v.id, "layout": v.layout}
    if v.name is not None:
        d["name"] = v.name
    if v.icon is not None:
        d["icon"] = v.icon
    if v.filter is not None:
        # The object form, NOT the text form — verified against a real
        # `nod view add`'s own views.json: it stores the parsed
        # {"field"/"any"/"all"/"not": ...} structure on disk, and only
        # renders filter_text() for its own CLI stdout/`nod views` output.
        d["filter"] = v.filter
    if v.group is not None:
        d["group"] = v.group
    if v.finished != "hide":
        d["finished"] = v.finished
    if v.deferred != "hide":
        d["deferred"] = v.deferred
    if v.sort:
        d["sort"] = v.sort
    if v.window:
        d["window"] = v.window
    if not v.count:
        d["count"] = v.count
    return d


def load_views(path: str | None = None) -> list[ViewDef]:
    path = path or views_path()
    if not os.path.isfile(path):
        return list(BUILTIN_VIEWS)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        views = []
        for v in raw.get("views") or []:
            try:
                views.append(validate_view(v))
            except ValueError:
                continue
        return views or list(BUILTIN_VIEWS)
    except (OSError, ValueError):
        return list(BUILTIN_VIEWS)


def save_views(views: list[ViewDef], path: str | None = None) -> None:
    if not views:
        raise ValueError("can't remove the last view")
    path = path or views_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"views": [_view_to_dict(v) for v in views]}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def reset_views(id_: str | None = None, path: str | None = None) -> list[ViewDef]:
    path = path or views_path()
    if id_ is None:
        if os.path.isfile(path):
            os.remove(path)
        return list(BUILTIN_VIEWS)
    shipped = builtin_view(id_)
    if not shipped:
        raise ValueError(f"{id_} is not a built-in view (one of: {' '.join(BUILTIN_IDS)})")
    views = load_views(path)
    at = next((i for i, v in enumerate(views) if v.id == shipped.id), -1)
    if at == -1:
        views = [*views, shipped]
    else:
        views = [shipped if i == at else v for i, v in enumerate(views)]
    save_views(views, path)
    return views
