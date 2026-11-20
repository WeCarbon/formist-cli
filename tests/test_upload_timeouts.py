"""Upload transport failures (C1553 / C1557, Formist prod, 2026-11-20).

What happened: four lanes ran `fm upload DIR` into prod while the server was
stalled converting PDFs. A chunk PUT waited longer than its 30 s default
timeout; urllib raised a bare `TimeoutError` (never wrapped in `URLError`),
which `_send` did not catch, so it skipped the chunk retry AND the per-file
FAILED line and killed each whole batch. The lanes' wrapper scripts then
re-ran the files from scratch: a NEW upload session per retry, while the
server was still finishing the first one, so the same bytes were stored and
converted again.

Each test pins one piece of the fix and goes red without it:

  1. `_send` turns a read timeout / connection reset into a retryable
     `ApiError` (status None);
  2. a chunk PUT gets `CHUNK_TIMEOUT`, a completion `UPLOAD_TIMEOUT`;
  3. a completion whose answer was lost is re-asked ON THE SAME SESSION
     (one session, one File), a 409 "still assembling" is waited out, and a
     real refusal is not retried;
  4. `fm upload DIR` reports a timed-out file as FAILED and carries on.
"""

from __future__ import annotations

import urllib.request

import pytest
from test_uploads import SPACE, FakeServer

from formist_cli import api, uploads
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(uploads, "_COMPLETE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(uploads, "_RETRY_BACKOFF_SECONDS", 0)


@pytest.mark.parametrize("exc", [TimeoutError("The read operation timed out"), ConnectionResetError(54, "reset")])
def test_a_read_timeout_or_reset_becomes_a_retryable_api_error(monkeypatch, exc):
    def _urlopen(request, timeout=None):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    with pytest.raises(api.ApiError) as caught:
        api.put_bytes(LOCAL, "/projects/x/files/upload/sessions/u/chunks/0", b"abc")
    assert caught.value.status is None


def test_chunk_and_completion_timeouts_are_not_the_30s_default(monkeypatch, tmp_path):
    seen: dict[str, int] = {}

    def _urlopen(request, timeout=None):
        seen[request.get_method()] = timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    with pytest.raises(api.ApiError):
        api.put_bytes(LOCAL, "/p", b"abc")
    assert seen["PUT"] == api.CHUNK_TIMEOUT > api.DEFAULT_TIMEOUT

    server = FakeServer()
    timeouts: list[int | None] = []
    real_post = server.post

    def post(target, path, body, *, token=None, **kw):
        if "/complete" in path:
            timeouts.append(kw.get("timeout"))
        return real_post(target, path, body, token=token, **kw)

    src = tmp_path / "a.bin"
    src.write_bytes(b"abcdefgh")
    uploads.upload_file(LOCAL, SPACE, src, token="t", post=post, put=server.put_bytes)
    assert timeouts == [api.UPLOAD_TIMEOUT]


def _lost_answer_server():
    """A completion that SUCCEEDS on the server but whose answer never arrives,
    the first time; the real server's completion is idempotent, so is this."""
    server = FakeServer()
    real_post = server.post
    state = {"completes": 0}

    def post(target, path, body, *, token=None, **kw):
        if "/complete" in path:
            state["completes"] += 1
            upload_id = path.rsplit("/", 2)[1]
            existing = server.sessions[upload_id]["file_id"]
            if existing:
                return {"item": dict(server.files[existing]), "audit_id": None}
            result = real_post(target, path, body, token=token, **kw)
            if state["completes"] == 1:
                raise api.ApiError("no answer (TimeoutError)")
            return result
        return real_post(target, path, body, token=token, **kw)

    return server, post, state


def test_a_lost_completion_answer_is_re_asked_on_the_same_session(tmp_path):
    server, post, state = _lost_answer_server()
    src = tmp_path / "a.bin"
    src.write_bytes(b"abcdefgh")
    done = uploads.upload_file(LOCAL, SPACE, src, token="t", post=post, put=server.put_bytes)
    assert done["item"]["size"] == 8
    opened = [p for m, p in server.calls if m == "POST" and p.endswith("/sessions")]
    assert len(opened) == 1, "a retry must not open a second upload session"
    assert len(server.files) == 1 and state["completes"] == 2


def test_still_assembling_is_waited_out_and_a_refusal_is_not_retried(tmp_path):
    server = FakeServer()
    real_post = server.post
    answers = {"n": 0}

    def post(target, path, body, *, token=None, **kw):
        if "/complete" in path:
            answers["n"] += 1
            if answers["n"] == 1:
                raise api.ApiError("upload-assembling", status=409)
        return real_post(target, path, body, token=token, **kw)

    src = tmp_path / "a.bin"
    src.write_bytes(b"abcdefgh")
    assert uploads.upload_file(LOCAL, SPACE, src, token="t", post=post, put=server.put_bytes)["item"]["size"] == 8
    assert answers["n"] == 2

    refusals = {"n": 0}

    def refusing(target, path, body, *, token=None, **kw):
        if "/complete" in path:
            refusals["n"] += 1
            raise api.ApiError("upload-too-large", status=413)
        return real_post(target, path, body, token=token, **kw)

    with pytest.raises(api.ApiError):
        uploads.upload_file(LOCAL, SPACE, src, token="t", post=refusing, put=server.put_bytes)
    assert refusals["n"] == 1


def test_upload_dir_reports_a_timed_out_file_and_carries_on(monkeypatch, tmp_path, capsys):
    """Through the REAL `api.put_bytes` / `_send`: urllib raises a bare
    TimeoutError for one file's chunk. The batch must still upload the other
    file and count the timed-out one as failed, instead of dying."""
    from test_upload_dir import FakeDirServer, _dir_args, _patch_effective

    (tmp_path / "ok.txt").write_text("fine")
    (tmp_path / "slow.txt").write_text("stalls")

    class StallingServer(FakeDirServer):
        def put_bytes(self, target, path, data, *, token=None, **kw):
            upload_id = path.rstrip("/").split("/")[-3]
            if self.sessions[upload_id]["filename"] == "slow.txt":
                def _urlopen(request, timeout=None):
                    raise TimeoutError("The read operation timed out")

                monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
                return api.put_bytes(target, path, data, token=token)
            return super().put_bytes(target, path, data, token=token, **kw)

    server = StallingServer()
    _patch_effective(monkeypatch, server)
    rc = uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True))
    out = capsys.readouterr()
    combined = out.out + out.err
    assert rc == 1
    assert "uploaded  ok.txt" in out.out, combined
    assert "slow.txt" in combined and "\n1 uploaded, 1 failed, 2 total" in combined
