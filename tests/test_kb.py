"""``fm kb search`` / ``fm kb read``: the client half of the ad-hoc KB surface.

Absorbs ``scripts/kb_search.py`` and ``scripts/kb_read.py`` (EV-ABSORB.md §3.1)
into ``ev``, reached through the SAME project-scoped, member-gated routes the
product itself uses: ``GET /projects/{id}/search`` (services/search.py) and
``GET /projects/{id}/files/{file_id}/content`` (services/user_acl.py): never a
local Postgres/disk read the way the scripts did.

Not the routing or the ranking (those are proven where they live, against a real
database). What this file pins is what the CLIENT does with the response:

* ``kb search`` joins its positional terms with spaces into ONE ``q=``: the
  ranking already happens server-side (services/search.py's own docstring:
  "matching is per term, not per phrase"), so this command must never split the
  work into one call per term the way the old script did.
* ``kb read`` takes the FIRST hit that carries a ``file_id`` from that same
  search, then reads its content: never a local ``cached_md_path`` open, which
  is exactly the "prod should read through the blob-backed route" correction
  EV-ABSORB.md records for this script.
* both refuse loudly on a non-dict / malformed response rather than rendering
  garbage.

Run: uv run pytest tests/test_formist_cli_kb.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import kb
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


class RecordingApi:
    """Stand-in for ``formist_cli.api``, answering scripted GETs and recording them."""

    def __init__(self, *, search=None, content_by_id=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._search = search if search is not None else {"items": [], "total": 0}
        self._content_by_id = content_by_id or {}

    def get(self, target, path, *, token=None, params=None, **kw):
        self.calls.append(("GET", path, params))
        if path.endswith("/search"):
            return self._search
        if "/files/" in path and path.endswith("/content"):
            file_id = path.rsplit("/", 2)[-2]
            if file_id not in self._content_by_id:
                from formist_cli import api

                err = api.ApiError("not-found")
                err.status = 404
                raise err
            return self._content_by_id[file_id]
        raise AssertionError(f"unexpected GET {path}")


def _args(**overrides) -> argparse.Namespace:
    base = {
        "env": "local",
        "space": SPACE,
        "subject": "search",
        "query": ["RACI"],
        "max_chars": kb.DEFAULT_MAX_CHARS,
        "limit": None,
        "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, **overrides):
        monkeypatch.setattr(kb, "api", api_stub)
        monkeypatch.setattr(
            kb.context,
            "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        return kb.run(_args(**overrides))

    return _drive


# ---------------------------------------------------------------------------
# search: multi-term query is joined into ONE q=, never split per term
# ---------------------------------------------------------------------------
def test_search_joins_positional_terms_into_one_query_param(driven):
    stub = RecordingApi()
    driven(stub, subject="search", query=["organi", "structure", "governance"])
    gets = [c for c in stub.calls if c[0] == "GET"]
    assert len(gets) == 1, "must be ONE server call, not one per term"
    assert gets[0][2]["q"] == "organi structure governance"


def test_search_prints_hits_grouped_with_source_and_snippet(driven, capsys):
    stub = RecordingApi(
        search={
            "items": [
                {
                    "file_id": "f1",
                    "path": "/GFSS/RACI.docx",
                    "name": "RACI.docx",
                    "snippet": "...responsible...",
                    "source": "document",
                }
            ],
            "total": 1,
        }
    )
    driven(stub, subject="search", query=["RACI"])
    out = capsys.readouterr().out
    assert "/GFSS/RACI.docx" in out
    assert "document" in out
    assert "responsible" in out


def test_search_with_no_hits_says_so_rather_than_printing_nothing(driven, capsys):
    stub = RecordingApi(search={"items": [], "total": 0})
    driven(stub, subject="search", query=["nonexistent-term"])
    out = capsys.readouterr().out
    assert "no hits" in out.lower() or "0 hit" in out.lower()


def test_search_a_non_dict_response_is_a_loud_error(driven):
    stub = RecordingApi(search="not a dict")
    with pytest.raises(kb.KbError, match="contract change"):
        driven(stub, subject="search", query=["x"])


def test_search_json_flag_prints_raw_json(driven, capsys):
    stub = RecordingApi(search={"items": [], "total": 0})
    driven(stub, subject="search", query=["x"], json=True)
    out = capsys.readouterr().out
    assert '"total": 0' in out


# ---------------------------------------------------------------------------
# read: search for the needle, then GET the top file_id hit's content
# ---------------------------------------------------------------------------
def test_read_searches_then_reads_the_top_file_id_hit(driven):
    stub = RecordingApi(
        search={
            "items": [
                {"file_id": "f1", "path": "/GFSS/RACI.docx", "name": "RACI.docx",
                 "snippet": "...", "source": "document"},
            ],
            "total": 1,
        },
        content_by_id={
            "f1": {
                "file_id": "f1", "path": "/GFSS/RACI.docx", "name": "RACI.docx",
                "mime_type": "application/vnd...", "source": "SHAREPOINT",
                "access_scope": "SHARED", "web_url": "https://...",
                "markdown": "# RACI\n\nfull text here",
            }
        },
    )
    driven(stub, subject="read", query=["RACI"])
    methods_paths = [(m, p) for m, p, _ in stub.calls]
    assert ("GET", f"/projects/{SPACE}/search") in methods_paths
    assert ("GET", f"/projects/{SPACE}/files/f1/content") in methods_paths


def test_read_prints_the_path_char_count_and_truncated_body(driven, capsys):
    stub = RecordingApi(
        search={"items": [{"file_id": "f1", "path": "/x.md", "name": "x.md",
                            "snippet": "", "source": "document"}], "total": 1},
        content_by_id={"f1": {"file_id": "f1", "path": "/x.md", "name": "x.md",
                               "markdown": "0123456789"}},
    )
    driven(stub, subject="read", query=["x"], max_chars=4)
    out = capsys.readouterr().out
    assert "/x.md" in out
    assert "0123" in out
    assert "…[truncated" in out or "more chars" in out


def test_read_with_no_hits_is_a_loud_error_not_a_silent_empty_print(driven):
    stub = RecordingApi(search={"items": [], "total": 0})
    with pytest.raises(kb.KbError, match="no.*hit|not found"):
        driven(stub, subject="read", query=["nonexistent"])


def test_read_skips_hits_with_no_file_id_to_find_the_first_readable_one(driven):
    """A message/sharepoint-native hit carries no `file_id` and cannot be read
    through /files/{id}/content: it must be SKIPPED, not sent as a literal
    string id (which would 422 the UUID path param)."""
    stub = RecordingApi(
        search={
            "items": [
                {"message_id": "m1", "subject": "an email", "name": "x", "snippet": "", "source": "message"},
                {"file_id": "f1", "path": "/x.md", "name": "x.md", "snippet": "", "source": "document"},
            ],
            "total": 2,
        },
        content_by_id={"f1": {"file_id": "f1", "path": "/x.md", "name": "x.md", "markdown": "hi"}},
    )
    driven(stub, subject="read", query=["x"])
    paths = [p for _m, p, _ in stub.calls]
    assert f"/projects/{SPACE}/files/f1/content" in paths


def test_read_json_flag_prints_raw_json(driven, capsys):
    stub = RecordingApi(
        search={"items": [{"file_id": "f1", "path": "/x.md", "name": "x.md",
                            "snippet": "", "source": "document"}], "total": 1},
        content_by_id={"f1": {"file_id": "f1", "path": "/x.md", "name": "x.md", "markdown": "hi"}},
    )
    driven(stub, subject="read", query=["x"], json=True)
    out = capsys.readouterr().out
    assert '"markdown": "hi"' in out
