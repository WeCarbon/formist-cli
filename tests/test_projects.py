"""``fm project set-logo``: upload a project's logo (WRITE).

Absorbs ``scripts/set_project_logo.py`` (EV-ABSORB.md §3.1, no new endpoint):
that script uploaded a local image to blob and wrote the URL onto
``Project.settings.logo`` over a direct DB session. ``fm project set-logo``
does the same thing through ``POST /projects/{id}/logo``
(``routes/projects.py::upload_project_logo``): a plain multipart upload, magic-
byte sniffed server-side, never the declared Content-Type.

Idempotent (re-running overwrites the stored URL), but still a WRITE: DRY RUN
by default, ``--apply`` performs the upload.

Run: uv run pytest tests/test_formist_cli_projects.py -q
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import projects as projects_mod
from formist_cli.context import Context
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"


class RecordingApi:
    def __init__(self, *, response=None):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self._response = response or {"id": SPACE, "name": "GFSS 2026", "logo": "https://blob/x.png"}

    def post_multipart(self, target, path, *, fields, files, token=None, **kw):
        self.calls.append(("POST", path, fields, files))
        return self._response


def _write_png(tmp_path):
    path = tmp_path / "logo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return path


def _args(**overrides):
    base = {
        "env": "local", "space": SPACE, "subject": "set-logo",
        "space_id": None, "path": None, "apply": False, "json": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture
def driven(monkeypatch):
    def _drive(api_stub, **overrides):
        monkeypatch.setattr(projects_mod, "api", api_stub)
        monkeypatch.setattr(
            projects_mod.context, "resolve",
            lambda args, **kw: Context(target=LOCAL, space=SPACE, token="t", space_source="flag"),
        )
        return projects_mod.run(_args(**overrides))

    return _drive


def test_without_apply_makes_no_upload_call(driven, tmp_path, capsys):
    stub = RecordingApi()
    png = _write_png(tmp_path)
    rc = driven(stub, path=str(png))
    assert rc == 0
    assert stub.calls == []
    assert "DRY RUN" in capsys.readouterr().err


def test_apply_uploads_the_exact_bytes_as_multipart(driven, tmp_path):
    stub = RecordingApi()
    png = _write_png(tmp_path)
    content = png.read_bytes()
    driven(stub, path=str(png), apply=True)
    assert len(stub.calls) == 1
    method, path, fields, files = stub.calls[0]
    assert path == f"/projects/{SPACE}/logo"
    assert files["file"][1] == content, "must upload the exact source bytes, not a truncated/altered copy"


def test_apply_prints_the_stored_logo_url(driven, tmp_path, capsys):
    stub = RecordingApi(response={"id": SPACE, "name": "GFSS 2026", "logo": "https://blob/new.png"})
    png = _write_png(tmp_path)
    driven(stub, path=str(png), apply=True)
    out = capsys.readouterr().out
    assert "https://blob/new.png" in out


def test_missing_file_is_a_loud_error_before_any_network_call(driven, tmp_path):
    stub = RecordingApi()
    missing = tmp_path / "nope.png"
    with pytest.raises(projects_mod.ProjectsError, match="no such file"):
        driven(stub, path=str(missing), apply=True)
    assert stub.calls == []


def test_no_path_is_a_loud_error(driven):
    with pytest.raises(projects_mod.ProjectsError, match="path"):
        driven(RecordingApi(), path=None, apply=True)


def test_project_id_positional_is_an_alternative_to_space_flag(monkeypatch, tmp_path):
    """`fm project set-logo` reads the plan doc's `<id>` positional as an
    alternative to --space, the same convention `fm connectors <space_id>` uses
    (formist_cli/cli.py::cmd_connectors): merged into args.space BEFORE the
    resolver runs, so a caller that only ever reads args.space (like
    formist_cli.context.resolve) sees it regardless of which spelling was used."""
    seen_args = {}

    def _fake_resolve(args, **kw):
        seen_args["space"] = args.space
        return Context(target=LOCAL, space=args.space, token="t", space_source="flag")

    monkeypatch.setattr(projects_mod.context, "resolve", _fake_resolve)
    stub = RecordingApi()
    monkeypatch.setattr(projects_mod, "api", stub)
    png = _write_png(tmp_path)
    projects_mod.run(_args(space=None, space_id=SPACE, path=str(png), apply=True))
    assert seen_args["space"] == SPACE
    assert stub.calls[0][1] == f"/projects/{SPACE}/logo"
