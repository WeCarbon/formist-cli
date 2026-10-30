"""``fm export``: writing a bundle to disk without lying about what landed.

WHY THIS COMMAND IS NOT A THIN WRAPPER
--------------------------------------
There are two export routes and they are not the same KIND of thing:

  ``GET  /projects/{id}/export``         a READ. JSON, ``require_member``, any role.
  ``POST /projects/{id}/export/bundle``  a WRITE. ``routes/workspace_transfer.py:90``
                                         calls ``create_upload``: it inserts a File
                                         row under Files → Exports, writes blob
                                         bytes, and emits ``ON_PROJECT_EXPORTED``.
                                         OWNER/ADMIN.

A command called "export" sounding like a read on both halves is exactly how the
second one gets run against the wrong environment, so the bundle half goes through
``for_write=True`` and can never inherit ``fm use``'s environment. That is the
first property below, and it is behavioural: with an env remembered and none on the
command line, the read proceeds and the write refuses.

THE THREE WAYS A FILE-WRITING COMMAND LIES
------------------------------------------
1. **It overwrites something.** Silently replacing a previous export is
   unrecoverable and looks like success.
2. **It writes nothing and says it worked.** the project rules records a test that passed
   having uploaded ZERO BYTES. An export that creates an empty file and prints a
   path is that failure with a different noun, so an empty body is refused rather
   than written.
3. **It writes somewhere else.** The filename comes from the SERVER
   (``Content-Disposition``), which means a path separator in it is a path the
   operator never chose. ``../../..`` in that header must not escape the target
   directory: and nothing else in this tool is watching that, because nothing else
   takes a filename from a response.

The reported path must be the path that was actually written. That is the same
property ``tests/test_formist_cli_report_provenance.py`` pins for ``fm update``: a
report that prints its intention rather than its outcome confirms whatever
happened.
"""

from __future__ import annotations

import pytest

from formist_cli.export import (
    ExportError,
    filename_from_disposition,
    render_bundle_report,
    safe_filename,
    write_export,
)
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


class TestNeverOverwrite:
    def test_refuses_rather_than_replacing(self, tmp_path):
        target = tmp_path / "evane-project.json"
        target.write_text("the previous export")
        with pytest.raises(ExportError) as exc:
            write_export(target, b'{"new": true}')
        assert str(target) in str(exc.value)
        # The refusal is only worth anything if the old bytes survived it.
        assert target.read_text() == "the previous export"

    def test_writes_when_the_path_is_free(self, tmp_path):
        target = tmp_path / "evane-project.json"
        written = write_export(target, b'{"new": true}')
        assert written.path == target
        assert written.bytes_written == len(b'{"new": true}')
        assert target.read_bytes() == b'{"new": true}'


class TestZeroBytesIsNotAnExport:
    """the project rules: a test passed having uploaded zero bytes. Same shape, new noun."""

    def test_an_empty_body_is_refused(self, tmp_path):
        target = tmp_path / "empty.json"
        with pytest.raises(ExportError) as exc:
            write_export(target, b"")
        assert "0 bytes" in str(exc.value) or "empty" in str(exc.value).lower()

    def test_and_leaves_no_file_behind(self, tmp_path):
        """A refusal that still created the file would be the worse outcome: the
        next run then refuses to overwrite a file this run should never have made."""
        target = tmp_path / "empty.json"
        with pytest.raises(ExportError):
            write_export(target, b"")
        assert not target.exists()


class TestTheServerDoesNotChooseWhereBytesLand:
    """``Content-Disposition`` is a response value. It names a FILE, never a PATH."""

    def test_a_plain_filename_is_read_from_the_header(self):
        assert filename_from_disposition(
            'attachment; filename="evane-project-abc.json"'
        ) == "evane-project-abc.json"

    def test_a_missing_header_is_None_not_a_guess(self):
        assert filename_from_disposition(None) is None
        assert filename_from_disposition("attachment") is None

    def test_a_traversal_in_the_filename_cannot_escape(self):
        """The unique guard. Nothing else in ev takes a filename from a response,
        so nothing else would catch this."""
        assert safe_filename("../../../etc/passwd") == "passwd"
        assert safe_filename("/etc/passwd") == "passwd"
        assert safe_filename("a/b/c.zip") == "c.zip"

    def test_a_filename_that_is_only_separators_is_refused(self):
        """Caught by the EMPTY half: "../../" ends in a slash, so the last segment
        is "" and `not bare` fires. Kept, but see the next test for why it is not
        enough on its own."""
        with pytest.raises(ExportError):
            safe_filename("../../")

    @pytest.mark.parametrize("name", ["..", ".", "a/..", "a/b/."])
    def test_a_dot_segment_that_survives_basename_is_refused(self, name):
        """The unique case of the `bare in {".", ".."}` half, which the test above
        does NOT reach.

        A mutation deleting that half SURVIVED the first run of this file: every
        input then in the suite ended in a separator, so `not bare` caught them all
        and the dot check never ran. Without a trailing slash `"..".rsplit("/")[-1]`
        is `".."`: a non-empty string that basename happily returns, and that
        names the PARENT DIRECTORY.

        Downstream `write_export` would still refuse it (a directory exists, so the
        overwrite check fires), which is exactly what made this survivable and is
        the reason to test the guard at its own level: relying on that would mean
        the refusal an operator sees is "refusing to overwrite /path/.." rather
        than "that names a path, not a file".
        """
        with pytest.raises(ExportError):
            safe_filename(name)

    def test_the_header_and_the_sanitiser_compose(self):
        name = filename_from_disposition(
            'attachment; filename="../../../etc/shadow"'
        )
        assert safe_filename(name) == "shadow"


class TestTheReportPrintsOnlyTheResponse:
    """The property ``_print_report`` keeps, kept here by the same means.

    ``render_bundle_report`` takes the RESPONSE and nothing else, so the request is
    not in scope to be echoed. A structural guarantee, not a discipline: the
    signature is the test.
    """

    def test_it_takes_only_the_response(self):
        import inspect

        params = list(inspect.signature(render_bundle_report).parameters)
        assert params == ["response"], (
            "render_bundle_report must take ONLY the response. Widening it to "
            "accept the request is how a report starts confirming what it hoped "
            "to write instead of what happened."
        )

    def test_it_prints_values_out_of_the_manifest(self):
        """``classes`` is a LIST of ``{name, file, count}``.

        Captured from a real ``manifest.json`` (local dev, 2026-08-27), not read
        off ``services/workspace_transfer.py``. The first version of this test
        wrote it as a dict of name→count, matching what the code assumed; both
        were wrong the same way, so the test passed and the report silently
        omitted every class row. That is the third appearance of this failure in
        this lane, so the fixture is now transcribed rather than reasoned.
        """
        report = render_bundle_report(
            {
                "file_id": "f-1",
                "filename": "evane-export-gfss-20260827T000000Z.zip",
                "manifest": {
                    "bundle_id": "b-1",
                    "source_project_name": "GFSS 2026",
                    "options": {"include_history": False},
                    "classes": [
                        {"name": "project", "file": "data/project.json", "count": 1},
                        {"name": "stakeholder", "file": "data/stakeholder.json",
                         "count": 12},
                    ],
                    "blobs": {"count": 3, "total_bytes": 999},
                    "missing_blobs": ["one"],
                },
            }
        )
        assert "evane-export-gfss-20260827T000000Z.zip" in report
        assert "GFSS 2026" in report
        assert "stakeholder" in report
        assert "12" in report
        # A missing blob is a hole in the bundle. Reporting the count without it
        # would describe an incomplete export as a complete one.
        assert "missing" in report.lower()

    def test_an_unrecognised_classes_shape_is_announced_not_skipped(self):
        """The guard the original bug had no equivalent of.

        Skipping an unknown shape renders as "the bundle contained no classes",
        which is indistinguishable from a real empty bundle: and is exactly how
        the dict-vs-list mistake stayed invisible until the command was run.
        """
        report = render_bundle_report(
            {"filename": "x.zip", "manifest": {"classes": {"stakeholder": 12}}}
        )
        assert "unrecognised shape" in report
        assert "dict" in report

    def test_a_response_without_a_manifest_does_not_invent_one(self):
        report = render_bundle_report({"file_id": "f-1", "filename": "x.zip"})
        assert "x.zip" in report
        assert "0 classes" not in report


class TestTheBundleHalfCannotInheritItsEnvironment:
    """Behavioural, not a comment: the read proceeds and the write refuses."""

    @pytest.fixture(autouse=True)
    def isolated_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMIST_CONFIG_DIR", str(tmp_path / "evane"))

    def test_a_read_export_may_use_the_remembered_environment(self):
        from formist_cli import prefs

        prefs.store(space=SPACE, env="evane-ae")
        eff = prefs.effective(env_flag=None, space_flag=None, for_write=False)
        assert eff.target.env == "ae"

    def test_the_bundle_write_refuses_the_remembered_environment(self):
        from formist_cli import prefs
        from formist_cli.prefs import PrefsError

        prefs.store(space=SPACE, env="evane-ae")
        with pytest.raises(PrefsError) as exc:
            prefs.effective(env_flag=None, space_flag=None, for_write=True)
        assert "--host evane-ae" in str(exc.value)


class TestTheDestinationIsCheckedBeforeTheServerBuilds:
    """``--bundle`` has a side effect, so a doomed run must fail before it.

    ``write_export`` refuses to overwrite, but it runs last: and for the bundle
    "last" is after a POST that already inserted a File row under Files → Exports
    and emitted ``ON_PROJECT_EXPORTED``. Refusing only at the end leaves the space
    holding an artefact this run cannot hand over, and the only way forward is a
    second build, i.e. a second row.
    """

    def test_an_occupied_out_file_is_refused_up_front(self, tmp_path):
        from formist_cli.export import precheck_out

        occupied = tmp_path / "taken.zip"
        occupied.write_text("previous")
        with pytest.raises(ExportError) as exc:
            precheck_out(str(occupied))
        assert "BEFORE building" in str(exc.value)

    def test_a_directory_is_not_refused(self, tmp_path):
        """The destination is not knowable yet: the server names the file."""
        from formist_cli.export import precheck_out

        precheck_out(str(tmp_path))

    def test_no_out_flag_is_not_refused(self):
        from formist_cli.export import precheck_out

        precheck_out(None)

    def test_a_free_path_is_not_refused(self, tmp_path):
        from formist_cli.export import precheck_out

        precheck_out(str(tmp_path / "not-there-yet.zip"))
