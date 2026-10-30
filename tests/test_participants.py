"""Tests for ``fm participants dedupe``.

``formist_cli/participants.py`` holds no merge decision logic (that lives on
the server); this file pins what the client sends, how it renders a
response, and that dedupe stays manual: tools detect and describe, never
one-shot delete. Concretely:

  * a dry run (the default, no ``--apply``) must never issue a write, so
    every write-capable transport stub in this file raises if a POST is
    reached when it should not be;
  * ``--apply`` needs the plan's own token, so a stale dry run refuses
    rather than merging a set the operator never saw;
  * ``--group``/``--reason`` are forwarded to the server exactly as given,
    never defaulted or coerced client-side; the server is the one place
    that rule is checked.

HTTP transport is stubbed; ``formist_cli/api.py`` is tested elsewhere.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlencode

import pytest

from formist_cli import api, participants
from formist_cli.participants import ParticipantsError
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
DEDUPE_PATH = participants.DEDUPE_PATH.format(space=SPACE)


# ===========================================================================
# parse_drops: pure function
# ===========================================================================
class TestParseDrops:
    def test_none_and_empty_are_no_drops(self):
        assert participants.parse_drops(None) == {}
        assert participants.parse_drops([]) == {}

    def test_a_single_drop(self):
        out = participants.parse_drops(["airtable:rec1=data.airtable.contact_email"])
        assert out == {"airtable:rec1": ["data.airtable.contact_email"]}

    def test_repeated_group_accumulates_paths(self):
        out = participants.parse_drops(
            ["g1=data.a", "g1=data.b", "g2=data.c"]
        )
        assert out == {"g1": ["data.a", "data.b"], "g2": ["data.c"]}

    def test_splits_on_the_FIRST_equals_only(self):
        """A normalised name can itself contain '=': splitting on every '='
        would silently truncate the key rather than fail loudly. The FIRST
        '=' is the group/path separator, so everything after it (including a
        second '=') stays part of the path."""
        out = participants.parse_drops(["name:a=b=data.x"])
        assert out == {"name:a": ["b=data.x"]}

    def test_missing_equals_raises(self):
        with pytest.raises(ParticipantsError, match="GROUP=data.path"):
            participants.parse_drops(["airtable:rec1"])

    def test_missing_group_before_equals_raises(self):
        with pytest.raises(ParticipantsError, match="group AND a path"):
            participants.parse_drops(["=data.x"])

    def test_missing_path_after_equals_raises(self):
        with pytest.raises(ParticipantsError, match="group AND a path"):
            participants.parse_drops(["g1="])


# ===========================================================================
# fetch_plan: the dry run. Must never write.
# ===========================================================================
class _NoPost:
    """A transport whose ``post`` fails the test if it is ever reached:
    dependency-injected straight into fetch_plan/submit, no monkeypatching."""

    def __call__(self, *a, **kw):
        raise AssertionError("fetch_plan (a dry run) must never POST")


class TestFetchPlanNeverWrites:
    def test_sends_a_bare_GET_with_no_query_when_no_groups(self):
        calls = []

        def get(target, path, *, token=None, **kw):
            calls.append((target, path, token))
            return {"groups": []}

        plan = participants.fetch_plan(LOCAL, SPACE, token="t", get=get)
        assert plan == {"groups": []}
        assert calls == [(LOCAL, DEDUPE_PATH, "t")]

    def test_repeated_groups_are_encoded_as_repeated_query_params(self):
        seen = {}

        def get(target, path, *, token=None, **kw):
            seen["path"] = path
            return {"groups": []}

        participants.fetch_plan(
            LOCAL, SPACE, token="t", groups=["airtable:rec1", "name:foo bar"], get=get
        )
        expected_qs = urlencode([("group", "airtable:rec1"), ("group", "name:foo bar")])
        assert seen["path"] == f"{DEDUPE_PATH}?{expected_qs}"

    def test_a_response_missing_groups_key_is_refused_not_swallowed(self):
        def get(*a, **kw):
            return {"live_participants": 3}

        with pytest.raises(ParticipantsError, match="groups"):
            participants.fetch_plan(LOCAL, SPACE, token="t", get=get)

    def test_a_non_dict_response_is_refused(self):
        def get(*a, **kw):
            return ["not", "a", "dict"]

        with pytest.raises(ParticipantsError):
            participants.fetch_plan(LOCAL, SPACE, token="t", get=get)


# ===========================================================================
# submit: forwards exactly what it is given, never invents a default
# ===========================================================================
class TestSubmitForwardsVerbatim:
    def test_sends_plan_token_groups_reason_and_omits_drop_when_absent(self):
        calls = []

        def post(target, path, body, *, token=None, **kw):
            calls.append((target, path, body))
            return {"groups": [], "outcomes": []}

        participants.submit(
            LOCAL, SPACE, "tok1", token="t", groups=["g1"], reason="same person", post=post
        )
        assert calls[0][1] == DEDUPE_PATH
        body = calls[0][2]
        assert body == {"plan_token": "tok1", "groups": ["g1"], "reason": "same person"}
        assert "drop_data_keys" not in body

    def test_includes_drop_data_keys_only_when_present(self):
        captured = {}

        def post(target, path, body, *, token=None, **kw):
            captured["body"] = body
            return {"groups": []}

        participants.submit(
            LOCAL, SPACE, "tok1", token="t", groups=["g1"], reason="x",
            drop_data_keys={"g1": ["data.a"]}, post=post,
        )
        assert captured["body"]["drop_data_keys"] == {"g1": ["data.a"]}

    def test_falsy_groups_and_reason_are_forwarded_verbatim_not_coerced(self):
        """The server is the one place an empty list or blank reason is
        refused (422). A client that quietly turned an empty list into None
        (or vice versa) before sending it would be a second, silently
        diverging copy of that rule."""
        captured = {}

        def post(target, path, body, *, token=None, **kw):
            captured["body"] = body
            return {"groups": []}

        participants.submit(LOCAL, SPACE, "tok1", token="t", groups=[], reason="", post=post)
        assert captured["body"]["groups"] == []
        assert captured["body"]["reason"] == ""

    def test_a_response_missing_groups_key_is_refused(self):
        def post(*a, **kw):
            return {"outcomes": []}

        with pytest.raises(ParticipantsError, match="groups"):
            participants.submit(LOCAL, SPACE, "tok1", token="t", groups=["g1"], reason="x", post=post)


# ===========================================================================
# Rendering
# ===========================================================================
def _plan_with_groups(**overrides) -> dict:
    base = {
        "live_participants": 42,
        "auto_mergeable": 1,
        "advisory": 1,
        "plan_token": "tok1",
        "rows_to_soft_delete": 1,
        "groups": [
            {
                "tier": "record_id",
                "key": "airtable:rec1",
                "label": "Jane Doe",
                "keep_id": "p1",
                "drop_ids": ["p2"],
                "updates": ["phone", "title"],
                "conflicts": [
                    {"field": "org", "keeper": "IFC", "loser": "I.F.C.", "loser_id": "p2"},
                ],
            }
        ],
    }
    return base | overrides


class TestRenderPlan:
    def test_summary_counts(self, capsys):
        participants.render_plan(_plan_with_groups())
        out = capsys.readouterr().out
        assert "42 live participant(s)" in out
        assert "1 record-id group(s)" in out
        assert "1 same-name group(s), advisory" in out

    def test_record_id_tier_labelled_and_same_name_tier_labelled_differently(self):
        import io

        buf = io.StringIO()
        plan = _plan_with_groups()
        plan["groups"].append(
            {"tier": "same_name", "key": "name:john", "label": "John",
             "keep_id": "p3", "drop_ids": []}
        )
        participants.render_plan(plan, out=buf)
        out = buf.getvalue()
        assert "[record-id] airtable:rec1" in out
        assert "[same-name] name:john" in out

    def test_conflict_is_printed_separately_never_folded_into_the_summary(self):
        import io

        buf = io.StringIO()
        participants.render_plan(_plan_with_groups(), out=buf)
        out = buf.getvalue()
        assert "CONFLICT org: keeping 'IFC', discarding 'I.F.C.' (from p2)" in out

    def test_updates_line_omitted_when_no_updates(self):
        import io

        buf = io.StringIO()
        plan = _plan_with_groups()
        plan["groups"][0]["updates"] = {}
        participants.render_plan(plan, out=buf)
        assert "would merge in:" not in buf.getvalue()

    def test_footer_always_says_nothing_applied_and_carries_the_token(self):
        import io

        buf = io.StringIO()
        participants.render_plan(_plan_with_groups(), out=buf)
        out = buf.getvalue()
        assert "NOTHING HERE IS APPLIED" in out
        assert "plan_token tok1" in out

    def test_zero_groups_does_not_crash_and_shows_zero_counts(self):
        import io

        buf = io.StringIO()
        participants.render_plan(
            {"live_participants": 0, "auto_mergeable": 0, "advisory": 0, "groups": []}, out=buf
        )
        out = buf.getvalue()
        assert "0 live participant(s)" in out


class TestRenderReview:
    def test_no_groups_says_so_and_returns_early(self):
        import io

        buf = io.StringIO()
        participants.render_review({"groups": []}, out=buf)
        out = buf.getvalue()
        assert "no duplicate groups" in out
        assert "|" not in out  # no table was attempted

    def test_review_header_keeper_suggestion_and_conflict_marks(self):
        import io

        buf = io.StringIO()
        plan = {
            "groups": [
                {
                    "tier": "record_id",
                    "key": "airtable:rec1",
                    "label": "Jane Doe",
                    "keeper_suggestion": {"id": "p1", "reason": "oldest row"},
                    "rows": [
                        {"id": "p1", "created_at": "2026-01-01", "session_participations": 2,
                         "audit_history": 1, "created_by": {"source": "connector"}},
                        {"id": "p2", "created_at": "2026-02-01", "session_participations": 0,
                         "audit_history": 0,
                         "created_by": {"source": "proposal", "actor": "alice", "proposal_id": "pr1"}},
                    ],
                    "fields": [
                        {"field": "org", "values": {"p1": "IFC", "p2": None}, "conflict": False},
                        {"field": "title", "values": {"p1": "A", "p2": "B"}, "conflict": True},
                    ],
                }
            ]
        }
        participants.render_review(plan, out=buf)
        out = buf.getvalue()
        assert "## [record_id] `airtable:rec1`: 'Jane Doe'" in out
        assert "keeper SUGGESTION (not a decision): `p1`: oldest row" in out
        assert "proposal pr1" in out
        assert "| org | IFC | - | agree |" in out
        assert "| title | A | B | CONFLICT |" in out


class TestRenderOutcomes:
    def test_error_outcome(self, capsys):
        participants.render_outcomes(
            {"outcomes": [{"key": "g1", "error": "409 stale"}],
             "live_participants": 41, "auto_mergeable": 0}
        )
        out = capsys.readouterr().out
        assert "ERROR g1: 409 stale" in out

    def test_skipped_outcome(self, capsys):
        participants.render_outcomes(
            {"outcomes": [{"key": "g1", "skipped": "already merged"}],
             "live_participants": 41, "auto_mergeable": 0}
        )
        assert "SKIP g1: already merged" in capsys.readouterr().out

    def test_success_outcome_shows_repointed_refs_sorted_and_dropped_values(self, capsys):
        participants.render_outcomes(
            {
                "outcomes": [{
                    "key": "g1", "merged": "p2", "keep_id": "p1",
                    "references_repointed": {"tasks": 2, "comments": 1},
                    "dropped_values": [{"path": "data.airtable.contact_email", "value": "x@y.com"}],
                }],
                "live_participants": 41, "auto_mergeable": 0,
            }
        )
        out = capsys.readouterr().out
        assert "OK g1: merged p2 into p1 (comments=1, tasks=2)" in out
        assert "dropped data.airtable.contact_email = 'x@y.com'" in out
        assert "soft-deleted row" in out

    def test_no_live_references_is_said_explicitly(self, capsys):
        participants.render_outcomes(
            {"outcomes": [{"key": "g1", "merged": "p2", "keep_id": "p1", "references_repointed": {}}],
             "live_participants": 41, "auto_mergeable": 0}
        )
        assert "no live references" in capsys.readouterr().out


# ===========================================================================
# cmd_participants: the full CLI flow, including the manual-dedupe rules.
# ===========================================================================
class RecordingTransport:
    """Stands in for formist_cli.api.{get,post} for the CLI-level tests. A POST
    reached with no ``submit_response`` scripted is a bug in the module under
    test (a write the test did not expect), so it fails loudly rather than
    silently returning something."""

    def __init__(self, *, plan=None, submit_response=None):
        self.calls: list[tuple[str, str, object]] = []
        self._plan = plan if plan is not None else {"groups": [], "plan_token": "tok0"}
        self._submit_response = submit_response

    def get(self, target, path, *, token=None, **kw):
        self.calls.append(("GET", path, None))
        return self._plan

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path, body))
        if self._submit_response is None:
            raise AssertionError("unexpected POST: this test scripted no submit_response")
        return self._submit_response


def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": "dedupe", "env": "local", "space": SPACE, "json": False,
        "group": None, "reason": None, "apply": None, "drop": None, "review": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def drive(monkeypatch):
    """Installs the fake transport at the formist_cli.api layer, and the
    formist_cli.cli._effective/_token_for resolvers that cmd_participants
    imports lazily."""
    from formist_cli import cli

    class _Eff:
        target = LOCAL
        space = SPACE

    monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")

    def _install(**transport_kw):
        t = RecordingTransport(**transport_kw)
        monkeypatch.setattr(api, "get", t.get)
        monkeypatch.setattr(api, "post", t.post)
        return t

    return _install


class TestDryRunNeverWrites:
    def test_no_apply_flag_issues_only_GETs(self, drive):
        t = drive(plan=_plan_with_groups())
        rc = participants.cmd_participants(_args(apply=None))
        assert rc == 0
        assert [m for m, _p, _b in t.calls] == ["GET"]

    def test_review_mode_is_still_a_dry_run(self, drive, capsys):
        t = drive(plan=_plan_with_groups())
        participants.cmd_participants(_args(apply=None, review=True))
        assert [m for m, _p, _b in t.calls] == ["GET"]
        assert "review (" in capsys.readouterr().out


class TestApplyNeverBypassesTheStaleTokenCheck:
    def test_a_token_that_does_not_match_the_fresh_plan_refuses_and_writes_nothing(self, drive):
        t = drive(plan=_plan_with_groups())  # server-side plan_token is "tok1"
        with pytest.raises(ParticipantsError, match="REFUSING TO APPLY"):
            participants.cmd_participants(
                _args(apply="stale-token", group=["airtable:rec1"], reason="same person")
            )
        assert [m for m, _p, _b in t.calls] == ["GET"], "no POST must be reached on a stale token"

    def test_a_plan_with_no_groups_left_writes_nothing_even_with_a_matching_token(self, drive, capsys):
        t = drive(plan={"groups": [], "plan_token": "tok1"})
        rc = participants.cmd_participants(_args(apply="tok1", group=["g1"], reason="x"))
        assert rc == 0
        assert [m for m, _p, _b in t.calls] == ["GET"]
        assert "nothing to merge" in capsys.readouterr().out


class TestApplyForwardsExactlyWhatWasNamed:
    def test_group_reason_and_drop_reach_the_submit_body_unchanged(self, drive):
        t = drive(
            plan=_plan_with_groups(),
            submit_response={"groups": [], "outcomes": [], "live_participants": 41, "auto_mergeable": 0},
        )
        participants.cmd_participants(
            _args(
                apply="tok1", group=["airtable:rec1"], reason="same person",
                drop=["airtable:rec1=data.airtable.contact_email"],
            )
        )
        post_calls = [c for c in t.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        assert post_calls[0][2] == {
            "plan_token": "tok1",
            "groups": ["airtable:rec1"],
            "reason": "same person",
            "drop_data_keys": {"airtable:rec1": ["data.airtable.contact_email"]},
        }

    def test_an_omitted_group_flag_forwards_None_the_client_never_invents_a_wildcard(self, drive):
        """`--group` is not repeated on this call. cmd_participants must not
        substitute `[]` or `"*"` for the missing flag; either would be a
        locally-invented "merge everything" shape. Forwarding None and
        letting the server's 422 own the refusal is the documented
        contract."""
        t = drive(
            plan=_plan_with_groups(),
            submit_response={"groups": [], "outcomes": [], "live_participants": 41, "auto_mergeable": 0},
        )
        participants.cmd_participants(_args(apply="tok1", group=None, reason="same person"))
        post_calls = [c for c in t.calls if c[0] == "POST"]
        assert post_calls[0][2]["groups"] is None


class TestJsonAndReturnCode:
    def test_json_flag_prints_the_plan_verbatim_on_a_dry_run(self, drive, capsys):
        drive(plan=_plan_with_groups())
        participants.cmd_participants(_args(apply=None, json=True))
        out = capsys.readouterr().out
        assert '"plan_token": "tok1"' in out

    def test_json_flag_suppresses_the_review_render_too(self, drive, capsys):
        drive(plan=_plan_with_groups())
        participants.cmd_participants(_args(apply=None, json=True, review=True))
        out = capsys.readouterr().out
        assert "review (" not in out

    def test_json_flag_prints_the_apply_report_verbatim(self, drive, capsys):
        drive(
            plan=_plan_with_groups(),
            submit_response={"groups": [], "outcomes": [], "live_participants": 41, "auto_mergeable": 0},
        )
        participants.cmd_participants(
            _args(apply="tok1", group=["airtable:rec1"], reason="x", json=True)
        )
        assert '"live_participants": 41' in capsys.readouterr().out

    def test_a_failed_merge_returns_1_and_says_so_on_stderr(self, drive, capsys):
        drive(
            plan=_plan_with_groups(),
            submit_response={
                "groups": [],
                "outcomes": [{"key": "airtable:rec1", "error": "409 stale"}],
                "live_participants": 42, "auto_mergeable": 1,
            },
        )
        rc = participants.cmd_participants(
            _args(apply="tok1", group=["airtable:rec1"], reason="x")
        )
        assert rc == 1
        assert "1 group(s) were NOT merged" in capsys.readouterr().err

    def test_every_outcome_succeeding_returns_0(self, drive):
        drive(
            plan=_plan_with_groups(),
            submit_response={
                "groups": [],
                "outcomes": [{"key": "airtable:rec1", "merged": "p2", "keep_id": "p1",
                               "references_repointed": {}}],
                "live_participants": 41, "auto_mergeable": 0,
            },
        )
        rc = participants.cmd_participants(
            _args(apply="tok1", group=["airtable:rec1"], reason="x")
        )
        assert rc == 0


class TestReRunHint:
    def test_a_dry_run_with_groups_prints_the_exact_reapply_hint(self, drive, capsys):
        drive(plan=_plan_with_groups())
        participants.cmd_participants(_args(apply=None))
        out = capsys.readouterr().out
        assert "--apply tok1" in out
        assert "--group <key>" in out

    def test_a_dry_run_with_no_groups_prints_no_hint(self, drive, capsys):
        drive(plan={"groups": [], "plan_token": "tok0"})
        participants.cmd_participants(_args(apply=None))
        out = capsys.readouterr().out
        assert "--apply tok0" not in out


# ===========================================================================
# add_parser: the CLI wiring an operator actually types against
# ===========================================================================
class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        top = argparse.ArgumentParser(prog="fm")
        participants.add_parser(top.add_subparsers(dest="command"))
        return top

    def test_group_is_repeatable(self):
        args = self._parser().parse_args(
            ["participants", "dedupe", "--group", "g1", "--group", "g2"]
        )
        assert args.group == ["g1", "g2"]

    def test_group_defaults_to_None_not_an_empty_list(self):
        """The distinction cmd_participants relies on to tell "no --group was
        given" apart from "an operator explicitly passed none": argparse's
        `append` action defaults to None, not []."""
        args = self._parser().parse_args(["participants", "dedupe"])
        assert args.group is None

    def test_an_unknown_subject_is_refused_by_argparse(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["participants", "delete"])

    def test_review_and_json_are_plain_flags(self):
        args = self._parser().parse_args(["participants", "dedupe", "--review", "--json"])
        assert args.review is True
        assert args.json is True

    def test_apply_takes_a_token_value_not_a_bare_flag(self):
        args = self._parser().parse_args(["participants", "dedupe", "--apply", "tok123"])
        assert args.apply == "tok123"
