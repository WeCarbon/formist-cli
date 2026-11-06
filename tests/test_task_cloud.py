"""Cloud mode for ``fm task``: request-shape and refusal tests.

No live server exists yet (C1048 hasn't shipped the ``/projects/{id}/nodes``
router) — these tests check what WOULD be sent, and that every verb
refuses clearly rather than guessing, per ``task_cloud``'s module
docstring.
"""

from __future__ import annotations

import argparse

import pytest

from formist_cli import task_cloud


def test_node_path_with_and_without_ref():
    assert task_cloud._node_path("space-1") == "/projects/space-1/nodes"
    assert task_cloud._node_path("space-1", "C12") == "/projects/space-1/nodes/C12"


def test_add_payload_shape():
    args = argparse.Namespace(
        title="Ship it", option=["1=Yes", "2=No"], default="1", task=False, fyi=False,
        parent="C10", tag=["ops"], due="2026-12-01", urgency="high",
    )
    payload = task_cloud._add_payload(args)
    assert payload["title"] == "Ship it"
    assert payload["options"] == [{"key": "1", "label": "Yes"}, {"key": "2", "label": "No"}]
    assert payload["default"] == "1"
    assert payload["parent"] == "C10"
    assert payload["tags"] == ["ops"]
    assert payload["due"] == "2026-12-01"
    assert payload["urgency"] == "high"
    assert "kind" not in payload


def test_add_payload_task_kind():
    args = argparse.Namespace(
        title="Do it", option=[], default=None, task=True, fyi=False, parent=None, tag=[], due=None, urgency=None,
    )
    assert task_cloud._add_payload(args)["kind"] == "task"


@pytest.mark.parametrize("verb,fn", [
    ("add", lambda t: task_cloud.add(t, argparse.Namespace())),
    ("get", lambda t: task_cloud.get(t, "C1")),
    ("show", lambda t: task_cloud.show(t, "available")),
])
def test_every_verb_refuses_clearly_until_the_server_exists(verb, fn):
    target = task_cloud.CloudTarget(host="formist")
    with pytest.raises(task_cloud.CloudNotAvailable) as exc:
        fn(target)
    assert verb in str(exc.value)
    assert "C1048" in str(exc.value)
