"""Cloud mode for ``fm task``: the client side of contract §4's API surface.

**Not wired to a live server.** The server-side router this calls
(``routes/task_nodes.py``, mounted at ``/projects/{id}/nodes``) is being
built on ``feat/noddit-tasks`` by a parallel lane (C1048) and does not
exist yet. This module is written against the contract's documented
shape so that once that router lands, wiring it up is a matter of
removing :func:`_not_yet` calls, not redesigning the client — the
request-building functions below (:func:`_node_path`, the payload
builders) are already correct against the contract and are exercised by
tests with a mocked transport (``tests/test_task_cloud.py``).

Mode resolution lives in ``tasks.py``; this module only builds and sends
requests once cloud mode has already been chosen.
"""

from __future__ import annotations

from dataclasses import dataclass


class CloudError(Exception):
    pass


class CloudNotAvailable(CloudError):
    def __init__(self, verb: str):
        super().__init__(
            f"`fm task {verb}` (cloud mode) is not available yet: the server's "
            "/projects/{id}/nodes router (C1048, feat/noddit-tasks) hasn't landed. "
            "Use --local for a decisions/ directory, or wait for C1048 to ship."
        )
        self.verb = verb


@dataclass
class CloudTarget:
    """What cloud mode needs once C1048's router exists: a resolved
    ``fm`` target/space/token, same shape ``context.resolve`` returns for
    every other ``fm`` command."""

    host: str | None = None
    space: str | None = None
    token: str | None = None


def _node_path(space: str, ref: str | None = None) -> str:
    """``GET/POST /projects/{id}/nodes[/{ref}]`` — contract §4's base route."""
    base = f"/projects/{space}/nodes"
    return f"{base}/{ref}" if ref else base


def _add_payload(args) -> dict:
    """The body ``POST /nodes`` takes for ``nod add`` / ``fm task add``
    (contract §4's "add" row)."""
    payload: dict = {"title": args.title}
    if getattr(args, "option", None):
        payload["options"] = [
            {"key": k.strip(), "label": v.strip()}
            for item in args.option for k, v in [item.split("=", 1)]
        ]
    if getattr(args, "default", None):
        payload["default"] = args.default
    if getattr(args, "task", False):
        payload["kind"] = "task"
    if getattr(args, "fyi", False):
        payload["kind"] = "fyi"
    if getattr(args, "parent", None):
        payload["parent"] = args.parent
    if getattr(args, "tag", None):
        payload["tags"] = args.tag
    if getattr(args, "due", None):
        payload["due"] = args.due
    if getattr(args, "urgency", None):
        payload["urgency"] = args.urgency
    return payload


def add(target: CloudTarget, args) -> None:
    raise CloudNotAvailable("add")


def get(target: CloudTarget, ref: str):
    raise CloudNotAvailable("get")


def show(target: CloudTarget, view: str):
    raise CloudNotAvailable("show")
