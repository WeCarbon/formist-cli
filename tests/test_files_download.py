"""``fm files download`` — every file under a folder, recursively, into a
local directory.

Its own file, matching `test_formist_cli_files.py`'s own docstring reasoning
("ENTERED THROUGH cmd_files, NOT the reevaluate half directly" — a third
subcommand is exactly the risk that dispatch line exists to catch, so this
file goes through `cmd_files` too, never calling `_run_download` directly).

WHAT THIS FILE IS ACTUALLY ABOUT. `GET /projects/{id}/files?folder=...`
(`routes/entities.py::list_files`) exists and is exact-match only — by owner
directive it deliberately does NOT cascade into subfolders (see
`formist_cli/files.py`'s module docstring). So `fm files download` cannot use that
filter for "under a folder, recursively" and instead pages through the WHOLE
listing and filters by path client-side — the property these tests are built
around is that this recursive filter is neither too narrow (misses a nested
file) nor too wide (matches a sibling folder with a similar name prefix).

Run: uv run pytest tests/test_formist_cli_files_download.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import files as files_mod
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


def _make_file(file_id: str, path: str, content: bytes) -> dict:
    return {
        "id": file_id,
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "size": len(content),
        "_bytes": content,
    }


class FakeFilesApi:
    """Stands in for the two real endpoints this command uses:
    `GET /projects/{id}/files` (paginated listing) and
    `GET /projects/{id}/files/{id}/download` (raw bytes + Content-Disposition,
    `api.download`'s own return shape)."""

    def __init__(self, files: list[dict]):
        self.files = files
        self.calls: list[tuple[str, str]] = []

    def get(self, target, path, *, token=None, params=None, **kw):
        self.calls.append(("GET", path))
        assert path == f"/projects/{SPACE}/files"
        limit = int((params or {}).get("limit", 200))
        offset = int((params or {}).get("offset", 0))
        page = self.files[offset : offset + limit]
        return {"items": page, "total": len(self.files), "limit": limit, "offset": offset}

    def download(self, target, path, *, token=None, **kw):
        self.calls.append(("DOWNLOAD", path))
        file_id = path.rsplit("/", 2)[1]
        file = next(f for f in self.files if f["id"] == file_id)
        return file["_bytes"], f'attachment; filename="{file["name"]}"'


def _args(**overrides):
    defaults = dict(env="local", space=SPACE, folder="/Photos", out=None, json=False)
    defaults.update(overrides)
    ns = argparse.Namespace(**defaults)
    ns.subject = "download"
    return ns


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, **overrides):
        monkeypatch.setattr(files_mod, "api", api_stub)
        monkeypatch.setattr(
            files_mod.context, "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        return files_mod.cmd_files(_args(**overrides))

    return _drive


# ===========================================================================
# is_under_folder / _normalize_folder / relative_download_path — pure
# ===========================================================================
class TestIsUnderFolder:
    def test_root_matches_everything(self):
        assert files_mod.is_under_folder("/", "/anything/at/all.txt")

    def test_a_direct_child_matches(self):
        assert files_mod.is_under_folder("/Photos", "/Photos/a.jpg")

    def test_a_grandchild_matches_recursively(self):
        assert files_mod.is_under_folder("/Photos", "/Photos/Day 1/Stage/a.jpg")

    def test_a_sibling_folder_with_a_shared_prefix_does_not_match(self):
        """The bug a bare `.startswith('/Photos')` would have: '/PhotosArchive'
        is not under '/Photos' just because the string starts the same way."""
        assert not files_mod.is_under_folder("/Photos", "/PhotosArchive/x.txt")

    def test_folder_given_without_a_leading_slash_still_normalizes(self):
        assert files_mod.is_under_folder("Photos", "/Photos/a.jpg")


class TestRelativeDownloadPath:
    def test_a_direct_child_is_just_its_name(self):
        assert files_mod.relative_download_path("/Photos", "/Photos/a.jpg") == "a.jpg"

    def test_a_nested_file_keeps_its_subfolder(self):
        assert (
            files_mod.relative_download_path("/Photos", "/Photos/Day 1/a.jpg")
            == "Day 1/a.jpg"
        )

    def test_root_folder_strips_only_the_leading_slash(self):
        assert files_mod.relative_download_path("/", "/Photos/a.jpg") == "Photos/a.jpg"

    def test_a_path_traversal_attempt_is_refused(self):
        with pytest.raises(files_mod.FilesError):
            files_mod.relative_download_path("/Photos", "/Photos/../../etc/passwd")


# ===========================================================================
# fm files download — end to end, through cmd_files
# ===========================================================================
class TestDownloadRecursesAndPreservesSubfolders:
    def test_nested_files_download_and_a_sibling_folder_is_excluded(self, tmp_path, driven, capsys):
        files = [
            _make_file("f1", "/Photos/a.jpg", b"AAA"),
            _make_file("f2", "/Photos/Day 1/b.jpg", b"BB"),
            _make_file("f3", "/PhotosArchive/c.jpg", b"C"),
        ]
        out_dir = tmp_path / "out"

        rc = driven(FakeFilesApi(files), folder="/Photos", out=str(out_dir))

        assert rc == 0
        assert (out_dir / "a.jpg").read_bytes() == b"AAA"
        assert (out_dir / "Day 1" / "b.jpg").read_bytes() == b"BB"
        assert not (out_dir / "c.jpg").exists()
        assert not list(out_dir.rglob("c.jpg"))
        out = capsys.readouterr().out
        assert "2 downloaded, 0 failed, 2 total" in out or "downloaded  a.jpg" in out


# ===========================================================================
# The required pagination test: the listing endpoint caps a page, so the
# command must walk every page, not just the first.
# ===========================================================================
class TestDownloadPagesThroughTheWholeListing:
    def test_every_page_is_walked_not_just_the_first(self, tmp_path, driven, monkeypatch):
        files = [_make_file(f"f{i}", f"/Photos/{i}.txt", f"{i}".encode()) for i in range(5)]
        monkeypatch.setattr(files_mod, "_LIST_PAGE_SIZE", 2)
        api_stub = FakeFilesApi(files)
        out_dir = tmp_path / "out"

        rc = driven(api_stub, folder="/Photos", out=str(out_dir))

        assert rc == 0
        get_calls = [c for c in api_stub.calls if c[0] == "GET"]
        assert len(get_calls) >= 3, "5 rows at page size 2 must take at least 3 pages"
        for i in range(5):
            assert (out_dir / f"{i}.txt").read_bytes() == f"{i}".encode()

    def test_a_single_page_the_control(self, tmp_path, driven, monkeypatch):
        """Without this, 'at least 3 pages' above could pass for a command
        that always pages 3 times regardless of how much data there is."""
        files = [_make_file("f0", "/Photos/0.txt", b"0")]
        monkeypatch.setattr(files_mod, "_LIST_PAGE_SIZE", 200)
        api_stub = FakeFilesApi(files)
        out_dir = tmp_path / "out"

        driven(api_stub, folder="/Photos", out=str(out_dir))

        get_calls = [c for c in api_stub.calls if c[0] == "GET"]
        assert len(get_calls) == 1


# ===========================================================================
# The required failure-path test: a size mismatch is a per-file failure, and
# does not stop the rest of the batch.
# ===========================================================================
class TestDownloadSizeMismatchIsAFailureNotASilentShortFile:
    def test_a_mismatched_size_is_reported_and_the_exit_code_is_nonzero(
        self, tmp_path, driven, capsys
    ):
        good = _make_file("f1", "/Photos/good.jpg", b"GOODBYTES")
        bad = _make_file("f2", "/Photos/bad.jpg", b"SHORT")
        bad["size"] = 999  # the listing lies about what the download will return
        out_dir = tmp_path / "out"

        rc = driven(FakeFilesApi([good, bad]), folder="/Photos", out=str(out_dir))

        assert rc == 1
        assert (out_dir / "good.jpg").read_bytes() == b"GOODBYTES", "one bad file must not stop the rest"
        combined = "".join(capsys.readouterr())
        assert "bad.jpg" in combined
        assert "999" in combined

    def test_the_control_matching_sizes_all_succeed(self, tmp_path, driven):
        """Without this, rc==1 above could just mean the command always fails."""
        files = [_make_file("f1", "/Photos/a.jpg", b"AAA"), _make_file("f2", "/Photos/b.jpg", b"BB")]
        out_dir = tmp_path / "out"
        rc = driven(FakeFilesApi(files), folder="/Photos", out=str(out_dir))
        assert rc == 0
