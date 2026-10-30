"""``fm upload`` / ``fm skill-attach`` — files into a space, and skill attachments.

``ev`` had no way to put bytes into a space at all — every other command reads.
This is that capability, built as two composable pieces because they answer two
different questions:

  * **`fm upload`** puts a local file into a project. It is a thin client over
    the RESUMABLE upload route (``routes/upload_sessions.py``), never the
    one-shot ``POST /files/upload`` — a 12.9 MB skill template is exactly the
    case that route's own docstring says the one-shot path is wrong for ("fine
    until the connection drops at 90%, at which point the user starts over from
    zero"), and the chunk PUT is a raw byte body, which is also the one shape
    ``urllib`` can send without ``ev`` growing a multipart dependency it is not
    allowed to have.

  * **`fm skill-attach`** registers an uploaded file on a skill's
    ``Skill.files`` — the mechanism ``services/skill_attachments.py`` reads at
    runtime, and the ONLY one that survives workspace export/import. Uploading a
    file and attaching it are genuinely different acts (a File can exist without
    being any skill's attachment), so this command does both, idempotently: it
    checks whether the skill already resolves an attachment by this name — using
    the EXACT match rule ``services/skill_attachments.py::_same_file`` uses at
    request time — before doing anything, and uploads nothing when it already
    does.

Both commands write, so both take ``--env`` with no default (a write must never
inherit an environment — see ``evtool/cli.py``'s module docstring) and both are
DRY RUN unless ``--apply`` is given, matching ``fm update``'s convention: the
one command in this tool that mutates production data prints its plan first and
requires a second, explicit flag to act on it.

Stdlib only — ``ev`` must add no runtime dependency (the production image builds
``uv sync --frozen --no-dev``).
"""

from __future__ import annotations

import mimetypes
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from formist_cli.targets import Target

# --- filename normalisation, kept in EXACT parity with the server -----------
#
# `create_upload` (services/file_lifecycle.py) stores a File's `name` through
# `safe_filename`, and `find_skill_attachment_bytes` (services/skill_attachments.py
# `_same_file`) matches an attachment by running BOTH sides through it before
# comparing case-insensitively. `ev` cannot import either: this is a stdlib-only
# client and the server module pulls in the whole SQLModel/FastAPI dependency
# tree. So this is a second, independent copy — and
# `tests/test_evtool_uploads.py::test_client_and_server_normalise_identically`
# pins it against the real function so the two cannot drift silently the way
# the project rules records `normalize_skill_name`'s rule once did (re-typed in two
# places, a function in neither).
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_DISPLAY_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ufeff]")
MAX_FILE_NAME_CHARS = 128


def safe_filename(name: str | None) -> str:
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "file"
    return cleaned[:128]


def display_filename(name: str | None) -> str:
    """Same rule as ``services/file_lifecycle.py::display_filename``."""
    base = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _DISPLAY_FORBIDDEN.sub("", base).strip().strip(".").strip()
    if not base:
        return "file"
    if len(base) > MAX_FILE_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        if dot and stem and 0 < len(ext) <= 16:
            base = f"{stem[: MAX_FILE_NAME_CHARS - len(ext) - 1].rstrip()}.{ext}"
        else:
            base = base[:MAX_FILE_NAME_CHARS].rstrip()
    return base


def same_filename(candidate: str | None, wanted: str) -> bool:
    """Same rule as ``services/skill_attachments.py::_same_file``."""
    if not candidate:
        return False
    if display_filename(candidate).casefold() == display_filename(wanted).casefold():
        return True
    legacy = safe_filename(wanted)
    return candidate == safe_filename(candidate) and "." in legacy and legacy.casefold() == candidate.casefold()


class UploadError(Exception):
    """A file/attachment operation that could not proceed. Operator-facing."""


Caller = Callable[..., object]

DEFAULT_FOLDER_PATH = "/"

#: Chunk PUTs get a few attempts before giving up — the store_chunk route
#: (services/upload_sessions.py) is idempotent per index, so a blind retry after
#: a dropped connection is safe rather than merely convenient.
DEFAULT_CHUNK_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.5


def _api():
    # Imported lazily, same reasoning as formist_cli.resolve._default_fetch: this
    # module stays importable — and its pure functions unit-testable with
    # injected transports — without pulling in the HTTP layer.
    from formist_cli import api

    return api


def _maybe_uuid(value: object) -> str | None:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(value)


def _guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _upload_prefix(space: str) -> str:
    return f"/projects/{space}/files/upload"


# --- the resumable-session HTTP calls, one function per verb ----------------


def open_session(
    target: Target,
    space: str,
    *,
    token: str | None,
    filename: str,
    content_type: str,
    total_size: int,
    folder_path: str = DEFAULT_FOLDER_PATH,
    owner: str | None = None,
    task_id: str | None = None,
    chunk_size: int | None = None,
    relative_path: str | None = None,
    post: Caller | None = None,
) -> dict:
    post = post or _api().post
    body = {
        "filename": filename,
        "content_type": content_type,
        "total_size": total_size,
        "folder_path": folder_path,
        "owner": owner,
        "task_id": task_id,
        "chunk_size": chunk_size,
        # A folder drop's path INSIDE the dropped folder (`OpenUploadSession.
        # relative_path`, routes/upload_sessions.py) — the SAME field a browser
        # folder-drop sends (`webkitRelativePath`). The SERVER derives the
        # destination folder from this (`effective_folder`,
        # services/upload_sessions.py), so `fm upload DIR` never re-implements
        # that join itself — it just states each file's path inside the
        # directory and lets the server land it identically to the product's
        # own folder-drop dialog.
        "relative_path": relative_path,
    }
    result = post(target, f"{_upload_prefix(space)}/sessions", body, token=token)
    if not isinstance(result, dict) or not result.get("upload_id"):
        raise UploadError(f"open session returned an unexpected shape: {result!r}")
    return result


def session_state(target: Target, space: str, upload_id: str, *, token: str | None, get: Caller | None = None) -> dict:
    get = get or _api().get
    result = get(target, f"{_upload_prefix(space)}/sessions/{upload_id}", token=token)
    if not isinstance(result, dict):
        raise UploadError(f"session state returned an unexpected shape: {result!r}")
    return result


def put_chunk(
    target: Target,
    space: str,
    upload_id: str,
    index: int,
    data: bytes,
    *,
    token: str | None,
    put: Caller | None = None,
) -> dict:
    put = put or _api().put_bytes
    path = f"{_upload_prefix(space)}/sessions/{upload_id}/chunks/{index}"
    result = put(target, path, data, token=token)
    if not isinstance(result, dict):
        raise UploadError(f"chunk {index} PUT returned an unexpected shape: {result!r}")
    return result


def _put_chunk_with_retry(
    target: Target,
    space: str,
    upload_id: str,
    index: int,
    data: bytes,
    *,
    token: str | None,
    put: Caller | None,
    retries: int,
) -> dict:
    """Retry a chunk PUT on a transport failure. NOT on a 4xx — that is not transient.

    ``store_chunk`` re-checks the exact byte length every time (``expected_chunk_size``),
    so re-sending the SAME index after a dropped connection is a no-op if it already
    landed and a normal write if it did not — never a duplicate.
    """
    api = _api()
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return put_chunk(target, space, upload_id, index, data, token=token, put=put)
        except api.ApiError as exc:
            # A real refusal (bad chunk size, expired session, auth) will not
            # change on retry — only a transport-layer failure might.
            if getattr(exc, "status", None) is not None:
                raise
            last = exc
            if attempt + 1 < retries:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise UploadError(f"chunk {index} failed after {retries} attempts: {last}") from last


def complete_session(
    target: Target, space: str, upload_id: str, *, token: str | None, post: Caller | None = None
) -> dict:
    post = post or _api().post
    path = f"{_upload_prefix(space)}/sessions/{upload_id}/complete"
    result = post(target, path, {}, token=token)
    if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
        raise UploadError(f"complete returned an unexpected shape: {result!r}")
    return result


def abort_session(
    target: Target, space: str, upload_id: str, *, token: str | None, delete: Caller | None = None
) -> None:
    delete = delete or _api().delete
    delete(target, f"{_upload_prefix(space)}/sessions/{upload_id}", token=token)


# --- the orchestration: local file -> a completed File row ------------------


def upload_file(
    target: Target,
    space: str,
    file_path: Path,
    *,
    token: str | None,
    folder_path: str = DEFAULT_FOLDER_PATH,
    owner: str | None = None,
    task_id: str | None = None,
    content_type: str | None = None,
    chunk_retries: int = DEFAULT_CHUNK_RETRIES,
    relative_path: str | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    post: Caller | None = None,
    put: Caller | None = None,
) -> dict:
    """Upload ``file_path`` via the resumable-session route. Returns the completion
    response — ``{"item": <File dict>, "audit_id": ...}`` — SHAPE-IDENTICAL to the
    one-shot route's, per ``routes/upload_sessions.py``'s own docstring.

    Refuses a 0-byte source before opening a network connection: a test — or a
    caller — that uploads zero bytes and gets back a 200 is a real failure mode
    this codebase has produced (the project rules), and the cheapest place to catch it is
    before any bytes move. The size that actually landed is checked again against
    the completion response at the end, for the same reason: never trust what was
    sent, read back what arrived.

    ``relative_path`` is this file's path inside a dropped DIRECTORY (see
    :func:`open_session`) — ``fm upload DIR`` is the caller that passes it, one
    file at a time, through this exact same per-file path.
    """
    if not file_path.is_file():
        raise UploadError(f"no such file: {file_path}")
    total_size = file_path.stat().st_size
    if total_size <= 0:
        raise UploadError(f"{file_path} is empty (0 bytes) — refusing to upload it")

    filename = file_path.name
    opened = open_session(
        target,
        space,
        token=token,
        filename=filename,
        content_type=content_type or _guess_content_type(filename),
        total_size=total_size,
        folder_path=folder_path,
        owner=owner,
        task_id=task_id,
        relative_path=relative_path,
        post=post,
    )
    upload_id = opened["upload_id"]
    chunk_size = opened["chunk_size"]
    chunk_count = opened["chunk_count"]
    already_received = set(opened.get("received_chunks") or [])

    with file_path.open("rb") as fh:
        for index in range(chunk_count):
            start = index * chunk_size
            length = min(chunk_size, total_size - start)
            if index in already_received:
                if on_chunk:
                    on_chunk(index, chunk_count)
                continue
            fh.seek(start)
            data = fh.read(length)
            if len(data) != length:
                raise UploadError(
                    f"read {len(data)} bytes for chunk {index}, expected {length} — "
                    f"{file_path} changed size while it was being uploaded"
                )
            _put_chunk_with_retry(
                target,
                space,
                upload_id,
                index,
                data,
                token=token,
                put=put,
                retries=chunk_retries,
            )
            if on_chunk:
                on_chunk(index, chunk_count)

    completed = complete_session(target, space, upload_id, token=token, post=post)
    item = completed["item"]
    if item.get("size") != total_size:
        raise UploadError(
            f"uploaded, but the server's recorded size ({item.get('size')}) does not "
            f"match the local file ({total_size} bytes) — do not trust this File row "
            f"(id={item.get('id')})"
        )
    return completed


# --- skill attachment: idempotent lookup + register --------------------------


def find_existing_attachment(
    target: Target,
    space: str,
    skill: dict,
    filename: str,
    *,
    token: str | None,
    get: Caller | None = None,
) -> dict | None:
    """A File already registered on ``skill`` whose name matches ``filename``.

    Mirrors ``services/skill_attachments.py::find_skill_attachment_bytes`` exactly:
    a non-UUID ``files`` entry is a typed label, never an attachment (see that
    module's docstring), so it is skipped rather than compared; a UUID entry that
    no longer resolves to a file in this project is reported and skipped rather
    than treated as a match or a fatal error, for the same reason the server-side
    resolver treats it as absent and falls through. The name comparison is the
    SAME rule the runtime resolver uses (:func:`same_filename`), so "already
    attached" here means the exact thing the server would resolve at request
    time — not a separately invented guess at the question.
    """
    get = get or _api().get
    for entry in skill.get("files") or []:
        file_id = _maybe_uuid(entry)
        if file_id is None:
            continue
        try:
            file = get(target, f"/projects/{space}/files/{file_id}", token=token)
        except Exception as exc:  # 404 / network — treat as unresolvable, not fatal
            print(
                f"  (skill {skill.get('id')} references file {file_id}, which could not be read: {exc})",
                file=sys.stderr,
            )
            continue
        if isinstance(file, dict) and same_filename(file.get("name"), filename):
            return file
    return None


def verify_file_size(
    target: Target,
    space: str,
    file_id: str,
    expected_size: int,
    *,
    token: str | None,
    get: Caller | None = None,
) -> dict:
    """Independent proof that ``file_id`` really holds ``expected_size`` bytes.

    A FRESH GET, never a value this command already believes — not the
    completion response just returned by the upload, not a size printed a
    moment ago. "A test that uploads zero bytes and passes" (the project rules) is a
    real failure mode this codebase has produced; the fix is to read back
    what actually landed rather than trust what was sent, and to do it against
    the SAME row a caller is about to report as good.
    """
    get = get or _api().get
    file = get(target, f"/projects/{space}/files/{file_id}", token=token)
    if not isinstance(file, dict) or not file.get("id"):
        raise UploadError(f"could not read back file {file_id} — refusing to report success")
    if file.get("size") != expected_size:
        raise UploadError(
            f"file {file_id} ({file.get('name')}) read back with size="
            f"{file.get('size')}, expected {expected_size} bytes. Do not trust "
            f"this attachment — this is exactly the 'uploaded zero bytes and "
            f"passed' failure class, read back and caught rather than assumed."
        )
    return file


def attach_file_to_skill(
    target: Target,
    space: str,
    skill: dict,
    file_id: str,
    *,
    token: str | None,
    patch: Caller | None = None,
) -> dict:
    """PATCH the skill's ``files`` to include ``file_id``, preserving every entry
    already there.

    ``files`` is a plain list field (``routes/skills.py::SkillUpdate``) — PATCH
    REPLACES whatever is sent, per ``routes/resource.py``'s ``exclude_unset``
    contract (the field itself is not merged, only its ABSENCE is). Sending a bare
    ``[file_id]`` would silently drop every attachment the skill already had. So
    this always starts from ``skill["files"]`` — read by the CALLER immediately
    beforehand, never a value cached from an earlier step — and appends.
    """
    patch = patch or _api().patch
    current = [str(f) for f in (skill.get("files") or [])]
    if file_id in current:
        # The id itself is already registered — not merely a same-named file.
        # Re-sending an unchanged list would be a no-op PATCH; skip the write
        # outright so a write that changes nothing does not look like one.
        return skill
    body = {"files": [*current, file_id]}
    result = patch(target, f"/projects/{space}/skills/{skill['id']}", body, token=token)
    item = result.get("item") if isinstance(result, dict) else None
    if not isinstance(item, dict):
        raise UploadError(f"attaching to the skill returned an unexpected shape: {result!r}")
    return item


def resolve_skill(
    name_or_id: str,
    *,
    target: Target,
    space: str,
    token: str | None,
    fetch: Caller | None = None,
) -> dict:
    """The skill's full row — addressed by id OR by name (exactly-one-or-refuse,
    via ``formist_cli.resolve``, the SAME resolver ``fm get SKILL`` uses)."""
    from formist_cli import resolve as resolve_mod

    fetch = fetch or _api().get
    file_id = _maybe_uuid(name_or_id)
    skill_id = file_id
    if skill_id is None:
        match = resolve_mod.resolve_one(name_or_id, kind="SKILL", target=target, space=space, token=token, fetch=fetch)
        skill_id = match.id
    item = fetch(target, f"/projects/{space}/skills/{skill_id}", token=token)
    if not isinstance(item, dict) or not item.get("id"):
        raise UploadError(f"GET skill {skill_id} returned an unexpected shape: {item!r}")
    return item


# --- `fm upload DIR` — every file under a directory, one resumable upload each ---
#
# Noise a directory drop should never treat as content. `__pycache__` is skipped
# as a whole DIRECTORY (its contents are Python build artifacts, never source
# material an operator meant to upload); the other two are per-file OS litter.
_SKIPPED_UPLOAD_NAMES = frozenset({".DS_Store", "Thumbs.db", "__pycache__"})

#: How many paths a directory dry run lists before summarising the rest — enough
#: to sanity-check a batch without a 168-file drop scrolling the terminal.
DIR_DRY_RUN_PREVIEW = 20

#: D626 (open, 2026-09-25): the server refuses a 0-byte upload with 400
#: `empty-upload` (`routes/uploads.py`), and `upload_file` already refuses one
#: locally before opening a socket (see its docstring). A directory drop must
#: not silently drop those rows from the count OR invent placeholder content to
#: get them past the refusal — the decision explicitly leaves that choice to a
#: human. This is just the label a directory upload's failure list uses so the
#: reason is legible without chasing the exception text.
EMPTY_FILE_REASON = "empty file (server refuses 0-byte uploads; see decs D626)"


def walk_upload_dir(root: Path) -> list[Path]:
    """Every uploadable file under ``root``, recursively, in a STABLE order —
    sorted, so a dry run and the ``--apply`` run right after it describe (and
    number) the exact same sequence.

    Skips :data:`_SKIPPED_UPLOAD_NAMES` — a bare filename match for the two
    per-file entries, and any ``__pycache__`` DIRECTORY pruned entirely rather
    than merely having its own name checked (its contents are never source
    material).
    """
    if not root.is_dir():
        raise UploadError(f"no such directory: {root}")
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in _SKIPPED_UPLOAD_NAMES:
            continue
        if _SKIPPED_UPLOAD_NAMES.intersection(path.relative_to(root).parts[:-1]):
            continue
        out.append(path)
    return out


def relative_upload_path(root: Path, file_path: Path) -> str:
    """``file_path``'s path inside ``root``, forward-slash — the SAME shape a
    browser folder-drop's ``webkitRelativePath`` sends
    (``routes/upload_sessions.py::normalize_relative_path``), so the server (not
    this client) computes the destination folder and a directory upload lands
    subfolders exactly where the product's own folder-drop dialog would.
    """
    return file_path.relative_to(root).as_posix()


def cmd_upload_dir(args) -> int:
    """``fm upload DIR`` — every file under DIR, each through the same
    single-file resumable path :func:`cmd_upload` uses, via ``relative_path``
    (see :func:`open_session`) rather than a client-side re-derivation of
    ``--folder-path`` per file.

    Dry run lists what would be uploaded; ``--apply`` uploads every file,
    NEVER stopping at the first failure — a bulk drop that aborted halfway
    would leave the operator unable to tell what already landed from what did
    not, so every row is attempted and the summary at the end says which
    failed and why. Exit code is non-zero iff at least one file failed.
    """
    import json as json_mod

    from formist_cli.cli import _effective, _token_for

    eff = _effective(args, for_write=True)
    token = _token_for(eff)
    root = Path(args.file)
    files = walk_upload_dir(root)
    total_bytes = sum(f.stat().st_size for f in files)

    if not args.apply:
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would upload {len(files)} file(s), {total_bytes} bytes total, from "
            f"{root} to space {eff.space} ({eff.target.name}), "
            f"folder_path={args.folder_path!r}.",
            file=sys.stderr,
        )
        for f in files[:DIR_DRY_RUN_PREVIEW]:
            print(f"  {relative_upload_path(root, f)}  ({f.stat().st_size} bytes)", file=sys.stderr)
        remaining = len(files) - DIR_DRY_RUN_PREVIEW
        if remaining > 0:
            print(f"  ... and {remaining} more", file=sys.stderr)
        print("To apply: re-run with --apply.", file=sys.stderr)
        return 0

    from formist_cli import api as api_mod

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for f in files:
        rel = relative_upload_path(root, f)
        if f.stat().st_size == 0:
            print(f"FAILED  {rel}  {EMPTY_FILE_REASON}", file=sys.stderr)
            failed.append((rel, EMPTY_FILE_REASON))
            continue
        try:
            completed = upload_file(
                eff.target,
                eff.space,
                f,
                token=token,
                folder_path=args.folder_path,
                owner=args.owner,
                task_id=args.task_id,
                content_type=args.content_type,
                relative_path=rel,
            )
        except (UploadError, api_mod.ApiError) as exc:
            print(f"FAILED  {rel}  {exc}", file=sys.stderr)
            failed.append((rel, str(exc)))
            continue
        item = completed["item"]
        print(f"uploaded  {rel}  id={item['id']}  size={item['size']}")
        succeeded.append(rel)

    if args.json:
        print(
            json_mod.dumps(
                {
                    "root": str(root),
                    "uploaded": len(succeeded),
                    "failed": [{"path": rel, "reason": reason} for rel, reason in failed],
                    "total": len(files),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    print(
        f"\n{len(succeeded)} uploaded, {len(failed)} failed, {len(files)} total",
        file=sys.stderr,
    )
    if failed:
        print("failed:", file=sys.stderr)
        for rel, reason in failed:
            print(f"  {rel}: {reason}", file=sys.stderr)
        return 1
    return 0


# ===========================================================================
# The CLI surface. Per-module `add_parser` is the convention (team-lead ruling,
# 2026-08-27, see evtool/chats.py) — one line in cli.py per module, not a block.
# ===========================================================================


def add_parser(sub) -> None:
    import argparse

    from formist_cli.cli import _add_env, _add_space

    p = sub.add_parser(
        "upload",
        help="upload a local file, or every file under a directory, into a space (resumable; WRITE)",
        description=(
            "Uploads FILE via the resumable-session route (open -> PUT chunks -> "
            "complete), never the one-shot POST /files/upload — that route reads "
            "the whole body in one request, so a dropped connection on a large "
            "file starts over from zero. This is a bare upload primitive: it does "
            "not check whether a same-named file already exists (the product UI "
            "upload button does not either). For registering a file as a skill's "
            "template attachment, with that idempotence check, use `fm "
            "skill-attach` instead.\n\n"
            "When FILE is a DIRECTORY, every file under it is uploaded recursively "
            "through this SAME per-file path, preserving subfolders under "
            "--folder-path via `relative_path` (routes/upload_sessions.py) — the "
            "identical field a browser folder-drop sends, so the server lands the "
            "structure exactly as the product's own dialog would. "
            f"{', '.join(sorted(_SKIPPED_UPLOAD_NAMES))} are skipped. A 0-byte file "
            "is refused by the server (400 empty-upload, D626) and is reported in "
            "the failure list rather than silently dropped or padded with invented "
            "content. One file failing does not stop the rest; the exit code is "
            "non-zero iff at least one failed.\n\n"
            "DRY RUN by default: prints what would be sent and touches nothing. "
            "--apply performs the upload(s)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("file", help="path to a local file, or a directory to upload recursively")
    p.add_argument("--folder-path", default=DEFAULT_FOLDER_PATH)
    p.add_argument("--owner", default=None, help="free-text team label")
    p.add_argument("--task-id", default=None, help="attach the upload to a task (UUID)")
    p.add_argument("--content-type", default=None, help="override the guessed MIME type (single file only)")
    p.add_argument("--apply", action="store_true", help="perform the upload (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser(
        "skill-attach",
        help="attach a local file to a skill as its template attachment (WRITE)",
        description=(
            "Registers FILE on SKILL's Skill.files — the mechanism "
            "services/skill_attachments.py reads at runtime and the ONLY one that "
            "survives workspace export/import.\n\n"
            "IDEMPOTENT: before doing anything, this reads the skill's CURRENT "
            "attachments and checks each one against FILE's name using the exact "
            "match rule the runtime resolver uses (safe_filename + casefold). If "
            "the skill already resolves an attachment by this name, nothing is "
            "uploaded and nothing is changed — the command says so and exits 0.\n\n"
            "SKILL is a skill id (UUID) or its exact name (resolved the same way "
            "`fm get SKILL --name ...` does; refuses on more than one match).\n\n"
            "DRY RUN by default: prints the plan (the skill's current attachments, "
            "the file that would be uploaded, and the resulting files list) and "
            "touches nothing. --apply performs the upload and the attach.\n\n"
            "--file-id skips the upload and attaches an ALREADY-uploaded file "
            "directly — the recovery path if a previous --apply uploaded "
            "successfully but the attach step then failed (its file id is in that "
            "run's output); this avoids uploading a second copy on retry."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_env(p, for_write=True)
    _add_space(p)
    p.add_argument("skill", help="skill id (UUID) or exact name")
    p.add_argument("file", help="path to a local file")
    p.add_argument("--folder-path", default=DEFAULT_FOLDER_PATH)
    p.add_argument("--owner", default=None, help="free-text team label")
    p.add_argument(
        "--file-id",
        default=None,
        help="attach this already-uploaded file id instead of uploading FILE again",
    )
    p.add_argument("--apply", action="store_true", help="perform the write (default: dry run)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _print_progress(index: int, count: int) -> None:
    print(f"  chunk {index + 1}/{count}", file=sys.stderr, end="\r")
    if index + 1 == count:
        print(file=sys.stderr)


def cmd_upload(args) -> int:
    import json as json_mod

    from formist_cli.cli import _effective, _token_for

    if Path(args.file).is_dir():
        return cmd_upload_dir(args)

    eff = _effective(args, for_write=True)
    token = _token_for(eff)
    file_path = Path(args.file)

    if not file_path.is_file():
        print(f"error: no such file: {file_path}", file=sys.stderr)
        return 2
    size = file_path.stat().st_size
    content_type = args.content_type or _guess_content_type(file_path.name)

    if not args.apply:
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Would upload {file_path} ({size} bytes, {content_type}) as "
            f"{file_path.name!r} to space {eff.space} ({eff.target.name}), "
            f"folder_path={args.folder_path!r} owner={args.owner!r}"
            f"{' task_id=' + args.task_id if args.task_id else ''}.\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    completed = upload_file(
        eff.target,
        eff.space,
        file_path,
        token=token,
        folder_path=args.folder_path,
        owner=args.owner,
        task_id=args.task_id,
        content_type=args.content_type,
        on_chunk=_print_progress,
    )
    item = completed["item"]
    if args.json:
        print(json_mod.dumps(completed, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"uploaded: id={item['id']}  name={item['name']}  size={item['size']}")
        print(f"  web_url={item.get('web_url')}")
        print(f"  audit_id={completed.get('audit_id')}")
    return 0


def cmd_skill_attach(args) -> int:
    import json as json_mod

    from formist_cli.cli import _effective, _token_for

    eff = _effective(args, for_write=True)
    token = _token_for(eff)
    file_path = Path(args.file)
    filename = file_path.name

    skill = resolve_skill(args.skill, target=eff.target, space=eff.space, token=token)
    print(
        f"  skill: {skill['id']}  {skill.get('name')!r}  "
        f"({len(skill.get('files') or [])} existing attachment entr"
        f"{'y' if len(skill.get('files') or []) == 1 else 'ies'})",
        file=sys.stderr,
    )

    existing = find_existing_attachment(eff.target, eff.space, skill, filename, token=token)
    if existing is not None:
        print(
            f"already attached — nothing to do.\n"
            f"  file: id={existing['id']}  name={existing['name']}  "
            f"size={existing['size']}\n"
            f"  skill: id={skill['id']}  name={skill.get('name')!r}"
        )
        # A same-named attachment already exists, but "the name resolves" is not
        # "the bytes are right" — the runtime resolver (services/skill_attachments.py)
        # matches on name alone, so a truncated or stale attachment would pass this
        # command's own idempotence check forever. When the local source is present,
        # compare it — this does not block the operator (idempotence still means "do
        # nothing"), but a live tool's continuity depends on this attachment, so a
        # mismatch must be LOUD rather than silent.
        if file_path.is_file():
            local_size = file_path.stat().st_size
            if existing.get("size") != local_size:
                print(
                    f"⚠️  size mismatch: the already-attached file is {existing.get('size')} "
                    f"bytes; the local source at {file_path} is {local_size} bytes. The "
                    f"attachment was NOT touched (that is what 'already attached' means), "
                    f"but this is worth investigating before relying on it — the local "
                    f"file may have been updated since the last attach, or the earlier "
                    f"upload may be incomplete.",
                    file=sys.stderr,
                )
        return 0

    if not args.file_id and not file_path.is_file():
        print(f"error: no such file: {file_path}", file=sys.stderr)
        return 2

    current_files = list(skill.get("files") or [])
    if not args.apply:
        if args.file_id:
            plan = f"attach the already-uploaded file {args.file_id}"
        else:
            size = file_path.stat().st_size
            plan = f"upload {file_path} ({size} bytes) as {filename!r}, then attach it"
        print(
            f"DRY RUN — nothing was changed.\n"
            f"Skill {skill['id']} ({skill.get('name')!r}) has no attachment "
            f"matching {filename!r}.\n"
            f"Would {plan}.\n"
            f"  files: {len(current_files)} -> {len(current_files) + 1}\n"
            f"To apply: re-run with --apply.",
            file=sys.stderr,
        )
        return 0

    if args.file_id:
        file_dict = None
        completed = None
    else:
        completed = upload_file(
            eff.target,
            eff.space,
            file_path,
            token=token,
            folder_path=args.folder_path,
            owner=args.owner,
            on_chunk=_print_progress,
        )
        file_dict = completed["item"]

    api = _api()
    file_id = args.file_id or file_dict["id"]

    # Independent read-back BEFORE attaching — a FRESH GET, not the completion
    # response `upload_file` already validated internally, and not the local
    # `stat()` this command already trusts. This is the check the owner asked
    # for explicitly for the live-in-prod agenda tool: "read back what you
    # wrote and prove the bytes... against prod after the apply, not only in a
    # test." Catching a bad upload HERE means a truncated file is never wired
    # into the skill's resolution chain at all, even transiently.
    if file_path.is_file():
        verify_file_size(eff.target, eff.space, file_id, file_path.stat().st_size, token=token)
    else:
        readback_file = api.get(eff.target, f"/projects/{eff.space}/files/{file_id}", token=token)
        if not isinstance(readback_file, dict) or not readback_file.get("id"):
            raise UploadError(f"could not read back file {file_id} — refusing to attach it")
        print(
            f"  (no local source at {file_path} to verify size against; file "
            f"reads back as {readback_file.get('size')} bytes, unverified)",
            file=sys.stderr,
        )

    try:
        attach_file_to_skill(eff.target, eff.space, skill, file_id, token=token)
    except Exception as exc:
        if completed is not None:
            print(
                f"error: uploaded successfully but attaching to the skill failed: "
                f"{exc}\n"
                f"The file exists and is NOT attached: id={file_id} "
                f"name={file_dict['name']} size={file_dict['size']}\n"
                f"→ Re-run with --file-id {file_id} to attach it without "
                f"uploading a second copy.",
                file=sys.stderr,
            )
        raise

    # Read back the skill INDEPENDENTLY of the PATCH response, from a fresh GET —
    # never the local variable this command is about to confirm, and never only
    # the mutation's own response body. See GATE.md §5's "a verification line must
    # read every value it prints from the thing it is verifying".
    readback = api.get(eff.target, f"/projects/{eff.space}/skills/{skill['id']}", token=token)
    readback_files = readback.get("files") if isinstance(readback, dict) else None

    result = {
        "skill": {"id": skill["id"], "name": skill.get("name")},
        "file_id": file_id,
        "files_before": current_files,
        "files_after": readback_files,
        "size_verified_by_fresh_read": file_path.is_file(),
    }
    if args.json:
        print(json_mod.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("attached.")
        print(
            "  size verified by an independent read-back before attaching."
            if file_path.is_file()
            else "  ⚠️ size NOT verified against a local source (see the read-back note above)."
        )
        print(f"  skill: id={skill['id']}  name={skill.get('name')!r}")
        print(
            f"  file:  id={file_id}"
            + (
                f"  name={file_dict['name']}  size={file_dict['size']}  web_url={file_dict.get('web_url')}"
                if file_dict
                else ""
            )
        )
        print(f"  files: {len(current_files)} -> {len(readback_files) if isinstance(readback_files, list) else '?'}")
        print(
            f"  to undo by hand: PATCH the skill's files back to {current_files!r}; "
            f"the uploaded File row (id={file_id}) can be deleted separately if "
            f"nothing else references it."
        )
    return 0
