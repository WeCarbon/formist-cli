"""``fm project set-logo``: upload a project's logo image (write).

Uploads a local image via a plain multipart ``POST /projects/{id}/logo``.
The server sniffs the magic bytes (png/jpeg/gif/webp) rather than
trusting the declared Content-Type, so an SVG cannot be uploaded as an
image.

Idempotent (re-running overwrites the stored URL), but still a write:
dry run by default, ``--apply`` performs the upload. There is no
``--url`` mode to reuse an existing blob; the server always uploads
the bytes it is given.
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


def run(args, *, label: str = "fm project set-logo") -> int:
    """``label`` names this call in its own error/usage messages: also reached
    from ``fm spaces set-logo`` (:func:`formist_cli.spaces._run_set_logo`),
    which passes its own name rather than this deprecated alias's.
    """
    subject = (getattr(args, "subject", None) or "").strip().lower()
    if subject not in SUBJECTS:
        raise _subject_error(getattr(args, "subject", None))

    # The space_id positional is optional and merges before context.resolve
    # runs, since resolve only reads args.space.
    if getattr(args, "space_id", None) and not getattr(args, "space", None):
        args.space = args.space_id

    if not args.path:
        raise ProjectsError(
            f"`{label}` needs a local image path.\n"
            f"Usage: {label} <path> --space <id> [--apply]"
        )
    image_path = Path(args.path)
    if not image_path.is_file():
        raise ProjectsError(f"no such file: {image_path}")

    ctx = context.resolve(args, for_write=True, need_space=True, need_token=True)

    if not args.apply:
        print(
            f"DRY RUN: nothing was changed.\n"
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
