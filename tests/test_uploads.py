"""``fm upload`` / ``fm skill-attach`` — the client-side upload + attachment logic.

WHY THESE TESTS ARE SHAPED THIS WAY
------------------------------------
`formist_cli/uploads.py` never talks to a real server in this suite (the repo
convention for `ev` commands — see `test_formist_cli_update.py`, `test_formist_cli_diff.py`
— is an injected fake transport, not a live socket; the transport layer itself
is proven separately in `test_formist_cli_api_uploads_transport.py`). So the fakes
here stand in for `routes/upload_sessions.py` / `routes/files_rest.py` /
`routes/skills.py`, and the one property that makes that legitimate rather than
a mock agreeing with itself is: **the fake actually assembles the bytes it was
sent and the tests assert on the assembled result**, not merely "was called".

That is the direct answer to the failure this codebase has produced before: "a
test that uploads ZERO BYTES and passes" (the project rules). `TestChunkedUploadSendsExactBytes`
reconstructs the file from every PUT the client made and diffs it byte-for-byte
against the source — a client that silently sent the wrong range, dropped a
chunk, or sent zero bytes fails that assertion, not a "call count" proxy for it.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from formist_cli import uploads
from formist_cli.resolve import ResolutionError
from formist_cli.targets import resolve as resolve_target
from formist_cli.uploads import (
    UploadError,
    attach_file_to_skill,
    find_existing_attachment,
    resolve_skill,
    safe_filename,
    same_filename,
    upload_file,
)

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


class TestFilenameHelpers:
    def test_the_parity_test_itself_has_a_positive_control(self):
        """A suite that only ever asserts equality could pass with two functions
        that both always return the same constant. Prove the comparison can
        distinguish two different names, so the parity above is meaningful."""
        assert not same_filename("Other Template.pptx", "GFSS PMO Meeting Template.pptx")
        assert safe_filename("a.pptx") != safe_filename("b.pptx")


# ===========================================================================
# The fake transport. Faithful to the REAL response shapes
# (routes/upload_sessions.py::session_dict, routes/files_rest.py::file_dict,
# routes/skills.py::skill_dict) — not because the server is retested here, but
# because a fake that lies about the shape would validate nothing about how the
# client parses a real response.
# ===========================================================================


class FakeServer:
    """In-memory stand-in for the upload-session + skill-attach HTTP surface.

    Chunk math mirrors `services/upload_sessions.py::expected_chunk_size`
    deliberately (a fake that accepted any chunk length would never catch a
    client that sliced the file wrong) — but nothing here re-implements the
    CLIENT logic under test.
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.files: dict[str, dict] = {}
        self.skills: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []  # (method, path) audit trail

    # --- seeding ------------------------------------------------------------
    def add_file(self, file_id: str, *, name: str, size: int, project_id: str = SPACE):
        self.files[file_id] = {
            "id": file_id,
            "project_id": project_id,
            "name": name,
            "size": size,
            "mime_type": "application/octet-stream",
            "web_url": f"blob://uploads/{file_id}",
        }

    def add_skill(self, skill_id: str, *, name: str, files: list[str] | None = None):
        self.skills[skill_id] = {
            "id": skill_id,
            "project_id": SPACE,
            "name": name,
            "files": list(files or []),
        }

    # --- transport methods (bound as post/put/get/patch) --------------------
    def post(self, target, path, body, *, token=None, **kw):
        self.calls.append(("POST", path))
        if path.endswith("/sessions"):
            total = body["total_size"]
            chunk_size = 4  # small and deliberate: forces >=1 partial last chunk
            count = (total + chunk_size - 1) // chunk_size
            upload_id = str(uuid4())
            self.sessions[upload_id] = {
                "upload_id": upload_id,
                "chunk_size": chunk_size,
                "total_size": total,
                "chunk_count": count,
                "received_chunks": [],
                "received_bytes": 0,
                "filename": body["filename"],
                "content_type": body["content_type"],
                "expires_at": "2099-01-01T00:00:00Z",
                "file_id": None,
            }
            self.chunks[upload_id] = {}
            return dict(self.sessions[upload_id])
        if "/complete" in path:
            upload_id = path.rsplit("/", 2)[1]
            session = self.sessions[upload_id]
            count = session["chunk_count"]
            have = self.chunks[upload_id]
            if sorted(have) != list(range(count)):
                raise UploadError("upload-incomplete (fake)")
            assembled = b"".join(have[i] for i in range(count))
            if len(assembled) != session["total_size"]:
                raise UploadError("assembled size mismatch (fake)")
            file_id = str(uuid4())
            self.add_file(file_id, name=safe_filename(session["filename"]), size=len(assembled))
            self.files[file_id]["_bytes"] = assembled  # test-only, for byte assertions
            session["file_id"] = file_id
            return {"item": dict(self.files[file_id]), "audit_id": str(uuid4())}
        raise AssertionError(f"unexpected POST {path}")

    def put_bytes(self, target, path, data, *, token=None, **kw):
        self.calls.append(("PUT", path))
        # /projects/{space}/files/upload/sessions/{id}/chunks/{index}
        parts = path.rstrip("/").split("/")
        upload_id, index = parts[-3], int(parts[-1])
        session = self.sessions[upload_id]
        count = session["chunk_count"]
        chunk_size = session["chunk_size"]
        expected = chunk_size if index < count - 1 else session["total_size"] - chunk_size * (count - 1)
        if len(data) != expected:
            from formist_cli import api

            err = api.ApiError(f"invalid-chunk-size: got {len(data)} want {expected}")
            err.status = 400
            raise err
        self.chunks[upload_id][index] = data
        session["received_chunks"] = sorted(self.chunks[upload_id])
        session["received_bytes"] = sum(len(v) for v in self.chunks[upload_id].values())
        return dict(session)

    def get(self, target, path, *, token=None, params=None, **kw):
        self.calls.append(("GET", path))
        if "/files/" in path:
            file_id = path.rsplit("/", 1)[-1]
            file = self.files.get(file_id)
            if file is None:
                from formist_cli import api

                err = api.ApiError("not-found")
                err.status = 404
                raise err
            return {k: v for k, v in file.items() if not k.startswith("_")}
        if "/skills/" in path and not path.rstrip("/").endswith("/skills"):
            skill_id = path.rsplit("/", 1)[-1]
            return dict(self.skills[skill_id])
        if path.rstrip("/").endswith("/skills"):
            q = (params or {}).get("q")
            items = list(self.skills.values())
            if q:
                items = [s for s in items if q in (s.get("name") or "")]
            return {"items": items, "total": len(items), "limit": 200, "offset": 0}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, target, path, body, *, token=None, **kw):
        self.calls.append(("PATCH", path))
        skill_id = path.rsplit("/", 1)[-1]
        skill = self.skills[skill_id]
        skill["files"] = body["files"]
        return {"item": dict(skill), "audit_id": str(uuid4())}


def _write_file(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


# ===========================================================================
# upload_file — the client's byte-slicing and chunk orchestration
# ===========================================================================


class TestChunkedUploadSendsExactBytes:
    def test_the_reassembled_bytes_equal_the_source_file(self, tmp_path):
        content = bytes(range(256)) * 3 + b"\x99\x00\x01"  # not chunk-size aligned
        path = _write_file(tmp_path, "template.pptx", content)
        server = FakeServer()

        completed = upload_file(
            LOCAL,
            SPACE,
            path,
            token="t",
            post=server.post,
            put=server.put_bytes,
        )

        upload_id = next(iter(server.sessions))
        reassembled = b"".join(server.chunks[upload_id][i] for i in range(server.sessions[upload_id]["chunk_count"]))
        assert reassembled == content, "client sent the wrong bytes, wrong order, or dropped some"
        assert completed["item"]["size"] == len(content)

    def test_more_than_one_chunk_is_actually_sent(self, tmp_path):
        """A CONTROL for the test above: with a 4-byte chunk size and >4 bytes of
        content, fewer than 2 PUTs would mean the chunk loop never ran — proving
        the byte-equality assertion is not vacuously true of a single whole-file PUT."""
        path = _write_file(tmp_path, "x.bin", b"0123456789")
        server = FakeServer()
        upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=server.put_bytes)
        puts = [c for c in server.calls if c[0] == "PUT"]
        assert len(puts) >= 3, puts

    def test_a_short_last_chunk_is_sized_correctly(self, tmp_path):
        # 10 bytes / chunk_size=4 -> chunks of 4, 4, 2
        path = _write_file(tmp_path, "x.bin", bytes(range(10)))
        server = FakeServer()
        upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=server.put_bytes)
        upload_id = next(iter(server.sessions))
        sizes = [len(server.chunks[upload_id][i]) for i in sorted(server.chunks[upload_id])]
        assert sizes == [4, 4, 2]

    def test_the_file_dict_is_returned_verbatim_from_the_server(self, tmp_path):
        path = _write_file(tmp_path, "x.bin", b"hello world")
        server = FakeServer()
        completed = upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=server.put_bytes)
        assert completed["item"]["name"] == "x.bin"
        assert "audit_id" in completed


class TestZeroByteUploadIsRefusedBeforeTheNetwork:
    def test_an_empty_file_raises_and_makes_no_calls(self, tmp_path):
        path = _write_file(tmp_path, "empty.pptx", b"")
        server = FakeServer()
        with pytest.raises(UploadError, match="empty"):
            upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=server.put_bytes)
        assert server.calls == [], "a refused upload must not touch the network at all"

    def test_the_control_a_nonempty_file_DOES_make_calls(self, tmp_path):
        """Without this, an empty `server.calls == []` up above could just mean
        the fake was never wired up correctly, not that the refusal fired."""
        path = _write_file(tmp_path, "nonempty.pptx", b"x")
        server = FakeServer()
        upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=server.put_bytes)
        assert len(server.calls) > 0


class TestServerRecordedSizeIsVerifiedNotTrusted:
    def test_a_mismatched_size_in_the_completion_response_is_caught(self, tmp_path):
        path = _write_file(tmp_path, "x.bin", b"0123456789")
        server = FakeServer()

        real_post = server.post

        def lying_post(target, path_, body, **kw):
            result = real_post(target, path_, body, **kw)
            if "/complete" in path_:
                result["item"]["size"] = result["item"]["size"] - 1  # corrupt it
            return result

        with pytest.raises(UploadError, match="does not match"):
            upload_file(LOCAL, SPACE, path, token="t", post=lying_post, put=server.put_bytes)


class TestChunkRetry:
    def test_a_transient_failure_is_retried_and_then_succeeds(self, tmp_path):
        path = _write_file(tmp_path, "x.bin", b"abcdefgh")
        server = FakeServer()
        attempts = {"n": 0}
        real_put = server.put_bytes

        def flaky_put(target, path_, data, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                from formist_cli import api

                raise api.ApiError("connection reset")  # status=None -> transient
            return real_put(target, path_, data, **kw)

        completed = upload_file(LOCAL, SPACE, path, token="t", post=server.post, put=flaky_put, chunk_retries=3)
        assert completed["item"]["size"] == 8
        assert attempts["n"] >= 2

    def test_a_real_refusal_status_is_not_retried(self, tmp_path):
        path = _write_file(tmp_path, "x.bin", b"abcdefgh")
        server = FakeServer()
        attempts = {"n": 0}

        def refusing_put(target, path_, data, **kw):
            attempts["n"] += 1
            from formist_cli import api

            err = api.ApiError("invalid-chunk-size")
            err.status = 400
            raise err

        from formist_cli import api

        with pytest.raises(api.ApiError):
            upload_file(
                LOCAL,
                SPACE,
                path,
                token="t",
                post=server.post,
                put=refusing_put,
                chunk_retries=3,
            )
        assert attempts["n"] == 1, "a 4xx is not transient — retrying it wastes time and hides the real cause"


# ===========================================================================
# find_existing_attachment — mirrors services/skill_attachments.py::_same_file
# ===========================================================================


class TestFindExistingAttachment:
    def test_a_matching_registered_file_is_found(self):
        server = FakeServer()
        fid = str(uuid4())
        server.add_file(fid, name="GFSS_PMO_Meeting_Template.pptx", size=100)
        skill = {"id": "s1", "files": [fid]}
        found = find_existing_attachment(
            LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get
        )
        assert found is not None
        assert found["id"] == fid

    def test_a_non_matching_registered_file_is_not_found(self):
        server = FakeServer()
        fid = str(uuid4())
        server.add_file(fid, name="Something Else.pptx", size=100)
        skill = {"id": "s1", "files": [fid]}
        assert (
            find_existing_attachment(LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get)
            is None
        )

    def test_a_bare_label_entry_is_never_treated_as_an_attachment(self):
        """See services/skill_attachments.py's module docstring: a non-uuid
        `files` entry is a typed label whose bytes were never captured — matching
        it by name would resolve a template to nothing and report success."""
        server = FakeServer()
        skill = {"id": "s1", "files": ["GFSS PMO Meeting Template.pptx"]}  # a LABEL, not a uuid
        assert (
            find_existing_attachment(LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get)
            is None
        )
        # Stronger than the None check above: a label is skipped WITHOUT ever
        # being sent as a file id — never merely tolerated after a failed GET.
        # A real `GET /projects/{p}/files/{file_id}` types `file_id` as a UUID
        # path param and would 422 on a non-UUID string rather than cleanly
        # 404 the way this fake does, so "catches the failure and continues"
        # is not an equivalent, safe substitute for "never asks".
        assert server.calls == [], server.calls

    def test_a_dangling_reference_is_skipped_not_fatal(self):
        server = FakeServer()  # file never added -> 404
        skill = {"id": "s1", "files": [str(uuid4())]}
        assert (
            find_existing_attachment(LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get)
            is None
        )

    def test_empty_files_list_is_not_found(self):
        server = FakeServer()
        skill = {"id": "s1", "files": []}
        assert (
            find_existing_attachment(LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get)
            is None
        )

    def test_none_files_is_not_found(self):
        server = FakeServer()
        skill = {"id": "s1", "files": None}
        assert (
            find_existing_attachment(LOCAL, SPACE, skill, "GFSS PMO Meeting Template.pptx", token="t", get=server.get)
            is None
        )


# ===========================================================================
# attach_file_to_skill — must preserve existing entries, never clobber
# ===========================================================================


class TestAttachFileToSkillPreservesExistingEntries:
    def test_appends_without_dropping_prior_entries(self):
        server = FakeServer()
        server.add_skill("s1", name="weekly pmo deck", files=["label-a", str(uuid4())])
        prior = list(server.skills["s1"]["files"])
        skill = dict(server.skills["s1"])

        updated = attach_file_to_skill(LOCAL, SPACE, skill, "new-file-id", token="t", patch=server.patch)

        assert updated["files"] == [*prior, "new-file-id"]

    def test_a_bare_files_list_would_have_dropped_them_the_control(self):
        """Proves the append path is exercised, not a PATCH that happens to
        equal the append because the prior list was empty."""
        server = FakeServer()
        server.add_skill("s1", name="x", files=[])
        skill = dict(server.skills["s1"])
        attach_file_to_skill(LOCAL, SPACE, skill, "new-file-id", token="t", patch=server.patch)
        # now attach a SECOND file and confirm the first one survives
        skill2 = dict(server.skills["s1"])
        attach_file_to_skill(LOCAL, SPACE, skill2, "second-file-id", token="t", patch=server.patch)
        assert server.skills["s1"]["files"] == ["new-file-id", "second-file-id"]

    def test_already_present_file_id_is_a_no_op_and_does_not_write(self):
        server = FakeServer()
        server.add_skill("s1", name="x", files=["already-there"])
        skill = dict(server.skills["s1"])
        attach_file_to_skill(LOCAL, SPACE, skill, "already-there", token="t", patch=server.patch)
        assert ("PATCH", f"/projects/{SPACE}/skills/s1") not in server.calls


# ===========================================================================
# resolve_skill — by id (no lookup) or by name (via formist_cli.resolve)
# ===========================================================================


class TestResolveSkill:
    def test_a_uuid_is_used_directly_no_listing_call(self):
        server = FakeServer()
        sid = str(uuid4())
        server.add_skill(sid, name="weekly pmo deck")
        result = resolve_skill(sid, target=LOCAL, space=SPACE, token="t", fetch=server.get)
        assert result["id"] == sid
        assert not any(c[1].endswith("/skills") for c in server.calls if c[0] == "GET")

    def test_a_unique_name_resolves(self):
        server = FakeServer()
        sid = str(uuid4())
        server.add_skill(sid, name="weekly pmo deck")
        result = resolve_skill("weekly pmo deck", target=LOCAL, space=SPACE, token="t", fetch=server.get)
        assert result["id"] == sid

    def test_an_ambiguous_name_refuses(self):
        server = FakeServer()
        server.add_skill(str(uuid4()), name="dup")
        server.add_skill(str(uuid4()), name="dup")
        with pytest.raises(ResolutionError):
            resolve_skill("dup", target=LOCAL, space=SPACE, token="t", fetch=server.get)

    def test_an_unknown_name_refuses(self):
        server = FakeServer()
        with pytest.raises(ResolutionError):
            resolve_skill("nope", target=LOCAL, space=SPACE, token="t", fetch=server.get)


# ===========================================================================
# CLI-level dry-run / idempotence contract — the production-safety properties
# ===========================================================================


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _base_args(**overrides):
    defaults = dict(
        env="local",
        space=SPACE,
        folder_path="/",
        owner=None,
        file_id=None,
        apply=False,
        json=False,
    )
    defaults.update(overrides)
    return _Args(**defaults)


class TestSkillAttachDryRunTouchesNothing:
    def test_no_apply_makes_no_write_calls(self, tmp_path, monkeypatch, capsys):
        server = FakeServer()
        sid = str(uuid4())
        server.add_skill(sid, name="weekly pmo deck")
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"pretend pptx bytes")

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_skill_attach(_base_args(skill=sid, file=str(path), apply=False))

        assert rc == 0
        assert not any(c[0] in ("POST", "PUT", "PATCH") for c in server.calls), server.calls
        assert "DRY RUN" in capsys.readouterr().err

    def test_apply_actually_writes(self, tmp_path, monkeypatch, capsys):
        server = FakeServer()
        sid = str(uuid4())
        server.add_skill(sid, name="weekly pmo deck")
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"pretend pptx bytes")

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_skill_attach(_base_args(skill=sid, file=str(path), apply=True))

        assert rc == 0
        assert any(c[0] == "PATCH" for c in server.calls)
        assert len(server.skills[sid]["files"]) == 1


class TestSkillAttachIsIdempotent:
    def test_an_already_attached_file_uploads_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        server = FakeServer()
        sid = str(uuid4())
        fid = str(uuid4())
        server.add_file(fid, name="GFSS_PMO_Meeting_Template.pptx", size=999)
        server.add_skill(sid, name="weekly pmo deck", files=[fid])
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"pretend pptx bytes")

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_skill_attach(_base_args(skill=sid, file=str(path), apply=True))

        assert rc == 0
        assert not any(c[0] in ("POST", "PUT", "PATCH") for c in server.calls), server.calls
        out = capsys.readouterr().out
        assert "already attached" in out

    def test_a_size_mismatch_on_an_already_attached_file_is_a_loud_warning_not_a_write(
        self, tmp_path, monkeypatch, capsys
    ):
        """'Already attached' means the NAME resolves — it says nothing about
        whether the bytes are still right. A live-in-prod tool's continuity
        depends on this attachment (the agenda Word skill has real generated
        files today, per the owner's read of prod), so a stale/truncated
        attachment must be surfaced, not silently reported as fine — while
        still doing nothing, because idempotence does not become "overwrite
        it" just because something looks wrong."""
        server = FakeServer()
        sid = str(uuid4())
        fid = str(uuid4())
        server.add_file(fid, name="GFSS_PMO_Meeting_Template.pptx", size=999)
        server.add_skill(sid, name="weekly pmo deck", files=[fid])
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"nineteen bytes here")

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_skill_attach(_base_args(skill=sid, file=str(path), apply=True))

        assert rc == 0, "still a no-op — idempotence, not auto-repair"
        assert not any(c[0] in ("POST", "PUT", "PATCH") for c in server.calls)
        err = capsys.readouterr().err
        assert "size mismatch" in err
        assert "999" in err and "19" in err


class TestSkillAttachSizeIsVerifiedIndependentlyAfterApply:
    """The owner's explicit ask after reading prod: 'read back what you wrote
    and prove the bytes... assert the size that arrived matches the size you
    sent, and do it against prod after the apply, not only in a test.'

    These drive that check from OUTSIDE `upload_file` — a fresh GET the
    completion response never sees — so a bug that makes the completion
    response lie (and the internal check in `upload_file` agree with the lie)
    is still caught.
    """

    def test_a_fresh_upload_whose_independent_readback_disagrees_is_refused(self, tmp_path, monkeypatch):
        server = FakeServer()
        sid = str(uuid4())
        server.add_skill(sid, name="weekly pmo deck")
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"real content, twenty-eight b")

        real_get = server.get

        def lying_get(target, path_, **kw):
            result = real_get(target, path_, **kw)
            if "/files/" in path_ and isinstance(result, dict):
                result = {**result, "size": result["size"] - 1}
            return result

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)
        server.get = lying_get

        with pytest.raises(UploadError, match="read back with size"):
            uploads.cmd_skill_attach(_base_args(skill=sid, file=str(path), apply=True))

        assert not any(c[0] == "PATCH" for c in server.calls), (
            "an unverified file must never be wired into the skill's resolution chain"
        )

    def test_the_file_id_recovery_path_is_verified_against_local_source_too(self, tmp_path, monkeypatch):
        server = FakeServer()
        sid = str(uuid4())
        fid = str(uuid4())
        server.add_file(fid, name="GFSS_PMO_Meeting_Template.pptx", size=999)  # server disagrees...
        server.add_skill(sid, name="weekly pmo deck")
        path = _write_file(tmp_path, "GFSS PMO Meeting Template.pptx", b"only nineteen bytes")  # ...with this

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        with pytest.raises(UploadError, match="read back with size"):
            uploads.cmd_skill_attach(
                _base_args(skill=sid, file=str(path), file_id=fid, apply=True)
            )
        assert not any(c[0] == "PATCH" for c in server.calls)


class TestSkillAttachFileIdRecoveryPath:
    def test_file_id_skips_the_upload_and_attaches_directly(self, monkeypatch, tmp_path):
        server = FakeServer()
        sid = str(uuid4())
        fid = str(uuid4())
        server.add_file(fid, name="GFSS_PMO_Meeting_Template.pptx", size=999)
        server.add_skill(sid, name="weekly pmo deck")

        monkeypatch.setattr(uploads, "_api", lambda: server)
        _patch_effective(monkeypatch, server)

        rc = uploads.cmd_skill_attach(
            _base_args(skill=sid, file="GFSS PMO Meeting Template.pptx", file_id=fid, apply=True)
        )

        assert rc == 0
        assert not any(c[0] in ("POST", "PUT") for c in server.calls), "must not re-upload"
        assert server.skills[sid]["files"] == [fid]


def _patch_effective(monkeypatch, server: FakeServer):
    """Stand in for formist_cli.cli._effective / _token_for without touching prefs
    or stored credentials — the CLI commands import these lazily from
    formist_cli.cli, so they are patched there, matching how formist_cli.chats etc. do it."""
    from formist_cli import cli

    class _Eff:
        target = LOCAL
        space = SPACE

    monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")
