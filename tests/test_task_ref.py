"""Reference parser contracts."""

from formist_cli.task_ref import parse_ref


def test_source_qualified_ref_keeps_source_and_letter_separate():
    """C1125: source selects the namespace; a node's display letter is a
    hint and must not make `local:C12` unparseable."""
    assert parse_ref("local:C12").source == "local"
    assert parse_ref("local:C12").letter == "C"
    assert parse_ref("local:C12").id == 12


def test_legacy_single_letter_head_still_means_a_letter():
    parsed = parse_ref("W:12")
    assert parsed.source is None
    assert parsed.letter == "W"
