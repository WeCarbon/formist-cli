"""Several `fm` processes sharing one stored credential.

The server rotates on refresh: trading a refresh token in revokes it AND the access
token issued with it. So when two processes on one machine use the same token file:

  * two that refresh at the same moment both trade the same refresh token; the
    second exchange is refused and that process is left holding a dead token;
  * a long-running process (an upload of hundreds of files) keeps the access token
    it started with; the moment any other `fm` refreshes, every request it sends
    from then on is refused.

These tests pin the fix: refreshing happens under a file lock and re-reads the file
inside it; a 401 is answered by re-reading the file (or refreshing) and retrying that
one request exactly once; a 401 that survives the retry stops a directory upload
instead of being repeated for every remaining file.

The concurrent refreshers are threads, each opening the lock file on its own file
descriptor: `flock` excludes separate descriptors whether they belong to one process
or two, so this is the same contention two processes produce.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from formist_cli import api, context, login, tokens, uploads
from formist_cli.targets import resolve as resolve_target

ENV = "formist"
TARGET = resolve_target("127.0.0.1:8160")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(tokens.CONFIG_DIR_VAR, str(tmp_path / "cfg"))
    monkeypatch.delenv(tokens.ENV_TOKEN_VAR, raising=False)
    api.set_reauthenticator(None)
    yield
    api.set_reauthenticator(None)


class RotatingServer:
    """A token endpoint that rotates: each refresh token works exactly once."""

    def __init__(self, first_refresh: str = "r0"):
        self.valid_refresh = first_refresh
        self.valid_access: set[str] = {"a0"}
        self.exchanges = 0
        self.refused = 0
        self._lock = threading.Lock()

    def refresh(self, _auth, _meta, *, client_id, refresh_token):
        with self._lock:
            if refresh_token != self.valid_refresh:
                self.refused += 1
                raise login.LoginError("invalid_grant")
            self.exchanges += 1
            n = self.exchanges
            self.valid_refresh = f"r{n}"
            self.valid_access = {f"a{n}"}  # the old access token is revoked with it
        time.sleep(0.2)  # widen the window two refreshers can collide in
        return {"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 3600}


def _store(access: str, refresh: str, *, expired: bool) -> None:
    when = datetime.now(timezone.utc) + (timedelta(minutes=-5) if expired else timedelta(hours=1))
    tokens.store(
        ENV, access,
        {"client_id": "cli", "refresh_token": refresh, "expires_at": when.isoformat()},
    )


def _install(monkeypatch, server: RotatingServer) -> None:
    monkeypatch.setattr(login, "refresh", server.refresh)
    monkeypatch.setattr(login, "discover", lambda target: {"token_endpoint": "/token"})
    monkeypatch.setattr(login, "auth_target", lambda target: target)


def test_two_concurrent_refreshers_trade_the_refresh_token_once_and_agree(monkeypatch):
    server = RotatingServer()
    _install(monkeypatch, server)
    _store("a0", "r0", expired=True)

    results: list[str | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(context._refresh_if_stale(ENV, "a0")))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert server.refused == 0, "a second process traded a refresh token that was already used"
    assert server.exchanges == 1
    assert results == ["a1", "a1", "a1"], results
    assert tokens.load(ENV) == "a1" and tokens.load_refresh(ENV) == "r1"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_urlopen(server: RotatingServer, seen: list[str]):
    def urlopen(request, timeout=None):
        auth = request.get_header("Authorization") or ""
        token = auth.removeprefix("Bearer ")
        seen.append(token)
        if token not in server.valid_access:
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"error": "not-authenticated"}'),  # type: ignore[arg-type]
            )
        return _Resp(json.dumps({"ok": True}).encode())

    return urlopen


def _register_like_context() -> None:
    api.set_reauthenticator(lambda _t, stale: context.reauthenticate(ENV, stale))


def test_a_token_revoked_by_another_processs_refresh_is_replaced_from_the_file(monkeypatch):
    server = RotatingServer()
    _install(monkeypatch, server)
    _store("a0", "r0", expired=False)
    _register_like_context()
    seen: list[str] = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(server, seen))

    # Another process refreshes: the file now holds a1, and a0 is revoked.
    context._refresh_locked(ENV, "a0")
    assert tokens.load(ENV) == "a1"

    assert api.get(TARGET, "/projects", token="a0") == {"ok": True}
    assert seen == ["a0", "a1"]
    assert server.exchanges == 1, "the replacement was in the file; no second refresh"

    # A caller still passing the old string goes straight to the new token.
    seen.clear()
    api.put_bytes(TARGET, "/p", b"x", token="a0")
    assert seen == ["a1"]


def test_an_expired_token_nobody_refreshed_is_refreshed_once_then_retried(monkeypatch):
    server = RotatingServer()
    _install(monkeypatch, server)
    _store("a0", "r0", expired=False)
    _register_like_context()
    server.valid_access = set()  # a0 expired server-side
    seen: list[str] = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(server, seen))

    assert api.get(TARGET, "/projects", token="a0") == {"ok": True}
    assert seen == ["a0", "a1"] and server.exchanges == 1


def test_a_401_the_new_token_does_not_fix_is_retried_exactly_once(monkeypatch):
    server = RotatingServer()
    _install(monkeypatch, server)
    _store("a0", "r0", expired=False)
    _register_like_context()
    seen: list[str] = []

    def always_401(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", always_401)
    with pytest.raises(api.NotAuthenticated):
        api.get(TARGET, "/projects", token="a0")
    assert len(seen) == 2, seen


def test_a_401_with_nothing_to_refresh_is_not_retried(monkeypatch):
    server = RotatingServer()
    _install(monkeypatch, server)
    _store("a0", "r-revoked", expired=False)  # refresh will be refused
    _register_like_context()
    server.valid_access = set()
    seen: list[str] = []
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(server, seen))
    with pytest.raises(api.NotAuthenticated):
        api.get(TARGET, "/projects", token="a0")
    assert seen == ["a0"]


def test_a_chunk_put_is_not_retried_on_a_401():
    calls = {"n": 0}

    def put(target, path, data, **kw):
        calls["n"] += 1
        raise api.NotAuthenticated("401 from test: no valid session.")

    with pytest.raises(api.NotAuthenticated):
        uploads._put_chunk_with_retry(TARGET, "sp", "u", 0, b"x", token="t", put=put, retries=3)
    assert calls["n"] == 1


def test_a_directory_upload_stops_at_the_first_unrecoverable_401(tmp_path, monkeypatch, capsys):
    from test_upload_dir import FakeDirServer, _dir_args, _patch_effective

    root = tmp_path / "drop"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (root / name).write_text(name)

    class ExpiringServer(FakeDirServer):
        def post(self, target, path, body, *, token=None, **kw):
            if path.endswith("/sessions") and len(self.open_bodies) >= 1:
                raise api.NotAuthenticated("401 from test: no valid session.")
            return super().post(target, path, body, token=token, **kw)

    server = ExpiringServer()
    _patch_effective(monkeypatch, server)
    rc = uploads.cmd_upload(_dir_args(file=str(root), apply=True))
    err = capsys.readouterr().err
    assert rc == 1
    assert [b["filename"] for b in server.open_bodies] == ["a.txt"]
    assert "2 file(s) not attempted" in err, err
    assert "1 uploaded, 3 failed, 4 total" in err, err
