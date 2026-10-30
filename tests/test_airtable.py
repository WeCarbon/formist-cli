"""Tests for ``fm airtable direction``: the client half, and the two ways
it can lie. The decision itself lives on the server; both client-side
failures covered here look like success at a terminal:

**A dry run that writes.** "Dry run by default" is not a property of the
wording in ``--help``; it is the property that no POST leaves this process
unless ``--apply`` was passed. So the dry-run tests inject a poster that
raises if it is ever called. A test that merely asserted on the printed
output would pass just as happily against a command that had already
written.

**An apply that submits something other than what it printed.** The whole
point of echoing the plan back to the server is that the operator
authorizes what they read. If ``changes_from`` were to re-derive "what
needs changing" instead of reading the server's ``would_change`` flag, the
client and the server would be answering the question separately. The
case that catches it is an inconclusive item whose ``current`` and
``wanted`` differ: a client that recomputes picks it up, a client that
reads the flag does not.

No network, no DB, no token store: every transport is injected.
"""

from __future__ import annotations

import io
import json

import pytest

from formist_cli import airtable
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CID = "11111111-2222-4333-8444-555555555555"


def _item(**kw):
    base = {
        "connection_id": CID,
        "current": "both",
        "wanted": "inbound",
        "conclusive": True,
        "reason": "ok",
        "detail": "Acme=read",
        "would_change": True,
        "bases": [{"id": "app1", "name": "Acme", "permissionLevel": "read"}],
    }
    base.update(kw)
    return base


def _plan(*items, applied=None):
    body = {
        "items": list(items),
        "counts": {
            "connections": len(items),
            "would_change": sum(1 for i in items if i.get("would_change")),
            "inconclusive": sum(1 for i in items if not i.get("conclusive")),
        },
    }
    if applied is not None:
        body["counts"]["applied"] = applied
    return body


def _getter(body):
    calls = []

    def get(target, path, *, token=None, **kwargs):
        calls.append({"path": path, "token": token})
        return body

    get.calls = calls  # type: ignore[attr-defined]
    return get


def _poster(body):
    calls = []

    def post(target, path, payload, *, token=None, **kwargs):
        calls.append({"path": path, "payload": payload})
        return body

    post.calls = calls  # type: ignore[attr-defined]
    return post


def _explodes(*_a, **_kw):
    raise AssertionError("a dry run must not send a write")


class _Args:
    def __init__(self, **kw):
        self.host = None
        self.space = SPACE
        self.subject = "direction"
        self.apply = False
        self.json = False
        self.__dict__.update(kw)


@pytest.fixture
def wired(monkeypatch, capsys):
    """Patch the CLI's target/token resolution so no prefs file or token is read."""

    class _Eff:
        target = LOCAL
        space = SPACE

        def banner(self):
            return "(test)"

    from formist_cli import cli

    seen = {}

    def fake_effective(args, *, for_write=False, need_space=True):
        seen["for_write"] = for_write
        return _Eff()

    monkeypatch.setattr(cli, "_effective", fake_effective)
    monkeypatch.setattr(cli, "_token_for", lambda eff: "tok")
    return seen


# --- transport ----------------------------------------------------------------
class TestTransport:
    def test_fetch_plan_hits_the_project_scoped_admin_path(self):
        get = _getter(_plan(_item()))
        airtable.fetch_plan(LOCAL, SPACE, token="tok", get=get)
        assert get.calls[0]["path"] == f"/projects/{SPACE}/admin/airtable/direction"
        assert get.calls[0]["token"] == "tok"

    def test_a_non_object_answer_is_refused_not_acted_on(self):
        get = _getter(["not", "a", "plan"])
        with pytest.raises(airtable.AirtableError):
            airtable.fetch_plan(LOCAL, SPACE, token="tok", get=get)

    def test_submit_sends_the_changes_under_the_key_the_endpoint_reads(self):
        post = _poster(_plan(_item(), applied=1))
        changes = [{"connection_id": CID, "current": "both", "wanted": "inbound"}]
        airtable.submit(LOCAL, SPACE, changes, token="tok", post=post)
        assert post.calls[0]["payload"] == {"changes": changes}


# --- what gets authorized -----------------------------------------------------
class TestChangesFrom:
    def test_takes_the_servers_flag(self):
        plan = _plan(_item())
        assert airtable.changes_from(plan) == [{"connection_id": CID, "current": "both", "wanted": "inbound"}]

    def test_an_inconclusive_item_is_never_authorized_even_if_its_values_differ(self):
        """The case that separates reading the flag from recomputing it."""
        plan = _plan(
            _item(
                conclusive=False,
                reason="no-permission-level",
                would_change=False,
                current="both",
                wanted="inbound",  # differs, but the server declined to decide
            )
        )
        assert airtable.changes_from(plan) == []

    def test_a_correct_connection_is_not_authorized(self):
        plan = _plan(_item(current="inbound", wanted="inbound", would_change=False))
        assert airtable.changes_from(plan) == []


# --- the dry-run guarantee ----------------------------------------------------
class TestDryRun:
    def test_without_apply_nothing_is_posted(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: _plan(_item()))
        monkeypatch.setattr(airtable, "submit", _explodes)
        assert airtable.cmd_airtable(_Args()) == 0
        out = capsys.readouterr().out
        assert "(dry run)" in out
        assert "--apply" in out

    def test_a_dry_run_may_inherit_its_environment(self, wired, monkeypatch):
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: _plan(_item()))
        monkeypatch.setattr(airtable, "submit", _explodes)
        airtable.cmd_airtable(_Args())
        assert wired["for_write"] is False

    def test_an_apply_may_not_inherit_its_environment(self, wired, monkeypatch):
        """A remembered env is one somebody forgets they set; this one writes."""
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: _plan(_item()))
        monkeypatch.setattr(airtable, "submit", lambda *a, **k: _plan(_item(), applied=1))
        airtable.cmd_airtable(_Args(apply=True))
        assert wired["for_write"] is True

    def test_apply_with_nothing_to_change_sends_no_request(self, wired, monkeypatch, capsys):
        plan = _plan(_item(current="inbound", wanted="inbound", would_change=False))
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: plan)
        monkeypatch.setattr(airtable, "submit", _explodes)
        assert airtable.cmd_airtable(_Args(apply=True)) == 0
        assert "nothing to change" in capsys.readouterr().out


class TestJson:
    """--json must emit JSON and nothing else. A machine reader that has to strip
    a human summary off the front is a parser, and a parser is a place to be wrong."""

    def test_dry_run_json_is_parseable_on_its_own(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: _plan(_item()))
        monkeypatch.setattr(airtable, "submit", _explodes)
        assert airtable.cmd_airtable(_Args(json=True)) == 0
        assert json.loads(capsys.readouterr().out)["counts"]["would_change"] == 1

    def test_apply_json_with_nothing_to_do_is_still_json(self, wired, monkeypatch, capsys):
        plan = _plan(_item(current="inbound", wanted="inbound", would_change=False))
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: plan)
        monkeypatch.setattr(airtable, "submit", _explodes)
        assert airtable.cmd_airtable(_Args(apply=True, json=True)) == 0
        assert json.loads(capsys.readouterr().out)["counts"]["applied"] == 0


class TestApply:
    def test_apply_submits_exactly_the_lines_it_printed(self, wired, monkeypatch, capsys):
        plan = _plan(
            _item(),
            _item(
                connection_id="other",
                conclusive=False,
                reason="no-credential",
                would_change=False,
                current="both",
                wanted="both",
            ),
        )
        sent = {}

        def fake_submit(target, space, changes, *, token=None, post=None):
            sent["changes"] = changes
            return {
                **_plan(*plan["items"], applied=1),
                "outcomes": [
                    {
                        "connection_id": CID,
                        "applied": True,
                        "reason": "applied",
                        "detail": "direction 'both' => 'inbound'",
                    }
                ],
            }

        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: plan)
        monkeypatch.setattr(airtable, "submit", fake_submit)
        assert airtable.cmd_airtable(_Args(apply=True)) == 0
        assert sent["changes"] == [{"connection_id": CID, "current": "both", "wanted": "inbound"}]

    def test_a_refused_line_makes_the_command_fail(self, wired, monkeypatch, capsys):
        """The grant moved between the dry run and the apply: exit
        non-zero, or a script wrapping `fm` reads a partial apply as a
        complete one."""
        monkeypatch.setattr(airtable, "fetch_plan", lambda *a, **k: _plan(_item()))
        monkeypatch.setattr(
            airtable,
            "submit",
            lambda *a, **k: {
                **_plan(_item(), applied=0),
                "outcomes": [
                    {
                        "connection_id": CID,
                        "applied": False,
                        "reason": "plan-moved",
                        "detail": "the live probe now says ...",
                    }
                ],
            },
        )
        assert airtable.cmd_airtable(_Args(apply=True)) == 1


# --- what the operator reads --------------------------------------------------
class TestRender:
    def test_inconclusive_is_not_printed_as_correct(self):
        out = io.StringIO()
        airtable.render_plan(
            _plan(
                _item(
                    conclusive=False,
                    reason="no-credential",
                    would_change=False,
                    wanted="both",
                    detail="no Airtable authorization stored",
                )
            ),
            applying=False,
            out=out,
        )
        text = out.getvalue()
        assert "NO CREDENTIAL" in text
        assert "is correct" not in text
        assert "1 inconclusive" in text

    def test_an_unknown_reason_code_is_printed_rather_than_swallowed(self):
        out = io.StringIO()
        airtable.render_plan(
            _plan(_item(conclusive=False, reason="something-new", would_change=False)),
            applying=False,
            out=out,
        )
        assert "something-new" in out.getvalue()
