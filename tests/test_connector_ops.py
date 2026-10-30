"""Tests for ``fm connector reauth`` / ``fm connector sync``: per-connector
operator actions.

Deliberately a separate top-level command from ``fm connectors`` (plural):
that one is the admin view over the space-wide connector list (global,
platform-admin-gated), while these two hit ordinary project routes,
member-gated, never admin.

* ``fm connector reauth <cid>`` drives the same two-step device-code OAuth
  flow the product's own "Reconnect" banner uses (one call to initiate,
  then polling until the identity provider answers): it never talks to
  the provider directly, and never sees the refresh token, since the
  route never returns it.
* ``fm connector sync <cid>`` triggers a per-connector sync run through
  one endpoint that dispatches by the connection's own type server-side.
  ``--kind`` is accepted for the operator's own bookkeeping only and is
  not sent to the server, since there is nothing to send it to.

Both are writes with real external side effects (a live OAuth flow; a
real crawl against the connector's provider), so both are dry run by
default, requiring ``--apply``.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import connectors as connectors_mod
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CID = "44444444-4444-4444-4444-444444444444"


class RecordingApi:
    def __init__(self, *, reauth_response=None, poll_responses=None, sync_response=None):
        self.calls: list[tuple[str, str, object]] = []
        self._reauth_response = reauth_response
        self._poll_responses = list(poll_responses or [])
        self._sync_response = sync_response if sync_response is not None else {"changed": 0}

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path, body))
        if path.endswith("/reauth"):
            return self._reauth_response
        if path.endswith("/reauth/poll"):
            return self._poll_responses.pop(0)
        if path.endswith("/sync"):
            return self._sync_response
        raise AssertionError(f"unexpected POST {path}")

    @property
    def bodies(self):
        return [b for _m, _p, b in self.calls]


def _args(**overrides):
    base = {
        "env": "local", "space": SPACE, "subject": "reauth", "arg": CID,
        "provider": "outlook", "kind": None, "apply": False, "json": False,
        "poll_interval": 0, "poll_timeout": 60,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, *, sleep=None, **overrides):
        monkeypatch.setattr(connectors_mod, "api", api_stub)
        monkeypatch.setattr(
            connectors_mod.context, "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        if sleep is not None:
            monkeypatch.setattr(connectors_mod.time, "sleep", sleep)
        return connectors_mod.run_connector(_args(**overrides))

    return _drive


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------
def test_reauth_without_apply_makes_no_call(driven, capsys):
    stub = RecordingApi()
    rc = driven(stub, subject="reauth", arg=CID)
    assert rc == 0
    assert stub.calls == []
    assert "DRY RUN" in capsys.readouterr().err


def test_reauth_apply_initiates_and_prints_the_user_code(driven, capsys):
    stub = RecordingApi(
        reauth_response={
            "status": "reauth_pending", "connection_id": CID,
            "device_code_flow": {
                "verification_uri": "https://microsoft.com/devicelogin",
                "user_code": "ABCD1234", "expires_in": 900, "interval": 0,
            },
        },
        poll_responses=[{"status": "connected", "auth_status": "ok"}],
    )
    rc = driven(stub, subject="reauth", arg=CID, apply=True, sleep=lambda s: None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ABCD1234" in out
    assert "https://microsoft.com/devicelogin" in out
    assert stub.calls[0] == ("POST", f"/projects/{SPACE}/connections/{CID}/reauth", {})


def test_reauth_apply_polls_until_status_is_no_longer_pending(driven):
    stub = RecordingApi(
        reauth_response={
            "status": "reauth_pending", "connection_id": CID,
            "device_code_flow": {"verification_uri": "u", "user_code": "X", "expires_in": 900, "interval": 0},
        },
        poll_responses=[{"status": "pending"}, {"status": "pending"}, {"status": "connected", "auth_status": "ok"}],
    )
    sleeps = []
    rc = driven(stub, subject="reauth", arg=CID, apply=True, sleep=lambda s: sleeps.append(s))
    assert rc == 0
    polls = [c for c in stub.calls if c[1].endswith("/reauth/poll")]
    assert len(polls) == 3
    assert len(sleeps) == 2, "must sleep BETWEEN polls, not after the terminal one"


def test_reauth_apply_reports_a_failed_poll_without_raising(driven, capsys):
    stub = RecordingApi(
        reauth_response={
            "status": "reauth_pending", "connection_id": CID,
            "device_code_flow": {"verification_uri": "u", "user_code": "X", "expires_in": 900, "interval": 0},
        },
        poll_responses=[{"status": "expired", "error": "expired_token"}],
    )
    rc = driven(stub, subject="reauth", arg=CID, apply=True, sleep=lambda s: None)
    assert rc == 2
    assert "expired" in capsys.readouterr().out.lower()


def test_reauth_no_connection_id_is_a_loud_error(driven):
    with pytest.raises(connectors_mod.ConnectorsError, match="connection"):
        driven(RecordingApi(), subject="reauth", arg=None, apply=True)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def test_sync_without_apply_makes_no_call(driven, capsys):
    stub = RecordingApi()
    rc = driven(stub, subject="sync", arg=CID)
    assert rc == 0
    assert stub.calls == []
    assert "DRY RUN" in capsys.readouterr().err


def test_sync_apply_posts_to_the_connector_sync_route_with_no_body(driven):
    stub = RecordingApi(sync_response={"changed": 3, "deleted": 0})
    driven(stub, subject="sync", arg=CID, apply=True)
    assert stub.calls == [("POST", f"/projects/{SPACE}/connectors/{CID}/sync", {})]


def test_sync_kind_is_never_sent_to_the_server(driven):
    """`--kind` is operator bookkeeping only: one endpoint dispatches by the
    connector's OWN type, so there is nothing server-side to send it to."""
    stub = RecordingApi()
    driven(stub, subject="sync", arg=CID, kind="calendar", apply=True)
    assert stub.bodies == [{}]


def test_sync_apply_prints_the_result_counts(driven, capsys):
    stub = RecordingApi(sync_response={"changed": 3, "deleted": 1, "errors": 0})
    driven(stub, subject="sync", arg=CID, apply=True)
    out = capsys.readouterr().out
    assert "changed" in out and "3" in out


def test_sync_no_connection_id_is_a_loud_error(driven):
    with pytest.raises(connectors_mod.ConnectorsError, match="connection"):
        driven(RecordingApi(), subject="sync", arg=None, apply=True)


def test_unknown_subject_is_a_loud_error(driven):
    with pytest.raises(connectors_mod.ConnectorsError, match="unknown"):
        driven(RecordingApi(), subject="bogus", arg=CID)
