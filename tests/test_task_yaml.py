"""The minimal YAML subset (``task_yaml``): dump/load round trips and the
scalar-quoting rules `nod`'s real output was used to derive (see
``task_yaml``'s module docstring)."""

from __future__ import annotations

import pytest

from formist_cli import task_yaml


@pytest.mark.parametrize(
    "value,quoted",
    [
        ("true", True), ("false", True), ("null", True), ("yes", False),
        ("123abc", False), ("a:b", False), ("a: b", True), ("O'Brien", False),
        ("@at", True), ("#hash", True), ("  spaced  ", True), ("", True),
        ("Title with colon: and more", True), ("merged-into:W15", False),
        ("plain text", False),
    ],
)
def test_needs_quoting_matches_real_nod_output(value, quoted):
    assert task_yaml.needs_quoting(value) is quoted


def test_scalar_repr_types():
    assert task_yaml.scalar_repr(True) == "true"
    assert task_yaml.scalar_repr(False) == "false"
    assert task_yaml.scalar_repr(None) == "null"
    assert task_yaml.scalar_repr(1) == "1"
    assert task_yaml.scalar_repr("1") == '"1"'
    assert task_yaml.scalar_repr("plain") == "plain"


def test_dump_flat_mapping():
    out = task_yaml.dump({"id": 2, "title": "Hello", "done": True})
    assert out == "id: 2\ntitle: Hello\ndone: true\n"


def test_dump_sequence_of_scalars():
    out = task_yaml.dump({"tags": ["alpha", "beta"]})
    assert out == "tags:\n  - alpha\n  - beta\n"


def test_dump_nested_mapping():
    out = task_yaml.dump({"answer": {"choice": "1", "by": "owner", "at": "2026-01-01T00:00:00+00:00"}})
    assert out == 'answer:\n  choice: "1"\n  by: owner\n  at: 2026-01-01T00:00:00+00:00\n'


def test_dump_sequence_of_mappings():
    out = task_yaml.dump({"steps": [{"label": "a", "done": True}, {"label": "b", "done": False}]})
    assert out == "steps:\n  - label: a\n    done: true\n  - label: b\n    done: false\n"


def test_dump_nested_sequence_inside_sequence_of_mappings():
    out = task_yaml.dump({"approvals": [{"target": "x", "approve_on": ["1", "3"]}]})
    assert out == 'approvals:\n  - target: x\n    approve_on:\n      - "1"\n      - "3"\n'


@pytest.mark.parametrize(
    "mapping",
    [
        {"id": 1, "title": "Plain task", "author": "a · b", "status": "not_started", "created": "2026-01-01T00:00:00+08:00"},
        {"tags": ["a", "b", "c"], "after": ["1", "2"]},
        {"answer": {"choice": "2", "note": "free text", "by": "owner", "at": "2026-01-01T00:00:00.500+08:00"}},
        {"steps": [{"label": "one", "done": True}, {"label": "two", "done": False}]},
        {"question_defaults": {"Q1": "A", "Q2": "B"}},
        {
            "approvals": [
                {"target": "../attachments/1/a.png", "hash": "sha256:abc", "approved_hash": "sha256:abc",
                 "approved_by": "owner", "approve_on": ["1"]},
            ],
        },
    ],
)
def test_dump_then_load_round_trips(mapping):
    dumped = task_yaml.dump(mapping)
    loaded = task_yaml.load(dumped)
    assert loaded == mapping


def test_load_rejects_malformed_front_matter():
    with pytest.raises(task_yaml.YamlError):
        task_yaml.load("not a key line at all\n")
