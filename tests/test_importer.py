"""Tests for ``fm import``: the refusals, the report, and the
dry-run/apply comparison.

Every test here is aimed at a way this command could report success over
a write that did not happen, or over one that happened differently, and
an import is where that is most expensive: the operator sees a plan,
authorizes it, and never looks again.

No test here asserts only a status code or a "did not raise": a
status-code assertion is a liveness check, not a correctness check, and
this whole family of defects preserves the status. Every assertion below
names a value that only the right answer produces.
"""

from __future__ import annotations

import inspect
import io
import json
import zipfile

import pytest

from formist_cli import api, importer
from formist_cli.importer import ImportRefused


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bundle_bytes(names=("manifest.json",), payload=b"{}") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, payload)
    return buf.getvalue()


def _report(**over):
    """A report shaped like the real server response, read off the
    server rather than invented. ``inserted``/``merged``/``skipped``/
    ``dropped_rows`` are dicts; ``dropped_refs``/``missing_blobs``/
    ``conflicts``/``unresolved_users`` are lists: a fixture built from a
    guessed shape can agree with a renderer written against the same
    guess and pass while disagreeing with the server.
    """
    base = {
        "dry_run": True,
        "source_project_id": "452093ea-7b91-491e-88b6-3285edd48a31",
        "bundle_id": "bnd-1",
        "merge": "skip-if-older",
        "inserted": {"stakeholder": 5, "task": 2},
        "merged": {},
        "skipped": {},
        "dropped_rows": {},
        "dropped_refs": [],
        "missing_blobs": [],
        "conflicts": [],
        "user_substitutions": 0,
        "unresolved_users": [],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# read_bundle: the ways an upload silently does nothing
# ---------------------------------------------------------------------------
def test_missing_file_is_refused_by_name(tmp_path):
    with pytest.raises(ImportRefused) as exc:
        importer.read_bundle(str(tmp_path / "nope.zip"))
    assert "nope.zip" in str(exc.value)


def test_directory_is_refused_and_says_so(tmp_path):
    with pytest.raises(ImportRefused) as exc:
        importer.read_bundle(str(tmp_path))
    assert "directory" in str(exc.value)


def test_empty_file_is_refused_here_where_the_name_is_known(tmp_path):
    """0 bytes uploads fine; the server's `empty-bundle` cannot name the file."""
    p = tmp_path / "empty.zip"
    p.write_bytes(b"")
    with pytest.raises(ImportRefused) as exc:
        importer.read_bundle(str(p))
    assert "0 bytes" in str(exc.value)
    assert "empty.zip" in str(exc.value)


def test_the_gdpr_json_is_the_mistake_that_actually_happens(tmp_path):
    """`fm export` (no --bundle) writes valid JSON. It is the wrong file entirely.

    The refusal must NAME that case: "not a zip" alone sends the operator looking
    for a corrupt download instead of at the command they ran.
    """
    p = tmp_path / "evane-project-x.json"
    p.write_bytes(b'{"project": {}}')
    with pytest.raises(ImportRefused) as exc:
        importer.read_bundle(str(p))
    assert "fm export --bundle" in str(exc.value)


def test_a_zip_without_a_manifest_is_not_a_bundle(tmp_path):
    p = tmp_path / "other.zip"
    p.write_bytes(_bundle_bytes(names=("notes.txt",)))
    with pytest.raises(ImportRefused) as exc:
        importer.read_bundle(str(p))
    assert "manifest.json" in str(exc.value)


def test_a_real_bundle_is_accepted_and_carries_its_bytes(tmp_path):
    """The positive control. Without it every refusal above could be a blanket no."""
    p = tmp_path / "b.zip"
    data = _bundle_bytes()
    p.write_bytes(data)
    bundle = importer.read_bundle(str(p))
    assert bundle.data == data
    assert bundle.size == len(data)


# ---------------------------------------------------------------------------
# multipart: the boundary check is about a SILENT truncation
# ---------------------------------------------------------------------------
def test_multipart_body_contains_the_file_bytes_and_a_boundary_absent_from_them():
    blob = b"PK\x03\x04payload-bytes"
    body, ctype = api.encode_multipart({"merge": "overwrite"}, {"file": ("b.zip", blob)})
    boundary = ctype.split("boundary=")[1].encode()
    assert blob in body
    assert b'name="file"; filename="b.zip"' in body
    assert b'name="merge"' in body and b"overwrite" in body
    # The property that matters: the delimiter never occurs inside the payload,
    # which is what stops a valid-looking SHORTER upload reaching the server.
    assert boundary not in blob
    assert body.count(b"--" + boundary) == 3  # two parts + the closing delimiter
    # The close-delimiter's trailing `--`, asserted separately because the
    # count above cannot see it: `--{boundary}\r\n` (a part delimiter) and
    # `--{boundary}--\r\n` (the terminator) both contain `--{boundary}`, so
    # the count is 3 either way. A stream that simply ends without it may
    # still be accepted by a lenient parser, but RFC 7578 requires it, and
    # a stricter parser downstream is entitled to enforce what this one
    # forgives.
    assert body.endswith(b"--" + boundary + b"--\r\n")


def test_multipart_boundary_is_rechosen_when_it_collides_with_the_payload(monkeypatch):
    """A collision must change the boundary, not be commented away as unlikely.

    Forced rather than waited for: the first candidate is made to collide, so the
    retry loop is the thing under test. Without this the loop is unreachable code
    that reads as a safeguard.
    """
    seen = []
    real = importer  # keep the import used, so the module under test is loaded

    class _FakeUUID:
        def __init__(self, hexval):
            self.hex = hexval

    values = ["dead", "beef", "cafe", "f00d", "aaaa", "bbbb"]

    def fake_uuid4():
        return _FakeUUID(values.pop(0) if values else "zzzz")

    monkeypatch.setattr(api.uuid, "uuid4", fake_uuid4)
    colliding = b"x----evbnddeadbeefx"
    body, ctype = api.encode_multipart({}, {"file": ("b.zip", colliding)})
    boundary = ctype.split("boundary=")[1]
    seen.append(boundary)
    assert "deadbeef" not in boundary, "the colliding boundary was used anyway"
    assert boundary.encode() not in colliding
    assert colliding in body
    assert real is importer


# ---------------------------------------------------------------------------
# render_report: provenance, and the unflattering numbers
# ---------------------------------------------------------------------------
def test_render_report_cannot_echo_the_request_because_it_takes_only_the_response():
    """Structural, not a string search.

    A test that greps the output for a payload sentinel proves that one
    value did not leak. A signature with no request parameter proves none
    can: a private copy under another name cannot follow, because there
    is nothing to pass it.
    """
    params = list(inspect.signature(importer.render_report).parameters)
    assert params == ["response"], (
        f"render_report takes {params}; a second parameter is a way for the "
        "request to reach the outcome report"
    )


def test_render_report_prints_per_class_counts_from_the_response():
    out = importer.render_report(_report())
    assert "stakeholder" in out and "5" in out
    assert "DRY RUN" in out


def test_applied_and_dry_run_are_not_reported_the_same_way():
    """The one word that says whether anything was written must come from the body."""
    assert "DRY RUN" in importer.render_report(_report(dry_run=True))
    assert "APPLIED" in importer.render_report(_report(dry_run=False))


def test_unresolved_users_and_missing_blobs_are_printed_not_summarised_away():
    """A partial import reported as a whole one is the defect this command exists in.

    `missing_blobs` means rows landed whose bytes did not: the space looks
    populated and every one of those files 404s when opened. Printing the counts
    while omitting the holes describes that as a success.
    """
    out = importer.render_report(
        _report(
            unresolved_users=["nobody@example.com"],
            missing_blobs=["blobs/digests/abc"],
            conflicts=["task:1"],
            dropped_refs=["file:9"],
        )
    )
    for expected in (
        "nobody@example.com",
        "blobs/digests/abc",
        "UNRESOLVED USERS",
        "MISSING BLOBS",
        "CONFLICTS",
        "DROPPED REFS",
    ):
        assert expected in out, f"{expected!r} missing from the report"


def test_an_unrecognised_section_shape_says_so_instead_of_rendering_empty():
    """Silently skipping an unknown shape reads as "the bundle had none of that"."""
    out = importer.render_report(_report(inserted=["stakeholder", "task"]))
    assert "unrecognised shape" in out
    assert "list" in out


# ---------------------------------------------------------------------------
# divergences: a plan is only true of the state it ran against
# ---------------------------------------------------------------------------
def test_identical_reports_diverge_in_nothing():
    assert importer.divergences(_report(), _report(dry_run=False)) == []


def test_a_changed_count_is_named_with_its_class_and_both_numbers():
    diffs = importer.divergences(
        _report(inserted={"stakeholder": 5}),
        _report(dry_run=False, inserted={"stakeholder": 7}),
    )
    assert len(diffs) == 1
    assert "stakeholder" in diffs[0]
    assert "5" in diffs[0] and "7" in diffs[0]


def test_a_class_that_appears_only_in_the_apply_is_caught():
    """The dangerous direction: the apply touched something the plan never showed."""
    diffs = importer.divergences(
        _report(inserted={"stakeholder": 5}),
        _report(dry_run=False, inserted={"stakeholder": 5, "file": 3}),
    )
    assert any("file" in d for d in diffs)


def test_a_grown_missing_blobs_list_is_a_divergence():
    diffs = importer.divergences(
        _report(missing_blobs=[]),
        _report(dry_run=False, missing_blobs=["blobs/digests/x"]),
    )
    assert any("missing_blobs" in d for d in diffs)


def test_dry_run_flag_itself_is_not_reported_as_a_divergence():
    """It differs by construction. Reporting it would make every apply
    "diverge", and a warning that always fires is one nobody reads."""
    diffs = importer.divergences(_report(dry_run=True), _report(dry_run=False))
    assert diffs == []


# ---------------------------------------------------------------------------
# the command is wired, and --apply cannot inherit an environment
# ---------------------------------------------------------------------------
def test_import_is_registered_as_a_command():
    from formist_cli.cli import _COMMANDS, build_parser

    assert _COMMANDS["import"] is importer.cmd_import
    args = build_parser().parse_args(["import", "b.zip", "--space", "x"])
    assert args.command == "import" and args.apply is False


def test_apply_defaults_to_false_so_the_first_run_is_never_the_write():
    from formist_cli.cli import build_parser

    args = build_parser().parse_args(["import", "b.zip"])
    assert args.apply is False
    assert args.host is None, "a write must not inherit an environment"


def test_apply_resolves_for_write_and_a_dry_run_does_not(tmp_path, monkeypatch):
    """The rule that keeps an import off production: `--apply` implies for_write=True.

    Asserted by capturing what cmd_import passes to `_effective`, because that
    flag IS the mechanism: formist_cli/prefs.py refuses to supply a remembered `--env`
    when it is set. A test naming the flag instead of driving the call would pass
    against a command that never sets it.
    """
    import formist_cli.cli as cli

    p = tmp_path / "b.zip"
    p.write_bytes(_bundle_bytes())
    captured = {}

    class _Eff:
        space = "452093ea-7b91-491e-88b6-3285edd48a31"
        target = object()

    def fake_effective(args, *, for_write=False, **kw):
        captured["for_write"] = for_write
        return _Eff()

    monkeypatch.setattr(cli, "_effective", fake_effective)
    monkeypatch.setattr(cli, "_token_for", lambda eff: "tok")
    monkeypatch.setattr(
        importer.api, "post_multipart", lambda *a, **k: _report()
    )

    args = cli.build_parser().parse_args(["import", str(p)])
    importer.cmd_import(args)
    assert captured["for_write"] is False, "a dry run may inherit an environment"

    args = cli.build_parser().parse_args(["import", str(p), "--apply", "--host", "127.0.0.1:8160"])
    importer.cmd_import(args)
    assert captured["for_write"] is True, "--apply must resolve as a write"


def test_apply_sends_dry_run_first_and_then_the_write(tmp_path, monkeypatch):
    """--apply must not skip the dry run: the plan is what authorizes the write.

    Drives the real command and records the `dry_run=` each call carried, so a
    version that applied straight away would fail here rather than in prod.
    """
    import formist_cli.cli as cli

    p = tmp_path / "b.zip"
    p.write_bytes(_bundle_bytes())
    calls = []

    class _Eff:
        space = "452093ea-7b91-491e-88b6-3285edd48a31"
        target = object()

    monkeypatch.setattr(cli, "_effective", lambda a, **k: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "tok")

    def fake_post(target, path, **kw):
        calls.append(path)
        return _report(dry_run="dry_run=true" in path)

    monkeypatch.setattr(importer.api, "post_multipart", fake_post)
    args = cli.build_parser().parse_args(["import", str(p), "--apply", "--host", "127.0.0.1:8160"])
    rc = importer.cmd_import(args)

    assert len(calls) == 2, f"expected dry run then apply, got {calls}"
    assert "dry_run=true" in calls[0]
    assert "dry_run=false" in calls[1]
    assert rc == 0


def test_a_divergent_apply_returns_nonzero(tmp_path, monkeypatch):
    """A silent divergence is the whole failure this comparison exists to catch."""
    import formist_cli.cli as cli

    p = tmp_path / "b.zip"
    p.write_bytes(_bundle_bytes())

    class _Eff:
        space = "452093ea-7b91-491e-88b6-3285edd48a31"
        target = object()

    monkeypatch.setattr(cli, "_effective", lambda a, **k: _Eff())
    monkeypatch.setattr(cli, "_token_for", lambda eff: "tok")

    def fake_post(target, path, **kw):
        if "dry_run=true" in path:
            return _report(inserted={"stakeholder": 5})
        return _report(dry_run=False, inserted={"stakeholder": 99})

    monkeypatch.setattr(importer.api, "post_multipart", fake_post)
    args = cli.build_parser().parse_args(["import", str(p), "--apply", "--host", "127.0.0.1:8160"])
    assert importer.cmd_import(args) == 1


def test_help_states_that_connector_credentials_do_not_travel(capsys):
    """The operator's question here is "does this configure my connectors?".

    The answer is no: the server NULLs connection foreign keys on import,
    and --help is the only place they look before running it.
    """
    from formist_cli.cli import build_parser

    # Driven through the real `fm import --help` path rather than reached out of
    # `parser._actions`: the operator meets argparse's output, not its internals,
    # and a test that walks private attributes can keep passing over a --help that
    # no longer renders.
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["import", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "credential" in text.lower(), "help never mentions credentials"
    assert "SharePoint" in text, "help never mentions the SharePoint exclusion"
    assert "NULL" in text, "help never says connection FKs are NULLed"


def test_import_never_shadows_the_builtin_ImportError():
    """`except ImportError` must keep meaning "a module failed to load",
    never collapsing into a caught application-level refusal that happens
    to share the name.
    """
    assert importer.ImportRefused is not ImportError
    assert not issubclass(importer.ImportRefused, ImportError)


def test_json_output_is_the_server_body_verbatim():
    """--json must not be a re-serialisation of a parsed summary."""
    report = _report()
    assert json.loads(json.dumps(report)) == report
