"""``fm project set-logo`` — upload a project's logo image (WRITE).

Absorbs the retired ``set_project_logo.py`` script (EV-ABSORB.md §3.1, no new endpoint):
that script uploaded a local image straight to blob and wrote the resulting URL
onto ``Project.settings.logo`` over a direct DB session. This command reaches
the SAME outcome through ``POST /projects/{id}/logo``
(``routes/projects.py::upload_project_logo``): a plain multipart upload whose
bytes are magic-byte sniffed server-side (png/jpeg/gif/webp) — never the
declared Content-Type, which is the check that route's own docstring records
replacing a weaker one that let an SVG (a document that can carry ``<script>``)
through as an "image".

Idempotent — re-running overwrites the stored URL — but still a WRITE: DRY RUN
by default, ``--apply`` performs the upload. Unlike the retired ``set_project_logo.py``
there is no ``--url`` reuse-an-existing-blob mode: that shortcut existed only
because the script talked to the database directly and could set
``Project.settings.logo`` to an arbitrary string; the route always uploads
what it is given.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

from formist_cli import api, context
from formist_cli.cli_support import build_standalone_parser as _build_standalone_parser


class ProjectsError(Exception):
    """A refusal about the project surface, written for an operator."""


SUBJECTS = ("set-logo",)

_DESCRIPTION = """Project-level operations.

  fm project set-logo <path> --space <id>            dry run
  fm project set-logo <path> --space <id> --apply     upload for real

A project id/UUID may also be given positionally instead of --space, matching
`fm connectors <space-id>`.
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "project",
        help="project-level ops: set-logo (WRITE)",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    from formist_cli.cli import _add_env, _add_space

    _add_env(parser, for_write=True)
    _add_space(parser)
    parser.add_argument("subject", help=f"one of: {', '.join(SUBJECTS)}")
    parser.add_argument("path", nargs="?", default=None, help="local image path")
    parser.add_argument(
        "space_id", nargs="?", default=None,
        help="project UUID (an alternative to --space)",
    )
    parser.add_argument("--apply", action="store_true", help="perform the upload (default: dry run)")
    parser.add_argument("--json", action="store_true", help="raw JSON instead of a report")


def build_standalone_parser() -> argparse.ArgumentParser:
    return _build_standalone_parser(add_parser)


def _subject_error(subject: object) -> ProjectsError:
    return ProjectsError(
        f"unknown subcommand {subject!r}. Valid: {', '.join(SUBJECTS)}."
    )


def run(args) -> int:
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in SUBJECTS:
        raise _subject_error(getattr(args, "subject", None))

    # ⚠️ OPTIONAL and merged BEFORE the resolver runs — formist_cli.context.resolve
    # only ever reads args.space, so the alternative positional must land there
    # first, the same order `evtool/cli.py::cmd_connectors` uses for its own
    # `space_id` positional.
    if getattr(args, "space_id", None) and not getattr(args, "space", None):
        args.space = args.space_id

    if not args.path:
        raise ProjectsError(
            "`fm project set-logo` needs a local image path.\n"
            "→ fm project set-logo <path> --space <id> [--apply]"
        )
    image_path = Path(args.path)
    if not image_path.is_file():
        raise ProjectsError(f"no such file: {image_path}")

    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)

    if not args.apply:
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would upload {image_path} ({image_path.stat().st_size} bytes) as the "
            f"logo for project {ctx.space} ({ctx.target.name}).\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    data = image_path.read_bytes()
    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    payload = api.post_multipart(
        ctx.target,
        f"/projects/{ctx.space}/logo",
        fields={},
        files={"file": (image_path.name, data)},
        token=ctx.token,
    )
    if not isinstance(payload, dict):
        raise ProjectsError(
            f"expected a JSON object from /projects/{{id}}/logo, got {type(payload).__name__}."
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"uploaded {len(data)} bytes ({content_type})")
        print(f"logo -> {payload.get('logo')}")
    return 0
