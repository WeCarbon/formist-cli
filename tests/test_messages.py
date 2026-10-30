"""Tests for ``fm messages``: the group-mailbox message surface.

Two write-capable subjects live behind one command:

  * ``dedupe`` / ``dedupe-scan``: soft-delete duplicate `message` rows.
    A whole-connection apply is never allowed: ``--apply`` must be scoped
    to explicit ``--group`` remote_ids plus a ``--reason``, exactly the
    same shape as ``fm participants dedupe``.
  * ``refresh-inline-images``: dry run unless ``--apply``.

Covers the request bodies these subjects send (dry run vs apply must
differ in exactly the documented fields, never silently gain or lose one),
the reports they render (DRY RUN / APPLIED banners, the truncation-free
connection table, the zip(strict=True) pairing between a group's ``drop``
ids and ``drop_last_synced_at``, where a length mismatch must raise
loudly, never drop the last row silently), and the two required-argument
refusals that never reach the network (`dedupe` needs ``--connection``;
`refresh-inline-images` needs a UUID ``--space``).

HTTP transport is stubbed at the module level (``messages.api``).
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import messages
from formist_cli.context import Context
from formist_cli.targets import TargetError
from formist_cli.targets import resolve as resolve_target

GLOBAL = resolve_target("evane")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CONN = "11111111-1111-1111-1111-111111111111"


class RecordingApi:
    def __init__(self, *, response=None):
        self.calls: list[tuple[str, str, object]] = []
        self._response = response if response is not None else {}

    def admin_get(self, target, path, *, token=None, **kw):
        self.calls.append(("GET", path, None))
        return self._response

    def admin_post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path, body))
        return self._response


def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": None, "host": "evane-ae", "connection": None, "space": None,
        "limit": None, "group": None, "reason": None, "plan_token": None,
        "apply": False, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    captured_resolve_kwargs: dict = {}

    def _drive(api_stub, *, ctx_space=SPACE, **overrides):
        def fake_resolve(args, **kw):
            captured_resolve_kwargs.update(kw)
            return Context(target=GLOBAL, space=ctx_space, token="t", space_source="flag")

        monkeypatch.setattr(messages, "api", api_stub)
        monkeypatch.setattr(messages.context, "resolve", fake_resolve)
        rc = messages.run(_args(**overrides))
        return rc, captured_resolve_kwargs

    return _drive


# ===========================================================================
# canonical_subject
# ===========================================================================
class TestCanonicalSubject:
    def test_none_is_dedupe_scan(self):
        assert messages.canonical_subject(None) == "dedupe-scan"

    def test_known_subjects_pass_through_lowercased_and_stripped(self):
        assert messages.canonical_subject(" Dedupe ") == "dedupe"

    def test_unknown_subject_refuses_and_names_the_valid_ones(self):
        with pytest.raises(messages.MessagesError) as exc:
            messages.canonical_subject("delete")
        assert "dedupe-scan" in str(exc.value)
        assert "refresh-inline-images" in str(exc.value)


# ===========================================================================
# render_scan
# ===========================================================================
class TestRenderScan:
    def test_header_counts(self):
        out = messages.render_scan(
            {"connections": 3, "connections_with_duplicates": 1, "groups": 5, "rows_to_soft_delete": 10, "detail": []}
        )
        assert "OUTLOOK connections: 3" in out
        assert "with duplicates: 1" in out
        assert "duplicate groups:    5" in out

    def test_no_connections_says_so_instead_of_an_empty_table(self):
        out = messages.render_scan({"connections": 0, "detail": []})
        assert "no OUTLOOK connections on this instance." in out

    def test_a_connection_row_is_printed(self):
        out = messages.render_scan(
            {
                "connections": 1, "connections_with_duplicates": 1, "groups": 2,
                "rows_to_soft_delete": 4,
                "detail": [{"connection_id": CONN, "groups": 2, "rows_in_groups": 6, "rows_to_soft_delete": 4}],
            }
        )
        assert CONN in out
        assert "connection" in out  # header row present


# ===========================================================================
# render_dedupe
# ===========================================================================
def _dedupe_payload(**overrides) -> dict:
    base = {
        "applied": False,
        "connection_id": CONN,
        "groups": 1,
        "rows_in_groups": 2,
        "rows_to_soft_delete": 1,
        "dependents": {"proposal": 2, "status_point": 1},
        "detail": [
            {
                "remote_id": "conv-1",
                "keep": "m1",
                "keep_last_synced_at": "2026-09-07T00:00:00",
                "drop": ["m2"],
                "drop_last_synced_at": ["2026-09-06T00:00:00"],
            }
        ],
        "plan_token": "msgtok1",
    }
    return base | overrides


class TestRenderDedupe:
    def test_dry_run_header_says_nothing_written(self):
        out = messages.render_dedupe(_dedupe_payload(applied=False))
        assert "DRY RUN — nothing was written" in out

    def test_applied_header_says_applied(self):
        out = messages.render_dedupe(_dedupe_payload(applied=True, reason="dup", groups_applied=["conv-1"], soft_deleted=1, repointed=2))
        assert out.splitlines()[0].startswith("APPLIED")

    def test_dependents_are_listed(self):
        out = messages.render_dedupe(_dedupe_payload())
        assert "proposal" in out and "2" in out
        assert "status_point" in out

    def test_keep_and_drop_rows_are_paired_by_index(self):
        out = messages.render_dedupe(_dedupe_payload())
        assert "KEEP m1  last_synced=2026-09-07T00:00:00" in out
        assert "drop m2  last_synced=2026-09-06T00:00:00" in out

    def test_a_drop_list_length_mismatch_raises_rather_than_dropping_the_last_row(self):
        """A property, not a single value: the zip(strict=True) pairing
        must never silently truncate to the shorter list."""
        payload = _dedupe_payload()
        payload["detail"][0]["drop_last_synced_at"] = []  # one fewer than `drop`
        with pytest.raises(ValueError):
            messages.render_dedupe(payload)

    def test_dry_run_with_a_plan_token_shows_it_and_the_staleness_warning(self):
        out = messages.render_dedupe(_dedupe_payload(applied=False))
        assert "plan token:   msgtok1" in out
        assert "--plan-token msgtok1" in out
        assert "refused 409" in out

    def test_applied_never_shows_the_plan_token_block(self):
        out = messages.render_dedupe(
            _dedupe_payload(applied=True, reason="dup", groups_applied=["conv-1"], soft_deleted=1, repointed=2)
        )
        assert "plan token:" not in out

    def test_applied_shows_reason_and_applied_groups(self):
        out = messages.render_dedupe(
            _dedupe_payload(applied=True, reason="same duplicate cluster", groups_applied=["conv-1"], soft_deleted=1, repointed=2)
        )
        assert "reason:       'same duplicate cluster'" in out
        assert "groups applied: ['conv-1']" in out

    def test_groups_not_named_are_called_out_when_present(self):
        out = messages.render_dedupe(
            _dedupe_payload(applied=True, reason="x", groups_applied=["conv-1"], groups_skipped=["conv-2"], soft_deleted=1, repointed=0)
        )
        assert "groups NOT named" in out
        assert "conv-2" in out

    def test_groups_not_named_is_omitted_when_nothing_was_skipped(self):
        out = messages.render_dedupe(
            _dedupe_payload(applied=True, reason="x", groups_applied=["conv-1"], groups_skipped=[], soft_deleted=1, repointed=0)
        )
        assert "groups NOT named" not in out

    def test_unknown_groups_are_flagged(self):
        out = messages.render_dedupe(
            _dedupe_payload(applied=True, reason="x", groups_applied=[], unknown_groups=["conv-9"], soft_deleted=0, repointed=0)
        )
        assert "requested groups that are not currently duplicates" in out
        assert "conv-9" in out

    def test_status_point_conflicts_shown_only_when_nonzero(self):
        applied_common = dict(applied=True, reason="x", groups_applied=["conv-1"], soft_deleted=1, repointed=1)
        out_with = messages.render_dedupe(_dedupe_payload(**applied_common, status_point_conflicts=2))
        out_without = messages.render_dedupe(_dedupe_payload(**applied_common, status_point_conflicts=0))
        assert "status points NOT re-pointed" in out_with
        assert "status points NOT re-pointed" not in out_without


# ===========================================================================
# render_refresh_inline_images / next_command
# ===========================================================================
def _refresh_payload(**overrides) -> dict:
    base = {
        "target": {"space_name": "Acme Summit", "space_id": SPACE, "project_name": "Acme", "project_id": "proj1", "region": "ae"},
        "dry_run": True,
        "connections": 2,
        "candidates": 5,
        "sample_remote_ids": ["r1", "r2"],
    }
    return base | overrides


class TestNextCommand:
    def test_builds_the_exact_apply_command(self):
        out = messages.next_command(_refresh_payload())
        assert out == f"fm messages refresh-inline-images --env <env> --space {SPACE} --apply"


class TestRenderRefreshInlineImages:
    def test_dry_run_shows_samples_and_the_reapply_command(self):
        out = messages.render_refresh_inline_images(_refresh_payload())
        assert "DRY RUN (writes nothing)" in out
        assert "r1" in out and "r2" in out
        assert "NOTHING WAS WRITTEN" in out
        assert messages.next_command(_refresh_payload()) in out

    def test_dry_run_with_no_candidates_omits_the_sample_section(self):
        out = messages.render_refresh_inline_images(_refresh_payload(candidates=0, sample_remote_ids=[]))
        assert "sample remote_ids" not in out
        assert "NOTHING WAS WRITTEN" in out

    def test_applied_shows_the_outcome_counts_not_the_samples(self):
        out = messages.render_refresh_inline_images(
            _refresh_payload(dry_run=False, refreshed=3, unchanged=1, gone=1, errors=0)
        )
        assert "APPLIED" in out
        assert "refreshed  3" in out
        assert "gone       1" in out
        assert "r1" not in out

    def test_region_note_says_it_is_resolved_by_the_server(self):
        out = messages.render_refresh_inline_images(_refresh_payload())
        assert "resolved by the" in out
        assert "not from --env" in out


# ===========================================================================
# dedupe-scan (read-only)
# ===========================================================================
class TestDedupeScan:
    def test_hits_the_scan_endpoint_with_a_GET(self, driven):
        stub = RecordingApi(response={"connections": 0, "detail": []})
        rc, kwargs = driven(stub, subject="dedupe-scan")
        assert rc == 0
        assert stub.calls == [("GET", messages.SCAN_ENDPOINT, None)]
        assert kwargs == {"need_token": True}

    def test_json_flag_prints_raw_json(self, driven, capsys):
        stub = RecordingApi(response={"connections": 4, "detail": []})
        driven(stub, subject="dedupe-scan", **{"json": True})
        assert '"connections": 4' in capsys.readouterr().out

    def test_non_dict_response_is_a_loud_error(self, driven):
        stub = RecordingApi(response="oops")
        with pytest.raises(messages.MessagesError):
            driven(stub, subject="dedupe-scan")


# ===========================================================================
# dedupe: the no-whole-connection-apply tests
# ===========================================================================
class TestDedupeRequiresAConnection:
    def test_missing_connection_refuses_before_any_request(self, driven):
        stub = RecordingApi(response={})
        with pytest.raises(messages.MessagesError, match="--connection"):
            driven(stub, subject="dedupe", connection=None)
        assert stub.calls == []

    def test_blank_connection_refuses_too(self, driven):
        stub = RecordingApi(response={})
        with pytest.raises(messages.MessagesError):
            driven(stub, subject="dedupe", connection="   ")
        assert stub.calls == []


class TestDedupeDryRunBody:
    def test_dry_run_sends_apply_false_and_no_group_reason_or_token_keys(self, driven):
        """The client does not invent an empty --group/--reason on a dry
        run: those keys are added only inside `if args.apply`."""
        stub = RecordingApi(response=_dedupe_payload(applied=False))
        rc, kwargs = driven(stub, subject="dedupe", connection=CONN, apply=False)
        assert rc == 0
        method, path, body = stub.calls[0]
        assert method == "POST"
        assert path == messages.DEDUPE_ENDPOINT
        assert body == {"connection_id": CONN, "apply": False}
        assert isinstance(body, dict)  # narrows the stub's `object` for pyright
        assert "groups" not in body and "reason" not in body and "plan_token" not in body

    def test_dry_run_still_resolves_as_a_write(self, driven):
        """Same rule as fm participants dedupe: a dry run of a command whose
        subject is a write resolves its environment the same way --apply
        will, so the banner it prints never lies about the target."""
        stub = RecordingApi(response=_dedupe_payload(applied=False))
        _, kwargs = driven(stub, subject="dedupe", connection=CONN, apply=False)
        assert kwargs == {"for_write": True, "need_token": True}


class TestDedupeApplyForwardsExactlyWhatWasNamed:
    def test_group_reason_and_plan_token_reach_the_body_unchanged(self, driven):
        stub = RecordingApi(response=_dedupe_payload(applied=True, reason="same cluster", groups_applied=["conv-1"], soft_deleted=1, repointed=1))
        driven(
            stub, subject="dedupe", connection=CONN, apply=True,
            group=["conv-1"], reason="same cluster", plan_token="msgtok1",
        )
        body = stub.calls[0][2]
        assert body == {
            "connection_id": CONN, "apply": True,
            "groups": ["conv-1"], "reason": "same cluster", "plan_token": "msgtok1",
        }

    def test_an_omitted_group_forwards_None_the_client_never_invents_a_wildcard(self, driven):
        """A client-side 'merge everything' default is the exact shape that
        must never happen. `--group` not given must reach the server as
        None, never as [] or '*': only the server's 422 owns the refusal."""
        stub = RecordingApi(response=_dedupe_payload(applied=True, reason="x", groups_applied=[], soft_deleted=0, repointed=0))
        driven(stub, subject="dedupe", connection=CONN, apply=True, group=None, reason="x")
        body = stub.calls[0][2]
        assert isinstance(body, dict)  # narrows the stub's `object` for pyright
        assert body["groups"] is None


class TestDedupeResponseContract:
    def test_non_dict_response_is_a_loud_error(self, driven):
        stub = RecordingApi(response="oops")
        with pytest.raises(messages.MessagesError):
            driven(stub, subject="dedupe", connection=CONN, apply=False)

    def test_json_flag_prints_raw_json(self, driven, capsys):
        stub = RecordingApi(response=_dedupe_payload(applied=False))
        driven(stub, subject="dedupe", connection=CONN, apply=False, **{"json": True})
        assert '"connection_id"' in capsys.readouterr().out


# ===========================================================================
# refresh-inline-images
# ===========================================================================
class TestRefreshInlineImagesRequiresASpace:
    def test_missing_space_raises(self, driven):
        stub = RecordingApi(response=_refresh_payload())
        with pytest.raises(TargetError):
            driven(stub, subject="refresh-inline-images", ctx_space=None)
        assert stub.calls == []

    def test_a_name_instead_of_a_uuid_is_refused(self, driven):
        stub = RecordingApi(response=_refresh_payload())
        with pytest.raises(TargetError):
            driven(stub, subject="refresh-inline-images", ctx_space="Acme Summit")
        assert stub.calls == []


class TestRefreshInlineImagesBody:
    def test_dry_run_sends_dry_run_true_and_no_limit_when_not_given(self, driven):
        stub = RecordingApi(response=_refresh_payload())
        rc, kwargs = driven(stub, subject="refresh-inline-images", space=SPACE, apply=False)
        assert rc == 0
        method, path, body = stub.calls[0]
        assert method == "POST"
        assert path == messages.REFRESH_INLINE_IMAGES_ENDPOINT.format(space=SPACE)
        assert body == {"dry_run": True}
        assert kwargs == {"for_write": True, "need_space": True, "need_token": True}

    def test_apply_sends_dry_run_false(self, driven):
        stub = RecordingApi(response=_refresh_payload(dry_run=False, refreshed=1, unchanged=0, gone=0, errors=0))
        driven(stub, subject="refresh-inline-images", space=SPACE, apply=True)
        assert stub.calls[0][2] == {"dry_run": False}

    def test_limit_is_forwarded_only_when_given(self, driven):
        stub = RecordingApi(response=_refresh_payload())
        driven(stub, subject="refresh-inline-images", space=SPACE, apply=False, limit=25)
        assert stub.calls[0][2] == {"dry_run": True, "limit": 25}

    def test_non_dict_response_is_a_loud_error(self, driven):
        stub = RecordingApi(response="oops")
        with pytest.raises(messages.MessagesError):
            driven(stub, subject="refresh-inline-images", space=SPACE, apply=False)

    def test_json_flag_prints_raw_json(self, driven, capsys):
        stub = RecordingApi(response=_refresh_payload(candidates=9))
        driven(stub, subject="refresh-inline-images", space=SPACE, apply=False, **{"json": True})
        assert '"candidates": 9' in capsys.readouterr().out


# ===========================================================================
# run() dispatch
# ===========================================================================
class TestRunDispatch:
    def test_default_subject_is_dedupe_scan(self, driven):
        stub = RecordingApi(response={"connections": 0, "detail": []})
        driven(stub, subject=None)
        assert stub.calls[0][1] == messages.SCAN_ENDPOINT

    def test_unknown_subject_is_refused_before_any_request(self, driven):
        stub = RecordingApi(response={})
        with pytest.raises(messages.MessagesError):
            driven(stub, subject="bogus")
        assert stub.calls == []


# ===========================================================================
# add_parser: the CLI wiring
# ===========================================================================
class TestAddParser:
    def _parser(self) -> argparse.ArgumentParser:
        top = argparse.ArgumentParser(prog="fm")
        messages.add_parser(top.add_subparsers(dest="command"))
        return top

    def test_group_is_repeatable_and_defaults_to_None(self):
        p = self._parser()
        args = p.parse_args(["messages", "dedupe", "--group", "a", "--group", "b"])
        assert args.group == ["a", "b"]
        args2 = p.parse_args(["messages", "dedupe"])
        assert args2.group is None

    def test_apply_is_a_bare_flag(self):
        args = self._parser().parse_args(["messages", "dedupe", "--apply"])
        assert args.apply is True

    def test_env_choices_reject_an_unlisted_region(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["messages", "dedupe-scan", "--env", "prod"])

    def test_standalone_parser_names_the_no_whole_connection_apply_rule(self):
        text = messages.build_standalone_parser().format_help()
        assert "NO WHOLE-CONNECTION APPLY" in text
