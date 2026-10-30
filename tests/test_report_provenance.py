"""The outcome report may only print what the server said; `fm` must not
rely on anyone remembering that rule by hand. A value in the outcome
report must be traceable to the response body: a payload value (the thing
that was about to be written, before the server confirmed anything) must
never appear there, even partially, because a reader who spots one
verified field stops checking whether the rest is verified too.

One deliberate exception: the "resolved by name" lines print the name
from the payload beside the id from the server. That line's whole claim is
"you asked for this name, the server returned this id": the payload half
is the question, not the answer. It is printed before the write, labelled
as a resolution, and never as an outcome. `_print_report` is what this
file constrains.
"""

from __future__ import annotations

import json

from formist_cli.cli import _print_report

#: Appears ONLY in the request. If it reaches the outcome report, the report is
#: echoing what it hoped to write.
PAYLOAD_SENTINEL = "VALUE-I-ASKED-FOR"
#: Appears ONLY in the response.
SERVER_SENTINEL = "VALUE-THE-SERVER-RETURNED"


def _server_report(outcome="applied", committed=True):
    """A response shaped like the endpoint's, carrying only server-side values."""
    return {
        "mode": "apply",
        "committed": committed,
        "counts": {"ops": 1, "ok": 1, "refused": 0, "applied": 1},
        "results": [{
            "index": 0, "action": "update", "kind": "STAKEHOLDER",
            "id": SERVER_SENTINEL, "audit_id": "aud-1",
            "cascaded": 3, "outcome": outcome,
        }],
        "refusals": [],
    }


class TestTheReportPrintsOnlyWhatTheServerSaid:
    def test_server_values_are_printed(self):
        """The report must actually carry the response through: a test that only
        checks for absence would pass on a function that prints nothing."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(_server_report(), applied=True, json_out=False)
        out = buf.getvalue()
        assert SERVER_SENTINEL in out
        assert "applied" in out

    def test_a_payload_value_never_reaches_the_report(self):
        """`_print_report` is given only the response, so a payload value
        cannot appear unless someone reintroduces the request into this
        path."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(_server_report(), applied=True, json_out=False)
        assert PAYLOAD_SENTINEL not in buf.getvalue()

    def test_the_printer_takes_no_request_argument_at_all(self):
        """Structural, and the reason this stays true after everyone forgets why:
        the request is not in scope, so it cannot be printed by accident. A future
        edit that wants to echo the payload has to change the signature first,
        which is a visible act in review rather than a one-line slip."""
        import inspect
        params = set(inspect.signature(_print_report).parameters)
        assert params == {"report", "applied", "json_out"}, (
            f"_print_report gained a parameter: {sorted(params)}. If one of them "
            "carries the request, the report can echo what it hoped to write."
        )

    def test_counts_come_from_the_response_not_recomputed(self):
        """A count computed locally from the ops list would agree with
        itself and with nothing else, which could report a failed op as
        applied."""
        import contextlib
        import io
        report = _server_report()
        report["counts"] = {"ops": 9, "ok": 4, "refused": 5, "applied": 4}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(report, applied=True, json_out=False)
        out = buf.getvalue()
        assert "ops=9" in out and "ok=4" in out
        assert "refused=5" in out and "applied=4" in out

    def test_committed_false_is_not_rendered_as_success(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(_server_report(committed=False), applied=True, json_out=False)
        assert "committed=False" in buf.getvalue()

    def test_refusals_are_printed_with_their_index_and_reason(self):
        """The refusal is the product on this endpoint: one status code with
        reason codes, and the index is what a caller acts on."""
        import contextlib
        import io
        report = _server_report()
        report["refusals"] = [
            {"index": 2, "reason": "expect-mismatch", "message": "org differs"}
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(report, applied=False, json_out=False)
        out = buf.getvalue()
        assert "2" in out and "expect-mismatch" in out

    def test_json_mode_emits_the_response_verbatim(self):
        """`--json` must not be a second, divergent rendering of the same data."""
        import contextlib
        import io
        report = _server_report()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_report(report, applied=True, json_out=True)
        assert json.loads(buf.getvalue()) == report
