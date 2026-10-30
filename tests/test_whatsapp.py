"""``fm whatsapp`` — the CLI front end for ``/admin/whatsapp/devices*``.

WHAT THIS FILE GUARDS. This module (``formist_cli/whatsapp.py``) had ZERO test
coverage before this file — it is new, written against the contract fixed by
``scratchpad/0916-whatsapp-neonize/REPORT.md`` §3.1 from a worktree that does not
contain the routes themselves (the parent lane lands those separately). Four
shapes of defect this guards against, each named on the test that catches it:

  1. Every subject's argparse wiring actually round-trips (a typo'd ``dest=`` or
     a missing positional would parse silently wrong, not loudly).
  2. ``pair``'s poll loop: it must call ``POST .../pair`` exactly once, then
     ``GET .../pairing`` on a fixed cadence until ``status == "connected"``, and
     it must give up loudly on ``--timeout`` rather than spin forever.
  3. ``delete``'s typed-confirmation gate: a mismatched or unset typed value
     must never reach ``admin_delete``, and ``--yes`` must skip the prompt
     without ever calling ``input()``.

``attest-pin`` (and the secret-NAME-vs-VALUE guard it used) is GONE — owner
D600, 2026-09-23: 「there is no PIN any more」.

Run: uv run pytest tests/test_formist_cli_whatsapp.py -q
"""

from __future__ import annotations

import argparse
import types

import pytest

from formist_cli import whatsapp
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

AE = resolve_target("evane-ae")
DEVICE_ID = "22222222-2222-2222-2222-222222222222"
PHONE = "+971501234567"


# ===========================================================================
# canonical_subject
# ===========================================================================
def test_default_subject_is_devices():
    """Would fail if DEFAULT_SUBJECT drifted from `devices` without `fm whatsapp`
    (no subject) being updated to match."""
    assert whatsapp.canonical_subject(None) == "devices"


def test_an_unknown_subject_names_every_valid_one():
    """Would fail if the error message stopped listing a real subject — an
    operator typo would then get an unhelpful refusal."""
    with pytest.raises(whatsapp.WhatsAppError) as exc:
        whatsapp.canonical_subject("frobnicate")
    msg = str(exc.value)
    for subject in whatsapp.SUBJECTS:
        assert subject in msg


def test_subject_is_case_and_whitespace_insensitive():
    """Would fail if canonicalisation regressed to a bare equality check."""
    assert whatsapp.canonical_subject("  Pair  ") == "pair"


# ===========================================================================
# parser round trips — every subject parses the shape the DO list promises
# ===========================================================================
@pytest.mark.parametrize(
    "argv, subject, record",
    [
        (["devices", "--host", "evane-ae"], "devices", None),
        (["add", PHONE, "--name", "GFSS bot", "--host", "evane-ae"], "add", PHONE),
        (["show", DEVICE_ID, "--host", "evane-ae"], "show", DEVICE_ID),
        (["pair", DEVICE_ID, "--host", "evane-ae"], "pair", DEVICE_ID),
        (["pair", DEVICE_ID, "--code", "--timeout", "60", "--host", "evane-ae"], "pair", DEVICE_ID),
        (["start", DEVICE_ID, "--host", "evane-ae"], "start", DEVICE_ID),
        (["stop", DEVICE_ID, "--host", "evane-ae"], "stop", DEVICE_ID),
        (["logout", DEVICE_ID, "--host", "evane-ae"], "logout", DEVICE_ID),
        (["mark-account-deleted", DEVICE_ID, "--host", "evane-ae"], "mark-account-deleted", DEVICE_ID),
        (["delete", DEVICE_ID, "--yes", "--host", "evane-ae"], "delete", DEVICE_ID),
        (["groups", DEVICE_ID, "--host", "evane-ae"], "groups", DEVICE_ID),
        (["runner", "start", "--host", "evane-ae"], "runner", "start"),
        (["runner", "stop", "--host", "evane-ae"], "runner", "stop"),
        (["runner", "status", "--host", "evane-ae"], "runner", "status"),
        (["runner", "--host", "evane-ae"], "runner", None),
    ],
)
def test_parser_round_trips_every_subject(argv, subject, record):
    """Would fail if a subject's positional/flag wiring silently changed shape —
    e.g. `record` renamed without `run()`'s `getattr(args, "record", ...)`
    updated to match, which would parse fine and then KeyError/None at dispatch."""
    args = whatsapp.build_standalone_parser().parse_args(argv)
    assert args.subject == subject
    assert args.record == record


def test_pair_dash_dash_code_sets_the_flag():
    """Would fail if --code stopped being wired to args.code, silently pairing
    by QR when the operator asked for a phone code."""
    args = whatsapp.build_standalone_parser().parse_args(["pair", DEVICE_ID, "--code"])
    assert args.code is True


def test_json_flag_parses():
    args = whatsapp.build_standalone_parser().parse_args(["devices", "--json"])
    assert args.json is True


# ===========================================================================
# add — E.164 validation
# ===========================================================================
def test_add_refuses_a_number_with_no_plus():
    with pytest.raises(whatsapp.WhatsAppError, match="E.164"):
        whatsapp._validate_phone("971501234567")


def test_add_accepts_a_well_formed_e164_number():
    assert whatsapp._validate_phone(PHONE) == PHONE


# ===========================================================================
# run — pre-network guards (a bad call must never reach the API)
# ===========================================================================
def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": None, "record": None, "host": "evane-ae", "display_name": None,
        "code": False, "timeout": None, "yes": False, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


class _RefusingApi:
    """Every method raises — proves a refusal happened before any network call,
    not merely that the test never bothered to check."""

    def _boom(self, *a, **k):
        raise AssertionError("the API must not be called when the guard should refuse first")

    admin_get = admin_post = admin_patch = admin_delete = _boom


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(whatsapp, "api", _RefusingApi())
    monkeypatch.setattr(
        whatsapp.context, "resolve",
        lambda args, **kw: Context(target=AE, space=None, token="t", space_source="flag"),
    )
    return whatsapp.run


def test_show_without_a_device_id_refuses_before_the_api(guarded):
    with pytest.raises(whatsapp.WhatsAppError, match="device id"):
        guarded(_args(subject="show"))


def test_add_without_a_phone_refuses_before_the_api(guarded):
    with pytest.raises(whatsapp.WhatsAppError, match="phone number"):
        guarded(_args(subject="add"))


def test_add_with_a_malformed_phone_refuses_before_the_api(guarded):
    with pytest.raises(whatsapp.WhatsAppError, match="E.164"):
        guarded(_args(subject="add", record="not-a-phone"))


# ===========================================================================
# run — a well-formed read/write reaches exactly the endpoint it should
# ===========================================================================
@pytest.fixture
def driven(monkeypatch):
    def _drive(payload, **overrides):
        calls: list[tuple] = []

        def _record(target, path, *rest, token, **kw):
            calls.append((path, rest[0] if rest else kw.get("params")))
            return payload

        stub = types.SimpleNamespace(
            admin_get=_record, admin_post=_record, admin_patch=_record, admin_delete=_record,
        )
        monkeypatch.setattr(whatsapp, "api", stub)
        monkeypatch.setattr(
            whatsapp.context, "resolve",
            lambda args, **kw: Context(target=AE, space=None, token="t", space_source="flag"),
        )
        rc = whatsapp.run(_args(**overrides))
        return rc, calls

    return _drive


def test_devices_hits_the_plain_devices_endpoint(driven):
    rc, calls = driven({"items": []}, subject="devices")
    assert rc == 0
    assert calls == [(whatsapp.DEVICES_PATH, None)]


def test_add_sends_exactly_phone_and_name(driven):
    rc, calls = driven(
        {"id": DEVICE_ID, "phone_e164": PHONE}, subject="add", record=PHONE, display_name="GFSS bot",
    )
    assert rc == 0
    assert calls[0] == (whatsapp.DEVICES_PATH, {"phone_e164": PHONE, "display_name": "GFSS bot"})


def test_add_omits_display_name_when_not_given(driven):
    """Would fail if a None --name were sent as a literal null instead of being
    omitted from the body."""
    rc, calls = driven({"id": DEVICE_ID, "phone_e164": PHONE}, subject="add", record=PHONE)
    assert calls[0] == (whatsapp.DEVICES_PATH, {"phone_e164": PHONE})


def test_show_reaches_the_one_device_endpoint(driven):
    rc, calls = driven({"id": DEVICE_ID}, subject="show", record=DEVICE_ID)
    assert calls == [(f"{whatsapp.DEVICES_PATH}/{DEVICE_ID}", None)]


@pytest.mark.parametrize("subject", ["start", "stop", "logout", "mark-account-deleted"])
def test_state_transition_subjects_post_to_their_own_endpoint_with_no_body(driven, subject):
    rc, calls = driven({"id": DEVICE_ID, "status": "x"}, subject=subject, record=DEVICE_ID)
    assert rc == 0
    assert calls == [(f"{whatsapp.DEVICES_PATH}/{DEVICE_ID}/{subject}", {})]


def test_groups_reaches_the_groups_endpoint(driven):
    rc, calls = driven(
        {"items": [], "cached_at": None}, subject="groups", record=DEVICE_ID,
    )
    assert calls == [(f"{whatsapp.DEVICES_PATH}/{DEVICE_ID}/groups", None)]


def test_a_non_dict_payload_is_a_refusal(driven):
    """Shape: a server contract change (list/None instead of an object) must
    raise, not render an empty table as if nothing existed — cli_support.as_dict's
    contract, exercised through this module's own error type."""
    with pytest.raises(whatsapp.WhatsAppError):
        driven(["not", "a", "dict"], subject="devices")


# ===========================================================================
# runner — the ARMED switch (services/whatsapp/runner.py module docstring)
# ===========================================================================
def test_runner_with_no_action_defaults_to_status_and_hits_the_get_endpoint(driven):
    rc, calls = driven({"armed": True, "autostart": True, "sessions": []}, subject="runner", record=None)
    assert rc == 0
    assert calls == [(whatsapp.RUNNER_PATH, None)]


def test_runner_status_hits_the_get_endpoint(driven):
    rc, calls = driven({"armed": False, "autostart": True, "sessions": []}, subject="runner", record="status")
    assert rc == 0
    assert calls == [(whatsapp.RUNNER_PATH, None)]


def test_runner_start_posts_with_no_body(driven):
    rc, calls = driven({"armed": True, "started": 2}, subject="runner", record="start")
    assert rc == 0
    assert calls == [(f"{whatsapp.RUNNER_PATH}/start", {})]


def test_runner_stop_posts_with_no_body(driven):
    rc, calls = driven({"armed": False, "autostart": True, "sessions": []}, subject="runner", record="stop")
    assert rc == 0
    assert calls == [(f"{whatsapp.RUNNER_PATH}/stop", {})]


def test_runner_action_is_case_and_whitespace_insensitive(driven):
    rc, calls = driven({"armed": True, "started": 0}, subject="runner", record="  START  ")
    assert rc == 0
    assert calls == [(f"{whatsapp.RUNNER_PATH}/start", {})]


def test_runner_with_an_unknown_action_refuses_before_the_api(guarded):
    with pytest.raises(whatsapp.WhatsAppError, match="one of"):
        guarded(_args(subject="runner", record="frobnicate"))


# ===========================================================================
# pair — POST once, then GET .../pairing on a fixed cadence until connected
# ===========================================================================
def test_pair_polls_and_stops_the_moment_status_is_connected(monkeypatch, capsys):
    """The real defect this catches: a loop that keeps polling after `connected`
    (never terminates) or that never polls at all (declares success off the
    initial POST alone, before the device has actually paired)."""
    calls = {"post": 0, "get": 0}

    def _post(target, path, body, *, token):
        calls["post"] += 1
        assert path == f"{whatsapp.DEVICES_PATH}/{DEVICE_ID}/pair"
        assert body == {"method": "qr"}
        return {"status": "pairing", "qr": "QR-1", "pair_code": None,
                "issued_at": "t0", "expires_at": "t1"}

    responses = iter([
        {"status": "pairing", "qr": "QR-1", "pair_code": None},
        {"status": "connected", "qr": None, "pair_code": None},
    ])

    def _get(target, path, *, token):
        calls["get"] += 1
        assert path == f"{whatsapp.DEVICES_PATH}/{DEVICE_ID}/pairing"
        return next(responses)

    monkeypatch.setattr(whatsapp, "api", types.SimpleNamespace(admin_post=_post, admin_get=_get))
    monkeypatch.setattr(
        whatsapp.context, "resolve",
        lambda args, **kw: Context(target=AE, space=None, token="t", space_source="flag"),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(whatsapp.time, "sleep", lambda s: sleeps.append(s))

    rc = whatsapp.run(_args(subject="pair", record=DEVICE_ID))

    assert rc == 0
    assert calls == {"post": 1, "get": 2}
    assert sleeps == [whatsapp.PAIR_POLL_INTERVAL_SECONDS] * 2
    out = capsys.readouterr().out
    assert "connected" in out


def test_pair_gives_up_loudly_once_the_timeout_elapses(monkeypatch):
    """Would fail if a device that never connects made `fm whatsapp pair` hang
    forever instead of raising after --timeout."""

    def _post(target, path, body, *, token):
        return {"status": "pairing", "qr": None, "pair_code": "12345678"}

    def _get(target, path, *, token):  # pragma: no cover - must never be reached
        raise AssertionError("must time out before polling again")

    monkeypatch.setattr(whatsapp, "api", types.SimpleNamespace(admin_post=_post, admin_get=_get))
    monkeypatch.setattr(
        whatsapp.context, "resolve",
        lambda args, **kw: Context(target=AE, space=None, token="t", space_source="flag"),
    )
    # First call sizes the deadline (now=0, timeout=5 -> deadline=5); the loop's
    # very next clock read (100) is already past it, so the refusal fires before
    # any sleep/poll — deterministic, no real waiting.
    ticks = iter([0, 100])
    monkeypatch.setattr(whatsapp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(whatsapp.time, "sleep", lambda s: (_ for _ in ()).throw(
        AssertionError("must not sleep once the deadline has already passed")
    ))

    with pytest.raises(whatsapp.WhatsAppError, match="timed out"):
        whatsapp.run(_args(subject="pair", record=DEVICE_ID, timeout=5))


# ===========================================================================
# delete — typed phone-number confirmation
# ===========================================================================
def _delete_stub(monkeypatch, *, device_payload, delete_payload):
    calls = {"get": 0, "delete": 0}

    def _get(target, path, *, token):
        calls["get"] += 1
        return device_payload

    def _delete(target, path, *, token):
        calls["delete"] += 1
        return delete_payload

    monkeypatch.setattr(whatsapp, "api", types.SimpleNamespace(admin_get=_get, admin_delete=_delete))
    monkeypatch.setattr(
        whatsapp.context, "resolve",
        lambda args, **kw: Context(target=AE, space=None, token="t", space_source="flag"),
    )
    return calls


def test_delete_with_a_mismatched_typed_value_never_calls_delete(monkeypatch):
    """Owner rule (task DO #2): delete asks for a typed confirmation of the
    phone number. A defect here (comparing the wrong field, or not comparing at
    all) would let a mistyped/blank confirmation through to a PERMANENT delete."""
    calls = _delete_stub(
        monkeypatch, device_payload={"id": DEVICE_ID, "phone_e164": PHONE}, delete_payload={"ok": True},
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "+15550000000")

    with pytest.raises(whatsapp.WhatsAppError, match="does not match"):
        whatsapp.run(_args(subject="delete", record=DEVICE_ID))

    assert calls == {"get": 1, "delete": 0}


def test_delete_with_the_correct_typed_number_proceeds(monkeypatch):
    calls = _delete_stub(
        monkeypatch, device_payload={"id": DEVICE_ID, "phone_e164": PHONE}, delete_payload={"ok": True},
    )
    monkeypatch.setattr("builtins.input", lambda prompt: PHONE)

    rc = whatsapp.run(_args(subject="delete", record=DEVICE_ID))

    assert rc == 0
    assert calls == {"get": 1, "delete": 1}


def test_delete_with_yes_never_calls_input(monkeypatch):
    """Would fail if --yes still prompted — a scripted/automated delete would
    hang waiting on stdin instead of skipping the gate as documented."""
    calls = _delete_stub(
        monkeypatch, device_payload={"id": DEVICE_ID, "phone_e164": PHONE}, delete_payload={"ok": True},
    )

    def _boom(prompt):  # pragma: no cover - must never be reached
        raise AssertionError("--yes must skip the prompt entirely")

    monkeypatch.setattr("builtins.input", _boom)

    rc = whatsapp.run(_args(subject="delete", record=DEVICE_ID, yes=True))

    assert rc == 0
    assert calls == {"get": 1, "delete": 1}


# ===========================================================================
# --json output
# ===========================================================================
def test_json_flag_prints_raw_json_not_the_rendered_table(driven, capsys):
    rc, _ = driven({"id": DEVICE_ID, "phone_e164": PHONE}, subject="show", record=DEVICE_ID, json=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert '"phone_e164"' in out
    assert PHONE in out


def test_without_json_flag_prints_the_rendered_report_not_raw_json(driven, capsys):
    rc, _ = driven({"id": DEVICE_ID, "phone_e164": PHONE}, subject="show", record=DEVICE_ID, json=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert '"phone_e164"' not in out
    assert PHONE in out


# ===========================================================================
# render_* — the "never print nothing when the answer is empty" contract
# ===========================================================================
def test_render_devices_empty_says_so_rather_than_printing_nothing():
    assert "no WhatsApp devices" in whatsapp.render_devices([])


def test_render_devices_shows_phone_status_and_id():
    out = whatsapp.render_devices(
        [{"phone_e164": PHONE, "status": "connected", "enabled": True,
          "region": "ae", "display_name": "GFSS bot", "id": DEVICE_ID}]
    )
    assert PHONE in out and "connected" in out and DEVICE_ID in out


def test_render_device_counts_registration_recordings_rather_than_dumping_them():
    """Would fail if the raw list (with blob keys/urls) were printed inline
    instead of a count — this is a HUMAN report, not the JSON payload."""
    out = whatsapp.render_device(
        {"id": DEVICE_ID, "registration_recordings": [{"recorded_at": "t", "blob_key": "k"}]}
    )
    assert "1 recording(s)" in out
    assert "blob_key" not in out


def test_render_device_shows_none_for_a_missing_field_rather_than_a_keyerror():
    out = whatsapp.render_device({"id": DEVICE_ID})
    assert "phone_e164" in out


def test_render_groups_empty_says_so():
    assert "no groups cached" in whatsapp.render_groups({"items": [], "cached_at": None})


def test_render_groups_shows_participant_count_and_jid():
    out = whatsapp.render_groups(
        {"items": [{"jid": "1203@g.us", "group_name": "Ops", "participants": 42,
                    "ephemeral_seconds": 0, "announce_only": False}], "cached_at": "t0"}
    )
    assert "1203@g.us" in out and "42" in out and "Ops" in out


def test_render_runner_shows_armed_autostart_and_no_sessions():
    out = whatsapp.render_runner({"armed": False, "autostart": True, "sessions": []})
    assert "armed=False" in out and "autostart=True" in out and "no live sessions" in out


def test_render_runner_shows_each_live_session():
    out = whatsapp.render_runner(
        {"armed": True, "autostart": True, "sessions": [{"device_id": DEVICE_ID, "status": "connected", "connected": True}]}
    )
    assert DEVICE_ID in out and "connected" in out


def test_render_runner_start_result_shows_the_started_count_with_no_sessions_key():
    """`POST .../runner/start` returns `{armed, started}` only — no `sessions`
    key — so the renderer must not KeyError on it."""
    out = whatsapp.render_runner({"armed": True, "started": 3})
    assert "started=3" in out
