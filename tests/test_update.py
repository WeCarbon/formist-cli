"""``fm update`` — the payload transform, the `$expect` copy-back, and the plan token.

This is the command the owner actually asked for: change production data without
writing code. Everything here is about making that safe enough to be boring.

The wire format is EXACTLY what `admin-bulk-data-api` defined — `$action`,
`$kind`, `$id`, `$set`, `$expect`, `$ref`, `apply` defaulting to false, a 500-op
bound. This module adds ONE thing on the operator's side of the wire: `$name`,
resolved client-side seconds before the write, so no UUID is ever transcribed.
The server never sees a `$name`.
"""

from __future__ import annotations

import json

import pytest

from formist_cli.targets import resolve as resolve_target
from formist_cli.update import (
    MAX_OPS,
    UpdateError,
    build_plan,
    plan_token,
)

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _fetcher(rows_by_path=None, **kw):
    """Canned list envelopes keyed by the endpoint's trailing segment."""
    rows_by_path = rows_by_path or {}

    def fetch(target, path, *, token=None, params=None, **kwargs):
        key = path.rstrip("/").rsplit("/", 1)[-1]
        items = rows_by_path.get(key, [])
        q = (params or {}).get("q")
        if q:
            items = [
                i for i in items
                if q in json.dumps(i, ensure_ascii=False)
            ]
        return {"items": items, "total": len(items), "limit": 200, "offset": 0}

    return fetch


STAKEHOLDERS = {
    "stakeholders": [
        {"id": UUID_A, "name": {"en": "ADAFSA", "ar": "أدافسا"}, "org": "Gov"},
        {"id": UUID_B, "name": "ADNEC", "org": "Gov"},
    ]
}


class TestNameResolutionReplacesTheTranscribedUuid:
    """The failure this whole lane exists to remove.

    `$ref` cannot do this: it is a within-batch back-reference to a row the same
    request just created (`services/bulk_data.py:201-227`), so an `update` or
    `delete` op has no way to name an EXISTING row except by literal UUID.
    """

    def test_name_in_id_position_resolves_to_a_uuid(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER",
             "$name": "ADAFSA", "$set": {"org": "FAO"}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        op = plan.ops[0]
        assert op["$id"] == UUID_A
        assert "$name" not in op, "the server must never see a $name"

    def test_the_resolved_op_gains_an_expect_carrying_the_RAW_value(self):
        """THE test in this file.

        The name the client resolved by is handed back to the server as `$expect`,
        so the resolution is re-checked by the row itself. A mis-resolution becomes
        an `expect-mismatch` refusal of the whole batch instead of a confident
        write to the wrong row.

        The value must be the RAW column — `column_snapshot` compares exactly and
        deliberately does not coalesce shapes (`services/bulk_data.py:260-272`).
        Prod stores an {en, ar} map here; flattening it to "ADAFSA" would send an
        `$expect` no prod row can ever match.
        """
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER",
             "$name": "ADAFSA", "$set": {"org": "FAO"}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[0]["$expect"]["name"] == {"en": "ADAFSA", "ar": "أدافسا"}

    def test_a_plain_string_name_is_carried_as_a_plain_string(self):
        payload = {"ops": [
            {"$action": "delete", "$kind": "STAKEHOLDER", "$name": "ADNEC"}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[0]["$expect"]["name"] == "ADNEC"

    def test_an_ambiguous_name_refuses_the_WHOLE_batch(self):
        """Not just the op. The endpoint is all-or-nothing and so is the plan —
        a partially-resolved batch is a plan nobody reviewed."""
        rows = {"stakeholders": [
            {"id": UUID_A, "name": "ADAFSA"}, {"id": UUID_B, "name": "ADAFSA"}
        ]}
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER",
             "$name": "ADAFSA", "$set": {"org": "FAO"}},
            {"$action": "update", "$kind": "STAKEHOLDER",
             "$name": "ADNEC", "$set": {"org": "FAO"}},
        ]}
        with pytest.raises(UpdateError, match="op 0"):
            build_plan(payload, target=LOCAL, space=SPACE, fetch=_fetcher(rows))

    def test_an_unresolvable_name_names_the_op_index(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER",
             "$name": "Nobody", "$set": {"org": "FAO"}}
        ]}
        with pytest.raises(UpdateError, match="op 0"):
            build_plan(
                payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
            )


class TestNameResolutionInsideSet:
    """A FOREIGN reference — `parent_id`, `relationship_owner_id` — is the case the
    brief's rule 1 was actually about: *every organisation id differs between dev
    and prod*, and a wrong one lands in a jsonb column with no FK where nothing
    errors.
    """

    def test_a_name_in_a_set_value_resolves_to_a_uuid(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$id": UUID_B,
             "$set": {"parent_id": {"$name": "ADAFSA"}}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[0]["$set"]["parent_id"] == UUID_A

    def test_a_set_reference_may_name_a_different_kind(self):
        rows = dict(STAKEHOLDERS, tasks=[{"id": UUID_B, "subject": "Draft agenda"}])
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$id": UUID_A,
             "$set": {"task_id": {"$name": "Draft agenda", "$kind": "TASK"}}}
        ]}
        plan = build_plan(payload, target=LOCAL, space=SPACE, fetch=_fetcher(rows))
        assert plan.ops[0]["$set"]["task_id"] == UUID_B

    def test_every_resolution_is_recorded_for_the_operator_to_read(self):
        """A resolution the operator cannot see is a transcribed id with extra
        steps. `$expect` cannot guard a foreign reference (it is a different row),
        so the report IS the verification surface here."""
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$id": UUID_B,
             "$set": {"parent_id": {"$name": "ADAFSA"}}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert any(
            r.name == "ADAFSA" and r.id == UUID_A and r.where == "op 0 $set.parent_id"
            for r in plan.resolutions
        )

    def test_ref_is_left_alone_because_the_server_owns_it(self):
        """`$ref` is the endpoint's within-batch back-reference. Resolving it here
        would be inventing a second implementation of someone else's contract."""
        payload = {"ops": [
            {"$action": "add", "$kind": "STAKEHOLDER", "$ref": "chair",
             "$set": {"name": "Dr Haddad", "type": "PERSON"}},
            {"$action": "add", "$kind": "STAKEHOLDER",
             "$set": {"name": "Deputy", "parent_id": {"$ref": "chair"}}},
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[1]["$set"]["parent_id"] == {"$ref": "chair"}


class TestAnOperatorsOwnExpectIsNeverOverwritten:
    def test_conflicting_expect_on_the_resolved_field_refuses(self):
        """If the operator said they expect a different name than the one the row
        actually holds, they are working from a different belief than the server.
        Silently replacing it would erase the disagreement instead of surfacing it.
        """
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$name": "ADNEC",
             "$set": {"org": "FAO"}, "$expect": {"name": "A.D.N.E.C."}}
        ]}
        with pytest.raises(UpdateError, match="expect"):
            build_plan(
                payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
            )

    def test_a_matching_expect_is_kept(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$name": "ADNEC",
             "$set": {"org": "FAO"}, "$expect": {"name": "ADNEC"}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[0]["$expect"]["name"] == "ADNEC"

    def test_expect_on_OTHER_fields_survives_resolution(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$name": "ADNEC",
             "$set": {"org": "FAO"}, "$expect": {"org": "Gov"}}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.ops[0]["$expect"]["org"] == "Gov"
        assert plan.ops[0]["$expect"]["name"] == "ADNEC"


class TestPayloadShapeGuards:
    def test_apply_is_never_read_from_the_payload_file(self):
        """`apply` is a decision made at the command line, in the moment. A file
        that can turn itself into a commit is a file that commits when someone
        re-runs an old plan."""
        payload = {"apply": True, "ops": [
            {"$action": "delete", "$kind": "STAKEHOLDER", "$name": "ADNEC"}
        ]}
        plan = build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )
        assert plan.body["apply"] is False

    def test_more_than_MAX_OPS_refuses_locally(self):
        """Mirrors `services/bulk_data.py` MAX_OPS=500. Refusing here means the
        operator learns the bound instead of a 422 from pydantic."""
        payload = {"ops": [
            {"$action": "delete", "$kind": "TASK", "$id": UUID_A}
        ] * (MAX_OPS + 1)}
        with pytest.raises(UpdateError, match=str(MAX_OPS)):
            build_plan(payload, target=LOCAL, space=SPACE, fetch=_fetcher())

    def test_empty_ops_refuses(self):
        with pytest.raises(UpdateError, match="no ops"):
            build_plan({"ops": []}, target=LOCAL, space=SPACE, fetch=_fetcher())

    def test_replace_data_is_a_known_key_and_passes_through(self):
        """`$replace_data` (services/bulk_data.py's escape hatch from the default
        `data` MERGE to a wholesale replace) must not be refused as an unknown
        key — this module's `_OP_KEYS` mirrors the endpoint's own vocabulary and
        has to be kept in step with it by hand."""
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$id": UUID_A,
             "$set": {"data": {"country": "AE"}}, "$expect": {"data": {}},
             "$replace_data": True}
        ]}
        plan = build_plan(payload, target=LOCAL, space=SPACE, fetch=_fetcher())
        assert plan.body["ops"][0]["$replace_data"] is True

    def test_a_payload_that_is_not_an_object_refuses(self):
        with pytest.raises(UpdateError):
            build_plan([], target=LOCAL, space=SPACE, fetch=_fetcher())

    def test_both_id_and_name_on_one_op_refuses(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$id": UUID_A,
             "$name": "ADNEC", "$set": {"org": "FAO"}}
        ]}
        with pytest.raises(UpdateError, match="both"):
            build_plan(
                payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
            )


class TestPlanToken:
    """A plan token proves the operator saw THIS dry run, for THIS target.

    It is not a confirmation prompt: `yes |` has nothing to answer, and a script
    can carry it, so automation stays possible while staying explicit.
    """

    def _plan(self, space=SPACE, target=LOCAL, org="FAO"):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$name": "ADNEC",
             "$set": {"org": org}}
        ]}
        return build_plan(
            payload, target=target, space=space, fetch=_fetcher(STAKEHOLDERS)
        )

    def test_same_inputs_give_the_same_token(self):
        report = {"counts": {"ops": 1}}
        assert plan_token(self._plan(), report) == plan_token(self._plan(), report)

    def test_a_different_env_gives_a_different_token(self):
        report = {"counts": {"ops": 1}}
        a = plan_token(self._plan(target=resolve_target("127.0.0.1:8160")), report)
        b = plan_token(self._plan(target=resolve_target("evane-ae")), report)
        assert a != b, "a plan must not be portable between environments"

    def test_a_different_space_gives_a_different_token(self):
        report = {"counts": {"ops": 1}}
        other = "11111111-2222-3333-4444-555555555555"
        assert plan_token(self._plan(), report) != plan_token(
            self._plan(space=other), report
        )

    def test_a_changed_op_gives_a_different_token(self):
        report = {"counts": {"ops": 1}}
        assert plan_token(self._plan(), report) != plan_token(
            self._plan(org="WHO"), report
        )

    def test_a_changed_dry_run_result_gives_a_different_token(self):
        """The dry run's own result is in the hash, so a plan whose meaning changed
        server-side — a row that moved under it — cannot be applied on an old
        token even though the ops are byte-identical."""
        plan = self._plan()
        assert plan_token(plan, {"counts": {"ops": 1}}) != plan_token(
            plan, {"counts": {"ops": 2}}
        )

    def test_token_is_short_enough_to_paste(self):
        assert len(plan_token(self._plan(), {})) <= 16


class TestPlanTokenIgnoresPerRunIdentifiers:
    """Found end-to-end against the real endpoint, not by unit test.

    A dry run EXECUTES every op and rolls back (`services/bulk_data.py` property
    2), so the server mints a fresh `audit_id` on every call — and a fresh row `id`
    for every `add`. Hashing the report verbatim therefore produced a DIFFERENT
    token each time the same plan was dry-run:

        run 1 → 646aba0f42c0
        run 2 → 50c1104c14a3

    which makes `--apply <token>` impossible to satisfy: the token printed by the
    dry run never matches the one recomputed at apply time. The guard would have
    been not merely useless but permanently closed — a check that always fails is
    as broken as one that never does, and this one would have been discovered by
    the owner, mid-change, on the first real use.

    So the token hashes a PROJECTION of the report: what the run meant, never what
    the run happened to name.
    """

    def _plan(self):
        payload = {"ops": [
            {"$action": "update", "$kind": "STAKEHOLDER", "$name": "ADNEC",
             "$set": {"org": "FAO"}, "$expect": {"org": "Gov"}}
        ]}
        return build_plan(
            payload, target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS)
        )

    def _report(self, audit_id, outcome="would-apply", cascaded=0):
        return {
            "mode": "dry-run", "committed": False,
            "counts": {"ops": 1, "ok": 1, "refused": 0, "applied": 0},
            "results": [{
                "index": 0, "action": "update", "kind": "STAKEHOLDER",
                "id": UUID_B, "audit_id": audit_id,
                "cascaded": cascaded, "outcome": outcome,
            }],
            "refusals": [],
        }

    def test_a_different_audit_id_gives_the_SAME_token(self):
        plan = self._plan()
        assert plan_token(plan, self._report("c25e0633-2fa9-43bd-85a6-e7977c446dd2")) \
            == plan_token(plan, self._report("2afa22b4-38fd-456e-b97f-da60f14374f6"))

    def test_a_generated_id_on_an_ADD_does_not_move_the_token(self):
        """An `add`'s id is minted by the insert the dry run then rolls back, so it
        is a per-run identifier exactly like `audit_id`. An `update`'s id comes
        from the plan and must still count."""
        plan = build_plan(
            {"ops": [{"$action": "add", "$kind": "STAKEHOLDER",
                      "$set": {"name": "New Org"}}]},
            target=LOCAL, space=SPACE, fetch=_fetcher(STAKEHOLDERS),
        )
        def make(row_id):
            return {
                "counts": {"ops": 1, "ok": 1, "refused": 0, "applied": 0},
                "results": [{"index": 0, "action": "add", "kind": "STAKEHOLDER",
                             "id": row_id, "outcome": "would-apply"}],
            }

        assert plan_token(plan, make(UUID_A)) == plan_token(plan, make(UUID_B))

    def test_a_changed_OUTCOME_still_moves_the_token(self):
        """The projection must not throw away meaning along with the noise."""
        plan = self._plan()
        assert plan_token(plan, self._report("x", outcome="would-apply")) \
            != plan_token(plan, self._report("x", outcome="refused"))

    def test_a_changed_cascade_size_still_moves_the_token(self):
        plan = self._plan()
        assert plan_token(plan, self._report("x", cascaded=0)) \
            != plan_token(plan, self._report("x", cascaded=7))

    def test_a_changed_count_still_moves_the_token(self):
        plan = self._plan()
        a = self._report("x")
        b = self._report("x")
        b["counts"]["refused"] = 1
        assert plan_token(plan, a) != plan_token(plan, b)

    def test_a_refusal_reason_still_moves_the_token(self):
        plan = self._plan()
        a = self._report("x")
        b = self._report("x")
        b["refusals"] = [{"index": 0, "reason": "expect-mismatch"}]
        assert plan_token(plan, a) != plan_token(plan, b)
