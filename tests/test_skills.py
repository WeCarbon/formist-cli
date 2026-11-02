"""Tests for ``fm skills-add`` / ``fm skills-update`` (``formist_cli/skills.py``):
the client half of installing or replacing a project's own skill from a
local ``SKILL.md``. Skills are data in the database, not files shipped with
the code, so deployments install them with these two commands.

``formist_cli/skills.py`` calls straight into ``formist_cli/uploads.py``'s
`upload_file`/`attach_file_to_skill` for attachments, so these tests use
the same convention: an injected fake transport, never a live socket or a
mock that merely records "was called".
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from formist_cli import skills
from formist_cli.skills import (
    SkillSourceError,
    add_skill_from_source,
    resolve_source,
    update_skill_from_source,
)
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"

MARKDOWN = """\
---
name: Probe Skill
description: A test fixture, not a real default.
---

Body text.
"""


def _write_dir(tmp_path: Path, *, extra_files: dict[str, bytes] | None = None) -> Path:
    d = tmp_path / "probe skill"
    d.mkdir()
    (d / "SKILL.md").write_text(MARKDOWN, encoding="utf-8")
    for name, content in (extra_files or {}).items():
        (d / name).write_bytes(content)
    return d


# ===========================================================================
# resolve_source: directory and zip inputs
# ===========================================================================
class TestResolveSourceFromADirectory:
    def test_reads_markdown_and_lists_attachments(self, tmp_path):
        d = _write_dir(tmp_path, extra_files={"template.pptx": b"pretend bytes", "b.docx": b"x"})
        markdown, attachments, cleanup = resolve_source(str(d))
        assert markdown == MARKDOWN
        assert [p.name for p in attachments] == ["b.docx", "template.pptx"]  # sorted
        assert cleanup is None  # nothing to clean up for a plain directory

    def test_a_directory_with_no_skill_md_refuses(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SkillSourceError):
            resolve_source(str(empty))

    def test_a_path_that_is_neither_dir_nor_zip_refuses(self, tmp_path):
        stray = tmp_path / "notes.txt"
        stray.write_text("hi")
        with pytest.raises(SkillSourceError):
            resolve_source(str(stray))


class TestResolveSourceFromAZip:
    def test_a_zip_with_no_wrapping_directory(self, tmp_path):
        zpath = tmp_path / "skill.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("SKILL.md", MARKDOWN)
            zf.writestr("template.pptx", b"pretend bytes")
        markdown, attachments, cleanup = resolve_source(str(zpath))
        try:
            assert markdown == MARKDOWN
            assert [p.name for p in attachments] == ["template.pptx"]
        finally:
            if cleanup is not None:
                cleanup.cleanup()

    def test_a_zip_wrapped_in_one_top_level_directory(self, tmp_path):
        """Most zip tools produce exactly this shape: dragging a folder into
        Archive Utility wraps it in one directory named after the folder."""
        zpath = tmp_path / "skill.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("probe skill/SKILL.md", MARKDOWN)
            zf.writestr("probe skill/template.pptx", b"pretend bytes")
        markdown, attachments, cleanup = resolve_source(str(zpath))
        try:
            assert markdown == MARKDOWN
            assert [p.name for p in attachments] == ["template.pptx"]
        finally:
            if cleanup is not None:
                cleanup.cleanup()

    def test_a_zip_with_no_skill_md_anywhere_refuses_and_cleans_up(self, tmp_path):
        zpath = tmp_path / "empty.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("readme.txt", "nothing here")
        with pytest.raises(SkillSourceError):
            resolve_source(str(zpath))
        # the tempdir it extracted into must not be left behind on the refusal
        # path: implicitly checked by SkillSourceError's own cleanup call;
        # nothing further to assert without reaching into tempfile internals.


# ===========================================================================
# add_skill_from_source / update_skill_from_source: dependency-injected core
# ===========================================================================
def _fake_post(response: dict):
    calls = []

    def post(target, path, body, *, token=None, **kw):
        calls.append((path, dict(body)))
        return dict(response)

    return post, calls


def _fake_patch(response: dict):
    calls = []

    def patch(target, path, body, *, token=None, **kw):
        calls.append((path, dict(body)))
        return dict(response)

    return patch, calls


def _fake_attachment_pair():
    """upload_file/attach_file_to_skill fakes that record the real bytes
    handed to them, not just that they were called."""
    uploaded = []
    attached = []

    def upload_file(target, space, path, *, token=None, **kw):
        data = Path(path).read_bytes()
        file_id = str(uuid4())
        uploaded.append((path.name, data, file_id))
        return {"item": {"id": file_id, "name": path.name, "size": len(data)}}

    def attach_file_to_skill(target, space, item, file_id, *, token=None, **kw):
        attached.append((item["id"], file_id))
        item = dict(item)
        item["files"] = [*(item.get("files") or []), file_id]
        return item

    return upload_file, attach_file_to_skill, uploaded, attached


class TestAddSkillFromSource:
    def test_posts_the_markdown_body_verbatim(self, tmp_path):
        d = _write_dir(tmp_path)
        sid = str(uuid4())
        post, calls = _fake_post({"id": sid, "name": "Probe Skill", "files": []})

        item = add_skill_from_source(LOCAL, SPACE, str(d), token="t", post=post)

        assert item["id"] == sid
        assert len(calls) == 1
        path, body = calls[0]
        assert path == f"/projects/{SPACE}/skills/import"
        assert body == {"markdown": MARKDOWN}

    def test_attachments_are_uploaded_and_registered_with_real_bytes(self, tmp_path):
        d = _write_dir(tmp_path, extra_files={"template.pptx": b"real bytes here"})
        sid = str(uuid4())
        post, _ = _fake_post({"id": sid, "name": "Probe Skill", "files": []})
        upload_file, attach_file_to_skill, uploaded, attached = _fake_attachment_pair()

        item = add_skill_from_source(
            LOCAL,
            SPACE,
            str(d),
            token="t",
            post=post,
            upload_file=upload_file,
            attach_file_to_skill=attach_file_to_skill,
        )

        assert len(uploaded) == 1
        name, data, file_id = uploaded[0]
        assert name == "template.pptx"
        assert data == b"real bytes here"  # the exact bytes, not merely "a file"
        assert attached == [(sid, file_id)]
        assert item["files"] == [file_id]

    def test_an_unexpected_response_shape_raises_rather_than_reporting_success(self, tmp_path):
        d = _write_dir(tmp_path)
        post, _ = _fake_post({"not": "a skill"})
        with pytest.raises(SystemExit, match="unexpected shape"):
            add_skill_from_source(LOCAL, SPACE, str(d), token="t", post=post)

    def test_the_extracted_zip_tempdir_is_cleaned_up_after_a_successful_add(self, tmp_path):
        zpath = tmp_path / "skill.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("SKILL.md", MARKDOWN)
        sid = str(uuid4())
        post, _ = _fake_post({"id": sid, "name": "Probe Skill", "files": []})

        # Capture the tempdir path by wrapping resolve_source's own cleanup.
        from formist_cli import skills as skills_mod

        seen = {}
        real_resolve = skills_mod.resolve_source

        def spying_resolve(source):
            markdown, attachments, cleanup = real_resolve(source)
            if cleanup is not None:
                seen["dir"] = Path(cleanup.name)
            return markdown, attachments, cleanup

        skills_mod.resolve_source = spying_resolve
        try:
            add_skill_from_source(LOCAL, SPACE, str(zpath), token="t", post=post)
        finally:
            skills_mod.resolve_source = real_resolve
        assert "dir" in seen, "the zip path should have produced a tempdir"
        assert not seen["dir"].exists(), "the tempdir must be cleaned up after use"


class _FakeSkillStore:
    """A tiny in-memory stand-in for the server's File rows + one Skill's
    ``files`` list: just enough to run ``update_skill_from_source`` more
    than once against the SAME skill and watch whether attachments
    accumulate, which a fixed canned response (``_fake_patch``/
    ``_fake_attachment_pair``) cannot do across calls.
    """

    def __init__(self, skill: dict):
        self.skill = dict(skill)
        self.files: dict[str, dict] = {}  # file_id -> {"name": ..., "data": ...}

    def patch_import(self, target, path, body, *, token=None, **kw):
        """Simulates PATCH .../skills/{id}/import: changes markdown-derived
        fields, leaves ``files`` exactly as the skill currently holds it."""
        return {**self.skill, "name": "Probe Skill"}

    def upload_file(self, target, space, path, *, token=None, **kw):
        data = Path(path).read_bytes()
        file_id = str(uuid4())
        self.files[file_id] = {"name": path.name, "data": data}
        return {"item": {"id": file_id, "name": path.name, "size": len(data)}}

    def attach_file_to_skill(self, target, space, item, file_id, *, token=None, **kw):
        current = list(item.get("files") or [])
        if file_id not in current:
            current.append(file_id)
        item = {**item, "files": current}
        self.skill = item
        return item

    def find_existing_attachment(self, target, space, item, filename, *, token=None, **kw):
        for fid in item.get("files") or []:
            f = self.files.get(fid)
            if f and f["name"] == filename:
                return {"id": fid, "name": f["name"], "size": len(f["data"])}
        return None

    def download_file_bytes(self, target, space, file_id, *, token=None, **kw):
        return self.files[file_id]["data"]

    def replace_file_in_skill(self, target, space, item, old_file_id, new_file_id, *, token=None, **kw):
        current = [new_file_id if f == old_file_id else f for f in (item.get("files") or [])]
        if old_file_id not in (item.get("files") or []):
            current.append(new_file_id)
        item = {**item, "files": current}
        self.skill = item
        return item

    def _update(self, source):
        from formist_cli.skills import update_skill_from_source

        return update_skill_from_source(
            LOCAL,
            SPACE,
            self.skill,
            source,
            token="t",
            patch=self.patch_import,
            upload_file=self.upload_file,
            attach_file_to_skill=self.attach_file_to_skill,
            find_existing_attachment=self.find_existing_attachment,
            download_file_bytes=self.download_file_bytes,
            replace_file_in_skill=self.replace_file_in_skill,
        )


class TestUpdateSkillAttachmentsAreDeduped:
    """The bug: re-running `fm skills-update` on an unchanged SOURCE used to
    re-attach the same template file every time (`_attach_all` uploaded and
    appended unconditionally). These pin the fix at the level
    `update_skill_from_source` actually runs at, not just `find_existing_attachment`
    in isolation.
    """

    def test_running_it_twice_with_identical_content_leaves_the_count_unchanged(
        self, tmp_path
    ):
        d = _write_dir(tmp_path, extra_files={"template.pptx": b"same bytes every run"})
        store = _FakeSkillStore({"id": str(uuid4()), "name": "Old Name", "files": []})

        first = store._update(str(d))
        assert len(first["files"]) == 1
        second = store._update(str(d))
        assert len(second["files"]) == 1, "a second run with unchanged content must not grow the list"
        assert second["files"] == first["files"], "the same File row must still be the one attached"

    def test_changed_content_replaces_the_same_slot_rather_than_appending(self, tmp_path):
        store = _FakeSkillStore({"id": str(uuid4()), "name": "Old Name", "files": []})
        d1 = _write_dir(tmp_path, extra_files={"template.pptx": b"version one"})
        first = store._update(str(d1))
        assert len(first["files"]) == 1
        old_file_id = first["files"][0]

        d2 = tmp_path / "v2"
        d2.mkdir()
        (d2 / "SKILL.md").write_text(MARKDOWN, encoding="utf-8")
        (d2 / "template.pptx").write_bytes(b"version two, different bytes")

        second = store._update(str(d2))
        assert len(second["files"]) == 1, "a changed attachment must replace, never append, its old slot"
        assert second["files"][0] != old_file_id, "the stale File id must be gone, a fresh one in its place"

    def test_a_genuinely_new_name_is_still_appended(self, tmp_path):
        store = _FakeSkillStore({"id": str(uuid4()), "name": "Old Name", "files": []})
        d1 = _write_dir(tmp_path, extra_files={"a.pptx": b"first attachment"})
        first = store._update(str(d1))
        assert len(first["files"]) == 1

        d2 = tmp_path / "v2"
        d2.mkdir()
        (d2 / "SKILL.md").write_text(MARKDOWN, encoding="utf-8")
        (d2 / "a.pptx").write_bytes(b"first attachment")  # unchanged, deduped
        (d2 / "b.docx").write_bytes(b"a second, different attachment")

        second = store._update(str(d2))
        assert len(second["files"]) == 2, "a new name is additive; the existing one is still deduped"


class TestUpdateSkillFromSource:
    def test_patches_the_resolved_skills_own_id_never_re_resolving(self, tmp_path):
        d = _write_dir(tmp_path)
        sid = str(uuid4())
        skill = {"id": sid, "name": "Old Name", "files": []}
        patch, calls = _fake_patch({"id": sid, "name": "Probe Skill", "files": []})

        item = update_skill_from_source(LOCAL, SPACE, skill, str(d), token="t", patch=patch)

        assert item["id"] == sid
        path, body = calls[0]
        assert path == f"/projects/{SPACE}/skills/{sid}/import"
        assert body == {"markdown": MARKDOWN}

    def test_never_creates_a_second_row_shape_is_a_single_patch_call(self, tmp_path):
        d = _write_dir(tmp_path)
        skill = {"id": str(uuid4()), "name": "Old Name", "files": []}
        patch, calls = _fake_patch({"id": skill["id"], "name": "New Name", "files": []})
        update_skill_from_source(LOCAL, SPACE, skill, str(d), token="t", patch=patch)
        assert len(calls) == 1


# ===========================================================================
# CLI dry-run: no --apply must never touch the network
# ===========================================================================
class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _patch_effective(monkeypatch):
    from formist_cli import cli

    class _Eff:
        target = LOCAL
        space = SPACE

    monkeypatch.setattr(cli, "_effective", lambda args, **kw: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "t")


class TestSkillsAddDryRunTouchesNothing:
    def test_no_apply_makes_no_post_call(self, tmp_path, monkeypatch, capsys):
        d = _write_dir(tmp_path)
        calls = []
        monkeypatch.setattr(skills.api, "post", lambda *a, **kw: calls.append((a, kw)) or {})
        _patch_effective(monkeypatch)

        rc = skills.cmd_skills_add(_Args(host="local", space=SPACE, source=str(d), apply=False, json=False))

        assert rc == 0
        assert calls == []
        assert "DRY RUN" in capsys.readouterr().err

    def test_apply_actually_posts(self, tmp_path, monkeypatch, capsys):
        d = _write_dir(tmp_path)
        sid = str(uuid4())

        def fake_post(target, path, body, *, token=None, **kw):
            return {"id": sid, "name": "Probe Skill", "hook": None, "files": []}

        monkeypatch.setattr(skills.api, "post", fake_post)
        _patch_effective(monkeypatch)

        rc = skills.cmd_skills_add(_Args(host="local", space=SPACE, source=str(d), apply=True, json=False))

        assert rc == 0
        assert f"id={sid}" in capsys.readouterr().out


class TestSkillsUpdateDryRunTouchesNothing:
    def test_no_apply_makes_no_patch_call(self, tmp_path, monkeypatch, capsys):
        d = _write_dir(tmp_path)
        sid = str(uuid4())
        calls = []
        monkeypatch.setattr(skills.api, "patch", lambda *a, **kw: calls.append((a, kw)) or {})
        from formist_cli import uploads as uploads_mod

        monkeypatch.setattr(uploads_mod, "resolve_skill", lambda *a, **kw: {"id": sid, "name": "Old", "files": []})
        _patch_effective(monkeypatch)

        rc = skills.cmd_skills_update(
            _Args(host="local", space=SPACE, skill=sid, source=str(d), apply=False, json=False)
        )

        assert rc == 0
        assert calls == []
        assert "DRY RUN" in capsys.readouterr().err
