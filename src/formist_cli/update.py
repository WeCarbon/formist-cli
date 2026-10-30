"""``fm update`` - a bulk data change an operator can make without writing code.

The wire format is the server's, not this module's: ``POST
/projects/{project_id}/admin/data-changes`` defines ``$action``, ``$kind``,
``$id``, ``$set``, ``$expect``, ``$ref``, ``$replace_data``, ``$hard``,
``apply`` defaulting to false, and a 500-op bound. This module does not
invent a second format and does not reinterpret that one. It adds exactly
one thing, on the operator's side of the wire: ``$name``, resolved to an
``$id`` here, in the client, seconds before the write, against the
environment being written. The server never sees a ``$name``.

Why ``$name`` has to exist: the endpoint's ``$ref`` looks like it covers
this and does not. It is a strictly backward, within-batch reference to a
row the same request just created, so an ``update`` or a ``delete`` has no
way to name an existing row except by literal UUID, and a wrong one lands
in a jsonb column with no foreign key and nothing errors. Every
organisation's id differs between environments, which is exactly the kind
of id an operator should never type or copy by hand.

The copy-back is the point, not the convenience. Resolving a name
client-side would, on its own, just move the guess. So the name the
client resolved by is written back into ``$expect``, carrying the raw
column value the server returned. The server then re-checks it exactly,
without coalescing shapes. A mis-resolution is therefore refused by the
row itself rather than written to confidently: the client's answer is
verified by the thing it is about to change, structurally, rather than by
anyone remembering to check.

A foreign reference inside ``$set`` (``parent_id`` and friends) cannot be
guarded that way: it points at a different row, and ``$expect`` only
describes the row being changed. There, the guard is the plan report:
every resolution is recorded and printed, and the server's own
unresolved-reference check refuses a reference that is not a real row in
the project. Between them, a wrong foreign id is visible before the write
and refused during it.

A partial ``data`` merges, not replaces. STAKEHOLDER and CUSTOM rows carry
a free-form ``data`` JSONB blob. ``$set`` naming ``data`` merges into
whatever the row already holds, so ``{"data": {"country": "AE"}}``
changes only ``country``, and every other key on the row survives. This
is the server's default, not something this module does; it matters here
because ``$expect`` still makes you name the row's current full ``data``
value, which is a different thing from the partial value you are
writing. Pass ``"$replace_data": true`` in an op to replace the whole
blob instead, the only way to remove a key, since a merge keeps every key
the payload does not mention.

A delete is soft, not permanent. ``$action: "delete"`` stamps
``deleted_at`` and lands the row in the space's trash, restorable,
exactly like the UI's trash icon. ``$hard`` is refused for a kind that has
a trash entry (purge one from there instead) and required for the two
that do not (FILE_LINK, SESSION_PARTICIPANT), whose delete has always
been permanent.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from formist_cli.resolve import ResolutionError, resolve_one
from formist_cli.targets import Target

#: Mirrors the server's own op-count bound. Duplicated deliberately rather
#: than imported: `fm` is stdlib-only and must run without the server's
#: dependency tree. Refusing here means the operator reads a sentence
#: about the bound instead of a validation error from the server.
MAX_OPS = 500

#: The endpoint's own vocabulary. Listed so a typo in a payload is a legible
#: refusal rather than an ``extra: forbid`` validation error from the server.
#: ``$replace_data`` opts an op back into wholesale-replacing ``$set.data``
#: instead of the server's default merge, and ``$hard`` names an explicit
#: hard/soft delete choice.
_OP_KEYS = {
    "$action", "$kind", "$id", "$set", "$expect", "$ref",
    "$replace_data", "$hard",
}
_ACTIONS = {"add", "update", "delete"}


class UpdateError(Exception):
    """A payload that will not be sent. Written for an operator."""


@dataclass(frozen=True)
class Resolution:
    """One ``$name`` that became an id, and where it was."""

    where: str
    kind: str
    name: str
    id: str
    #: The raw column value the match was made on, what goes into ``$expect``.
    value: Any = None
    field_name: str | None = None

    def describe(self) -> str:
        return f"{self.where}: {self.kind} {self.name!r} -> {self.id}"


@dataclass(frozen=True)
class Plan:
    """A resolved, ready-to-send request plus what had to be looked up to build it."""

    env: str
    space: str
    ops: list[dict]
    resolutions: list[Resolution] = field(default_factory=list)

    @property
    def body(self) -> dict:
        """The request body. ``apply`` is always False here, see :func:`send`."""
        return {"apply": False, "ops": self.ops}


def load_payload(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise UpdateError(f"no such payload file: {path}") from None
    except json.JSONDecodeError as exc:
        raise UpdateError(f"{path} is not valid JSON: {exc}") from None
    except OSError as exc:
        raise UpdateError(f"could not read {path}: {exc}") from None


def _is_name_ref(value: Any) -> bool:
    return isinstance(value, dict) and "$name" in value


def _resolve_name(
    name: str,
    kind: str,
    where: str,
    *,
    target: Target,
    space: str,
    token: str | None,
    fetch: Callable | None,
):
    try:
        return resolve_one(
            name, kind=kind, target=target, space=space, token=token, fetch=fetch
        )
    except ResolutionError as exc:
        # The op index is the thing the operator needs; the endpoint reports
        # refusals by index too, so the two vocabularies match.
        raise UpdateError(f"{where}: {exc}") from None


def build_plan(
    payload: Any,
    *,
    target: Target,
    space: str,
    token: str | None = None,
    fetch: Callable | None = None,
) -> Plan:
    """Validate a payload and resolve every ``$name`` in it, or refuse entirely.

    All-or-nothing, matching the endpoint: a partially resolved batch is a plan
    nobody reviewed.
    """
    if not isinstance(payload, dict):
        raise UpdateError(
            "a payload must be a JSON object like "
            '{"ops": [...]}, got a ' + type(payload).__name__ + "."
        )
    ops = payload.get("ops")
    if not isinstance(ops, list):
        raise UpdateError('a payload must carry an "ops" list.')
    if not ops:
        raise UpdateError("no ops in this payload, nothing to do.")
    if len(ops) > MAX_OPS:
        raise UpdateError(
            f"{len(ops)} ops, over the {MAX_OPS}-op bound the endpoint enforces. "
            "The bound caps transaction duration; split the change into batches."
        )

    resolved_ops: list[dict] = []
    resolutions: list[Resolution] = []

    for index, op in enumerate(ops):
        where = f"op {index}"
        if not isinstance(op, dict):
            raise UpdateError(f"{where}: an op must be a JSON object.")

        action = op.get("$action")
        if action not in _ACTIONS:
            raise UpdateError(
                f"{where}: $action must be one of {', '.join(sorted(_ACTIONS))}; "
                f"got {action!r}."
            )
        kind = op.get("$kind")
        if not isinstance(kind, str):
            raise UpdateError(f"{where}: $kind is required and must be a string.")

        unknown = set(op) - _OP_KEYS - {"$name"}
        if unknown:
            raise UpdateError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))}. The endpoint "
                f"forbids extras; valid keys are {', '.join(sorted(_OP_KEYS))} "
                "(plus fm's own $name)."
            )

        new_op = {k: v for k, v in op.items() if k != "$name"}

        # --- $name in the $id position -------------------------------------
        if "$name" in op:
            if "$id" in op:
                raise UpdateError(
                    f"{where}: carries both $id and $name. They are two answers to "
                    "the same question; keep the one you trust."
                )
            match = _resolve_name(
                str(op["$name"]), kind, where,
                target=target, space=space, token=token, fetch=fetch,
            )
            new_op["$id"] = match.id

            # The copy-back. This is what makes the resolution self-checking.
            expect = dict(new_op.get("$expect") or {})
            if match.field in expect and expect[match.field] != match.value:
                raise UpdateError(
                    f"{where}: $expect says {match.field} is "
                    f"{expect[match.field]!r}, but the row resolved by name "
                    f"{op['$name']!r} holds {match.value!r}.\n"
                    "Refusing rather than replacing your expectation: you and the "
                    "server disagree about this row, and that disagreement is the "
                    "finding, not an inconvenience."
                )
            expect[match.field] = match.value
            new_op["$expect"] = expect

            resolutions.append(
                Resolution(
                    where=where, kind=kind, name=str(op["$name"]), id=match.id,
                    value=match.value, field_name=match.field,
                )
            )

        # --- $name inside $set (a foreign reference) ------------------------
        set_ = new_op.get("$set")
        if isinstance(set_, dict):
            new_set = {}
            for column, value in set_.items():
                if _is_name_ref(value):
                    ref_kind = value.get("$kind") or kind
                    ref_name = str(value["$name"])
                    ref_where = f"{where} $set.{column}"
                    match = _resolve_name(
                        ref_name, str(ref_kind), ref_where,
                        target=target, space=space, token=token, fetch=fetch,
                    )
                    new_set[column] = match.id
                    resolutions.append(
                        Resolution(
                            where=ref_where, kind=str(ref_kind), name=ref_name,
                            id=match.id, value=match.value, field_name=match.field,
                        )
                    )
                else:
                    # `$ref` is the endpoint's within-batch back-reference and is
                    # left exactly as written. Resolving it here would be a second
                    # implementation of someone else's contract.
                    new_set[column] = value
            new_op["$set"] = new_set

        resolved_ops.append(new_op)

    return Plan(
        env=target.env, space=space, ops=resolved_ops, resolutions=resolutions
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def plan_token(plan: Plan, dry_run_report: Any) -> str:
    """A short token proving the operator saw this dry run for this target.

    Hashed over the environment, the space, the resolved ops, and the dry run's own
    result. Each is load-bearing:

    - env and space, so a plan is not portable. A token minted against local
      cannot apply against ``ae``.
    - resolved ops, so a re-resolution that landed on a different row (someone
      renamed something in between) invalidates it.
    - the dry-run report, so a plan whose meaning changed server-side is
      refused even when the ops are byte-identical: the dry run executes for real
      and rolls back, so its report reflects live state.

    This is deliberately not a y/n confirmation: there is nothing for ``yes |`` to
    answer, and a script can carry the token, so automation stays possible while
    staying explicit. It is also not a retyped id prefix: eight characters are not
    a reliable discriminator across a real dataset.
    """
    digest = hashlib.sha256(
        _canonical(
            {
                "env": plan.env,
                "space": plan.space,
                "ops": plan.ops,
                "report": _stable_report(plan, dry_run_report),
            }
        ).encode()
    ).hexdigest()
    return digest[:12]


def _stable_report(plan: Plan, report: Any) -> Any:
    """What the dry run meant, with what it happened to name stripped out.

    A dry run executes every op for real and rolls back, so the server
    mints a fresh ``audit_id`` on every call and a fresh row ``id`` for
    every ``add``. Hashing the report verbatim would therefore produce a
    different token on each run of an unchanged plan, making
    ``--apply <token>`` impossible to satisfy.

    So the projection keeps ``counts``, each result's
    ``index``/``action``/``kind``/``outcome``/``cascaded``, and every
    refusal's ``index``/``reason``, all of which change when the plan's
    meaning changes. A result's ``id`` is kept only where the plan already
    determined it (the op carried an ``$id``); on an ``add`` the id is
    minted by the insert and is per-run noise, exactly like ``audit_id``.
    """
    if not isinstance(report, dict):
        return report

    plan_ids = [op.get("$id") for op in plan.ops]

    results = []
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        index = result.get("index")
        kept = {
            "index": index,
            "action": result.get("action"),
            "kind": result.get("kind"),
            "outcome": result.get("outcome"),
            "cascaded": result.get("cascaded"),
            "changed": result.get("changed"),
        }
        # Plan-determined ids pin the row; server-generated ones are noise.
        if isinstance(index, int) and 0 <= index < len(plan_ids) and plan_ids[index]:
            kept["id"] = result.get("id")
        results.append(kept)

    refusals = [
        {"index": r.get("index"), "reason": r.get("reason")}
        for r in (report.get("refusals") or [])
        if isinstance(r, dict)
    ]

    return {
        "counts": report.get("counts"),
        "committed": report.get("committed"),
        "results": results,
        "refusals": refusals,
    }


ENDPOINT = "/projects/{space}/admin/data-changes"

#: Said in `--help` and in the 404 so a missing endpoint fails legibly
#: rather than as a mystery. Deliberately names no branch, PR or
#: deployment state, since none of those are facts a client can hold; it
#: describes what a 404 means and names the command that measures the
#: answer.
NOT_DEPLOYED_NOTE = (
    "NOTE: if a region answers 404 here, the bulk endpoint is not in the image that "
    "region is running, and `fm update` can only work against a server that serves "
    "it. `fm instance --host <host>` reports what is actually deployed there; fm "
    "cannot tell a missing route from a wrong one from the client side."
)


def dry_run(plan: Plan, target: Target, *, token: str | None, post=None) -> Any:
    """Execute the plan with ``apply:false``.

    The endpoint runs every op for real and rolls the transaction back,
    so this reports the ids the inserts actually produced rather than ids
    it predicted.
    """
    from formist_cli import api

    post = post or api.post
    return post(target, ENDPOINT.format(space=plan.space), plan.body, token=token)


def apply(
    plan: Plan,
    target: Target,
    *,
    token: str | None,
    supplied_token: str,
    post=None,
) -> Any:
    """Re-run the dry run, check the operator's plan token, then commit.

    The dry run is repeated inside this call rather than trusted from a previous
    one, because a token proves the operator saw a plan, not that the plan is still
    true. Re-running is what makes the token a check instead of a receipt.
    """
    from formist_cli import api

    post = post or api.post
    report = dry_run(plan, target, token=token, post=post)
    current = plan_token(plan, report)
    if current != supplied_token:
        raise UpdateError(
            "REFUSING TO APPLY: this plan no longer matches the one you were shown.\n"
            f"  you supplied: {supplied_token}\n"
            f"  current plan: {current}\n"
            "\n"
            "Nothing was applied. The token covers the environment, the space, the "
            "resolved ops and the dry run's own result, so one of those changed: a "
            "row moved under the plan, a name now resolves elsewhere, or this is a "
            "plan for a different target.\n"
            "\n"
            "-> Re-run the dry run, read it, and apply with the token it prints."
        )
    body = {"apply": True, "ops": plan.ops}
    return post(target, ENDPOINT.format(space=plan.space), body, token=token)
