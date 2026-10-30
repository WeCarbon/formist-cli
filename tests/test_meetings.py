"""``fm meeting transcript-refresh`` / ``fm meeting join``: meeting-bot client ops.

Absorbs two scripts (EV-ABSORB.md §3.1, both "no new endpoint"):

* ``scripts/ingest_transcript.py`` looped every APPOINTMENT task with a bot_id
  in one hardcoded project and force-refreshed each transcript.
  ``fm meeting transcript-refresh <task-id>`` does ONE named task through
  ``GET /projects/{id}/meetings/{task_id}/transcript``: the route ALREADY
  best-effort re-ingests before answering (routes/meetings_center.py's own
  docstring), so this is a bare client, not a re-implementation of the ingest
  logic. ``--all`` restores the "every task with a bot" behaviour by listing
  meetings first and refreshing each one that has a recording.
* ``scripts/join_meeting.py`` classified a join URL and dispatched a bot.
  ``fm meeting join <url>`` does the SAME thing through
  ``POST /projects/{id}/meetings/join-now``: a WRITE (it can really dispatch a
  bot into a live meeting), so unlike the script it is dry-run by default and
  needs ``--apply``.

Run: uv run pytest tests/test_formist_cli_meetings.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import meetings
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
TASK = "11111111-1111-1111-1111-111111111111"


class RecordingApi:
    def __init__(self, *, transcripts=None, meetings_list=None, join_response=None):
        self.calls: list[tuple[str, str, object]] = []
        self._transcripts = transcripts or {}
        self._meetings_list = meetings_list if meetings_list is not None else {"items": [], "total": 0}
        self._join_response = join_response

    def get(self, target, path, *, token=None, params=None, **kw):
        self.calls.append(("GET", path, params))
        if path.endswith("/transcript"):
            task_id = path.rsplit("/", 2)[-2]
            if task_id not in self._transcripts:
                from formist_cli import api

                err = api.ApiError("not-found")
                err.status = 404
                raise err
            return self._transcripts[task_id]
        if path.endswith("/meetings"):
            return self._meetings_list
        raise AssertionError(f"unexpected GET {path}")

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path, body))
        if path.endswith("/join-now"):
            return self._join_response
        raise AssertionError(f"unexpected POST {path}")

    @property
    def bodies(self):
        return [b for m, _p, b in self.calls if m == "POST"]


def _args(**overrides):
    base = {
        "env": "local",
        "space": SPACE,
        "subject": "transcript-refresh",
        "arg": TASK,
        "all": False,
        "bot_name": None,
        "summary_skill_id": None,
        "apply": False,
        "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, **overrides):
        monkeypatch.setattr(meetings, "api", api_stub)
        monkeypatch.setattr(
            meetings.context,
            "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        return meetings.run(_args(**overrides))

    return _drive


# ---------------------------------------------------------------------------
# transcript-refresh: one task
# ---------------------------------------------------------------------------
def test_transcript_refresh_gets_the_named_task(driven):
    stub = RecordingApi(transcripts={TASK: {"file_id": "f1", "path": "t.md", "name": "t.md", "markdown": "hi"}})
    rc = driven(stub, subject="transcript-refresh", arg=TASK)
    assert rc == 0
    assert stub.calls == [("GET", f"/projects/{SPACE}/meetings/{TASK}/transcript", None)]


def test_transcript_refresh_prints_the_file_metadata(driven, capsys):
    stub = RecordingApi(transcripts={TASK: {"file_id": "f1", "path": "t.md", "name": "t.md", "markdown": "hello world"}})
    driven(stub, subject="transcript-refresh", arg=TASK)
    out = capsys.readouterr().out
    assert "t.md" in out
    assert "f1" in out


def test_transcript_refresh_no_task_and_no_all_is_a_loud_error(driven):
    with pytest.raises(meetings.MeetingsError, match="task"):
        driven(RecordingApi(), subject="transcript-refresh", arg=None, all=False)


def test_transcript_refresh_missing_transcript_translates_the_404(driven):
    stub = RecordingApi(transcripts={})
    from formist_cli import api

    with pytest.raises(api.ApiError):
        driven(stub, subject="transcript-refresh", arg=TASK)


# ---------------------------------------------------------------------------
# transcript-refresh --all: restores the "every task with a bot" behaviour
# ---------------------------------------------------------------------------
def test_transcript_refresh_all_only_refreshes_tasks_with_a_recording(driven):
    stub = RecordingApi(
        meetings_list={
            "items": [
                {"id": TASK, "subject": "Weekly Sync", "has_recording": True},
                {"id": "22222222-2222-2222-2222-222222222222", "subject": "No bot", "has_recording": False},
            ],
            "total": 2,
        },
        transcripts={TASK: {"file_id": "f1", "path": "t.md", "name": "t.md", "markdown": "hi"}},
    )
    driven(stub, subject="transcript-refresh", arg=None, all=True)
    gets = [p for m, p, _ in stub.calls if m == "GET"]
    assert f"/projects/{SPACE}/meetings/{TASK}/transcript" in gets
    assert "22222222-2222-2222-2222-222222222222" not in "".join(gets)


def test_transcript_refresh_all_with_no_recordings_reports_zero_not_an_error(driven, capsys):
    stub = RecordingApi(meetings_list={"items": [], "total": 0})
    rc = driven(stub, subject="transcript-refresh", arg=None, all=True)
    assert rc == 0
    assert "0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# join: dry-run by default, --apply performs the write
# ---------------------------------------------------------------------------
def test_join_without_apply_makes_no_write_call(driven, capsys):
    stub = RecordingApi()
    rc = driven(stub, subject="join", arg="https://teams.example/x")
    assert rc == 0
    assert not any(m == "POST" for m, _p, _b in stub.calls)
    assert "DRY RUN" in capsys.readouterr().err


def test_join_with_apply_posts_join_now_with_the_url(driven):
    stub = RecordingApi(join_response={
        "task": {"id": TASK, "subject": "Ad-hoc"}, "platform": "teams",
        "bot_id": "b1", "already_joined": False, "dispatch_error": None,
    })
    rc = driven(stub, subject="join", arg="https://teams.example/x", apply=True)
    assert rc == 0
    assert stub.bodies == [{"url": "https://teams.example/x"}]


def test_join_apply_includes_optional_fields_only_when_passed(driven):
    stub = RecordingApi(join_response={
        "task": {"id": TASK, "subject": "Ad-hoc"}, "platform": "teams",
        "bot_id": "b1", "already_joined": False, "dispatch_error": None,
    })
    driven(
        stub, subject="join", arg="https://teams.example/x", apply=True,
        bot_name="Evane Notetaker", summary_skill_id="s1",
    )
    assert stub.bodies == [{
        "url": "https://teams.example/x",
        "bot_name": "Evane Notetaker",
        "summary_skill_id": "s1",
    }]


def test_join_apply_reports_a_dispatch_error_without_raising(driven, capsys):
    """The task is saved even when dispatch fails (the script's own docstring) —
    this must be visible in the report, not swallowed as a success."""
    stub = RecordingApi(join_response={
        "task": {"id": TASK, "subject": "Ad-hoc"}, "platform": "zoom",
        "bot_id": None, "already_joined": False,
        "dispatch_error": "missing Zoom Meeting SDK credentials",
    })
    rc = driven(stub, subject="join", arg="https://zoom.example/x", apply=True)
    assert rc == 2
    assert "missing Zoom Meeting SDK credentials" in capsys.readouterr().out


def test_join_no_url_is_a_loud_error(driven):
    with pytest.raises(meetings.MeetingsError, match="url"):
        driven(RecordingApi(), subject="join", arg=None, apply=True)
