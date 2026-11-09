"""``nod attach`` / ``nod approved`` — a port of noddit's ``Store.attach``
et al. (``core/store.ts``).

Simplified relative to the reference implementation (documented gaps,
tracked under C1052): every attachment lands in the body's own ``## 附件``
section rather than resolving ``--to``/"the latest open round" (the
``resolveAttachAnchor`` precedence in the research spec §5.3.1); file
history on ``--replace`` is not ported.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil

from formist_cli.task_format import HEADING
from formist_cli.task_model import Attachment, Node
from formist_cli.task_paths import Layout, attachment_dir, to_posix_path
from formist_cli.task_serialize import (
    is_image_path,
    render_attachments_block,
    upsert_section_scoped,
)

_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


class AttachError(ValueError):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}-{n}{ext}"):
        n += 1
    return f"{base}-{n}{ext}"


def attach_sources(
    layout: Layout, node: Node, sources: list[str], *, link: bool = False, name: str | None = None,
    caption: str | None = None, group: str | None = None,
) -> tuple[str, list[Attachment]]:
    new_atts: list[Attachment] = []
    for src in sources:
        is_url = bool(_URL_RE.match(src))
        if is_url:
            target = src
            label = name or src
        elif link:
            target = os.path.abspath(src)
            label = name or os.path.basename(src)
        else:
            if not os.path.isfile(src):
                raise AttachError(f"no such file: {src}")
            dest_dir = attachment_dir(layout, node.id)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = _unique_path(os.path.join(dest_dir, os.path.basename(src)))
            shutil.copyfile(src, dest_path)
            target = to_posix_path(os.path.relpath(dest_path, os.path.dirname(node.file)))
            label = name or os.path.basename(dest_path)
        new_atts.append(Attachment(label=label, target=target, image=is_image_path(target), group=group, caption=caption))

    all_body = [*node.body_attachments, *new_atts]
    stop_before = node.notes[0].heading if node.notes else None
    new_body = upsert_section_scoped(node.body, None, stop_before, HEADING["attachments"], render_attachments_block(all_body))
    return new_body, new_atts


def default_approve_on(node: Node) -> list[str]:
    if len(node.options) == 1 and not node.questions:
        return [node.options[0].key]
    raise AttachError(
        "which answers count as approving this content? pass --approve-on "
        "(e.g. --approve-on 1,3, or Q2:1 for a sub-question)"
    )


def check_approve_on(node: Node, keys: list[str]) -> None:
    for key in keys:
        if ":" in key:
            qk, ok = key.split(":", 1)
            q = next((x for x in node.questions if x.key == qk), None)
            if not q:
                raise AttachError(f"no such sub-question: {qk}")
            if not any(o.key == ok for o in q.options):
                raise AttachError(f"{qk} has no option {ok!r}")
        else:
            if not any(o.key == key for o in node.options):
                raise AttachError(f"no such option: {key}")
