"""``fm files reevaluate``: the client half of the file re-evaluation surface.

⚠️ ENTERED THROUGH ``cmd_files``, NOT ``run``. ``fm files`` carries TWO
subcommands since the 2026-09-14 main/prod integration: ``merge`` (the prod
line's duplicate-File repair, D455/D461) and ``reevaluate`` (this one): so the
module's entry point dispatches on ``args.subject``. Calling the reevaluate
half directly would stop covering that dispatch, which is the one line where a
future third subcommand could silently swallow this one.

Absorbs ``scripts/reevaluate_files.py`` (EV-ABSORB.md §3.1, no new endpoint):
that script re-applied the CURRENT processing criteria to File rows judged
under older ones, over a direct DB session. ``fm files reevaluate`` does the
same job through the routes ``services/file_reevaluation.py`` already exposes —

* no ``--file`` -> ``POST /projects/{id}/files/reevaluate`` (the project-wide
  sweep, ``ReevaluateSweepRequest``)
* ``--file <id>`` -> ``POST /projects/{id}/files/{file_id}/reevaluate`` (one
  row, ``ReevaluateRequest``)

DRY RUN by default on BOTH forms: the sweep route already defaults
``dry_run: true`` server-side, but the single-file route does NOT
(``ReevaluateRequest.dry_run: bool = False``), so the CLIENT sends the same
default on every call rather than inheriting whichever the server happens to
pick for the shape it was asked to hit.

Run: uv run pytest tests/test_formist_cli_files.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import files as files_mod
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
FILE_ID = "33333333-3333-3333-3333-333333333333"


class RecordingApi:
    def __init__(self, *, response=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._response = response if response is not None else {"to_fill": 0}

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path, body))
        return self._response

    @property
    def bodies(self):
        return [b for _m, _p, b in self.calls]


def _args(**overrides):
    base = {
        "env": "local", "space": SPACE, "subject": "reevaluate",
        "file": None, "apply": False, "only_skipped": True, "no_rederive": False,
        "limit": None, "after_id": None, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, **overrides):
        monkeypatch.setattr(files_mod, "api", api_stub)
        monkeypatch.setattr(
            files_mod.context, "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        return files_mod.cmd_files(_args(**overrides))

    return _drive


# ---------------------------------------------------------------------------
# project-wide sweep (no --file)
# ---------------------------------------------------------------------------
def test_sweep_without_apply_sends_dry_run_true(driven):
    stub = RecordingApi(response={"to_fill": 0, "counts": {}})
    rc = driven(stub)
    assert rc == 0
    assert stub.calls[0][1] == f"/projects/{SPACE}/files/reevaluate"
    assert stub.bodies[0]["dry_run"] is True


def test_sweep_with_apply_sends_dry_run_false(driven):
    stub = RecordingApi(response={"applied": 3})
    driven(stub, apply=True)
    assert stub.bodies[0]["dry_run"] is False


def test_sweep_only_skipped_and_limit_and_after_id_reach_the_body(driven):
    stub = RecordingApi()
    driven(stub, only_skipped=False, limit=50, after_id="prev-id")
    body = stub.bodies[0]
    assert body["only_skipped"] is False
    assert body["limit"] == 50
    assert body["after_id"] == "prev-id"


def test_sweep_no_rederive_flag_reaches_the_body(driven):
    stub = RecordingApi()
    driven(stub, no_rederive=True)
    assert stub.bodies[0]["rederive"] is False


# ---------------------------------------------------------------------------
# single file (--file <id>)
# ---------------------------------------------------------------------------
def test_single_file_posts_to_the_per_file_route(driven):
    stub = RecordingApi(response={"item": {"id": FILE_ID}, "reevaluation": {"action": "mark-for-resync"}})
    driven(stub, file=FILE_ID)
    assert stub.calls[0][1] == f"/projects/{SPACE}/files/{FILE_ID}/reevaluate"


def test_single_file_defaults_to_dry_run_true_even_though_the_SERVER_default_is_false(driven):
    """ReevaluateRequest.dry_run defaults False server-side: the client must
    never rely on that and must always send its OWN dry-run-by-default."""
    stub = RecordingApi(response={"item": {"id": FILE_ID}, "reevaluation": {}})
    driven(stub, file=FILE_ID)
    assert stub.bodies[0]["dry_run"] is True


def test_single_file_apply_sends_dry_run_false(driven):
    stub = RecordingApi(response={"item": {"id": FILE_ID}, "reevaluation": {}})
    driven(stub, file=FILE_ID, apply=True)
    assert stub.bodies[0]["dry_run"] is False


def test_single_file_only_skipped_is_not_sent_it_has_no_meaning_there(driven):
    """`only_skipped` is a SWEEP-only option (ReevaluateSweepRequest): the
    single-file body is `extra='forbid'` (ReevaluateRequest), so sending it
    would 422 instead of being ignored."""
    stub = RecordingApi(response={"item": {"id": FILE_ID}, "reevaluation": {}})
    driven(stub, file=FILE_ID, only_skipped=False)
    assert "only_skipped" not in stub.bodies[0]
    assert "limit" not in stub.bodies[0]
    assert "after_id" not in stub.bodies[0]


def test_a_non_dict_response_is_a_loud_error(driven):
    stub = RecordingApi(response="nope")
    with pytest.raises(files_mod.FilesError, match="contract change"):
        driven(stub)


def test_dry_run_report_says_nothing_written(driven, capsys):
    stub = RecordingApi(response={"to_fill": 2, "counts": {"mark-for-resync": 2}})
    driven(stub)
    out = capsys.readouterr().out
    assert "DRY RUN" in out or "dry" in out.lower()


def test_json_flag_prints_raw_json(driven, capsys):
    stub = RecordingApi(response={"to_fill": 0})
    driven(stub, json=True)
    out = capsys.readouterr().out
    assert '"to_fill": 0' in out
