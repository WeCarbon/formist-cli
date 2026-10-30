"""Tests for ``fm upload DIR``: uploading every file under a directory,
recursively.

Reuses `formist_cli.uploads.upload_file` per file, the same resumable
path already proven to send exact bytes elsewhere. What is new here, and
what these tests are built around, is the directory layer on top of it:
which files get walked, how a subfolder's path reaches the server
(`relative_path`, not a client-side re-derivation of `--folder-path`),
and that one file failing does not stop the rest.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from formist_cli import uploads
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


# ===========================================================================
# walk_upload_dir / relative_upload_path: pure, no HTTP at all
# ===========================================================================
class TestWalkUploadDir:
    def test_skips_ds_store_thumbs_and_pycache_recursively(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / ".DS_Store").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b")
        (tmp_path / "sub" / "Thumbs.db").write_text("x")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "c.pyc").write_text("x")

        files = uploads.walk_upload_dir(tmp_path)
        rels = sorted(uploads.relative_upload_path(tmp_path, f) for f in files)
        assert rels == ["a.txt", "sub/b.txt"]

    def test_a_nonexistent_directory_refuses(self, tmp_path):
        with pytest.raises(uploads.UploadError):
            uploads.walk_upload_dir(tmp_path / "nope")

    def test_relative_upload_path_is_forward_slash(self, tmp_path):
        (tmp_path / "sub").mkdir()
        f = tmp_path / "sub" / "b.txt"
        f.write_text("b")
        assert uploads.relative_upload_path(tmp_path, f) == "sub/b.txt"


# ===========================================================================
# The fake transport: same shape as the plain-upload FakeServer, kept
# minimal here: a directory upload's own logic (walking, per-file continue,
# the empty-file label) is what these tests are for, not the chunk math
# (already proven elsewhere).
# ===========================================================================
class FakeDirServer:
    def __init__(self, *, fail_names: frozenset[str] = frozenset()):
        from formist_cli import api as _api_mod

        self.sessions: dict[str, dict] = {}
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.open_bodies: list[dict] = []
        self.fail_names = fail_names
        self.calls: list[tuple[str, str]] = []
        # `_put_chunk_with_retry` resolves `_api()` internally to reach
        # `api.ApiError` for its except clause even when `put=` is not given
        # explicitly (see formist_cli/uploads.py): so a fake standing in for `_api()`
        # needs the REAL exception class, not a re-implementation of it.
        self.ApiError = _api_mod.ApiError

    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path))
        if path.endswith("/sessions"):
            self.open_bodies.append(dict(body))
            total = body["total_size"]
            upload_id = str(uuid4())
            self.sessions[upload_id] = {
                "upload_id": upload_id,
                "chunk_size": max(total, 1),
                "total_size": total,
                "chunk_count": 1,
                "received_chunks": [],
                "filename": body["filename"],
                "content_type": body["content_type"],
            }
            self.chunks[upload_id] = {}
            return dict(self.sessions[upload_id])
        if "/complete" in path:
            upload_id = path.rsplit("/", 2)[1]
            session = self.sessions[upload_id]
            data = self.chunks[upload_id][0]
            file_id = str(uuid4())
            item = {"id": file_id, "name": session["filename"], "size": len(data)}
            return {"item": item, "audit_id": str(uuid4())}
        raise AssertionError(f"unexpected POST {path}")

    def put_bytes(self, target, path, data, *, token=None, **kw):
        self.calls.append(("PUT", path))
        parts = path.rstrip("/").split("/")
        upload_id, index = parts[-3], int(parts[-1])
        session = self.sessions[upload_id]
        if session["filename"] in self.fail_names:
            from formist_cli import api

            err = api.ApiError("simulated refusal (fake)")
            err.status = 400
            raise err
        self.chunks[upload_id][int(index)] = data
        return dict(session)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _dir_args(**overrides):
    defaults = dict(
        env="local", space=SPACE, file=None, folder_path="/",
        owner=None, task_id=None, content_type=None, apply=False, json=False,
    )
    defaults.update(overrides)
    return _Args(**defaults)


def _patch_effective(monkeypatch, server):
    from formist_cli import cli

    class _Eff:
        target = LOCAL
        space = SPACE

    monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
    monkeypatch.setattr(uploads, "_api", lambda: server)


# ===========================================================================
# fm upload DIR: dry run
# ===========================================================================
class TestDirDryRun:
    def test_lists_count_and_total_bytes_and_touches_nothing(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("bb")  # 2 bytes
        server = FakeDirServer()
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_upload(_dir_args(file=str(tmp_path)))

        assert rc == 0
        assert server.calls == [], "a dry run must not touch the network at all"
        err = capsys.readouterr().err
        assert "DRY RUN" in err
        assert "2 file(s)" in err
        assert "5 bytes" in err


# ===========================================================================
# fm upload DIR --apply: the resumable path is reused per file
# ===========================================================================
class TestDirApply:
    def test_every_file_uploads_with_its_relative_path(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("bb")
        server = FakeDirServer()
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True))

        assert rc == 0
        out = capsys.readouterr().out
        assert "uploaded  a.txt" in out
        assert "uploaded  sub/b.txt" in out
        rels = {b["relative_path"] for b in server.open_bodies}
        assert rels == {"a.txt", "sub/b.txt"}
        # folder_path itself stays the flag's value on every call: the SERVER
        # (not this client) derives the destination folder from relative_path.
        assert {b["folder_path"] for b in server.open_bodies} == {"/"}

    def test_more_than_one_file_actually_reaches_the_network_the_control(self, tmp_path, monkeypatch):
        """A control for the assertion above: with two files, fewer than two
        POSTs to /sessions would mean the walk never really iterated."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")
        server = FakeDirServer()
        _patch_effective(monkeypatch, server)
        uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True))
        opens = [c for c in server.calls if c[0] == "POST" and c[1].endswith("/sessions")]
        assert len(opens) == 2


# ===========================================================================
# fm upload DIR --apply: failures are per-file, never abort the walk
# ===========================================================================
class TestDirApplyFailures:
    def test_an_empty_file_is_labelled_and_failed_without_opening_a_session(
        self, tmp_path, monkeypatch, capsys
    ):
        (tmp_path / "good.txt").write_text("hello")
        (tmp_path / "empty.txt").write_bytes(b"")
        server = FakeDirServer()
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True))

        assert rc == 1
        combined = "".join(capsys.readouterr())
        assert "empty file (server refuses 0-byte uploads)" in combined
        assert "empty.txt" in combined
        # the client-side 0-byte refusal never opens a network connection for
        # THAT file: good.txt's session is the only one opened.
        assert {b["filename"] for b in server.open_bodies} == {"good.txt"}

    def test_a_server_refusal_on_one_file_does_not_stop_the_rest(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "good.txt").write_text("hello")
        (tmp_path / "bad.txt").write_text("will be refused")
        server = FakeDirServer(fail_names=frozenset({"bad.txt"}))
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True))

        assert rc == 1
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "uploaded  good.txt" in out.out, "good.txt must still succeed"
        assert "bad.txt" in combined
        assert "\n1 uploaded, 1 failed, 2 total" in combined

    def test_json_summary_lists_the_failure(self, tmp_path, monkeypatch, capsys):
        import json as json_mod

        (tmp_path / "empty.txt").write_bytes(b"")
        server = FakeDirServer()
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_upload(_dir_args(file=str(tmp_path), apply=True, json=True))

        assert rc == 1
        # Nothing succeeded, so stdout carries ONLY the JSON summary (per-file
        # success lines, which would otherwise precede it, never printed).
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["uploaded"] == 0
        assert payload["failed"][0]["path"] == "empty.txt"
        assert "empty file" in payload["failed"][0]["reason"]
