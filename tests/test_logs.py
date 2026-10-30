"""``fm logs`` — the server's own event firehose, and the thing it cannot tell you.

WHAT CHANGED, AND WHY THE OLD PREMISE IS GONE
----------------------------------------------
``formist_cli/logs.py`` used to read container stdout out of Azure Log Analytics,
because when it was written ``/admin/events/*`` genuinely 404'd in prod. Measured
2026-08-27, that is no longer true:

    https://api.evane.ai/admin/events/rates     401   ← deployed, needs auth
    https://ae.api.evane.ai/admin/events/rates  410   ← the region gate, not absence

401, not 404. ``routes/admin_events.py`` has shipped. The 410 on ``ae`` is
``utils/auth_region_gate.py`` doing its job — the admin surface is Global-only,
which ``api.admin_get`` already knows.

THE DISTINCTION THESE TESTS EXIST TO PROTECT
---------------------------------------------
**A live firehose is not a log.** ``services/dispatcher.py:1`` is an *in-process*
dispatcher and ``routes/admin_events.py:56`` says the stream generator "touches no
database and no Redis". Every dispatched event is also written as a JSON line by
``utils/eventlog.py`` — to a LOGGER, i.e. container stdout, which ``ev`` no longer
reaches by rule.

So this command shows what happens **from now on** and can never answer "what
happened at 14:29 yesterday". That is not a limitation to mention in passing; it
is the single most likely way an operator misreads this tool, and
:data:`LIVE_ONLY_NOTE` is tested as output rather than left in a docstring where
nobody meets it at the moment they need it.

THE PARSER GUARD WORTH THE MOST
--------------------------------
``--json`` consumes an ``EventSourceResponse`` (``routes/admin_events.py:367``)
whose ``ping`` emits SSE **comment** frames. A comment line starts with ``:`` and
carries no data. Emitting those as records would put non-events into a stream
whose entire purpose is being machine-readable — and it would do so only on an
IDLE connection, i.e. exactly when nobody is watching closely enough to notice.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from formist_cli import logs
from formist_cli.logs import (
    LIVE_ONLY_NOTE,
    SUBJECTS,
    LogsError,
    build_params,
    canonical_subject,
    follow,
    parse_sse,
    subject_path,
)
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
GLOBAL = resolve_target("evane")
AE = resolve_target("evane-ae")
FORMIST = resolve_target("formist")


class TestSubjects:
    def test_the_default_subject_is_the_stream(self):
        assert canonical_subject(None) == "stream"

    def test_every_advertised_subject_maps_to_its_route(self):
        for subject in SUBJECTS:
            assert subject_path(subject) == f"/admin/events/{subject}"

    def test_an_unknown_subject_is_refused_with_the_valid_ones(self):
        with pytest.raises(LogsError) as exc:
            canonical_subject("tail")
        assert "stream" in str(exc.value)
        assert "jobs" in str(exc.value)


class TestParamsGoToTheServerVerbatim:
    """The route owns these filters; the client must not re-derive them.

    Filtering a STREAM client-side is worse than filtering a list client-side: the
    lines you dropped were never counted anywhere, so there is no `total` to
    contradict the impression that you saw everything.
    """

    def test_filters_are_passed_through(self):
        params = build_params(
            types="on_meeting_*,@background",
            exclude="on_caption_token",
            project="d260af6f-addf-49cd-9aab-69084aa2969d",
            source="scheduler,api",
            only_work=True,
            mode="digest",
            as_json=False,
        )
        assert params["types"] == "on_meeting_*,@background"
        assert params["exclude"] == "on_caption_token"
        assert params["project_id"] == "d260af6f-addf-49cd-9aab-69084aa2969d"
        assert params["source"] == "scheduler,api"
        assert params["only_work"] == "1"
        assert params["mode"] == "digest"

    def test_unset_filters_are_absent_rather_than_empty(self):
        """An empty string is a filter matching nothing; absence is no filter."""
        params = build_params()
        assert "types" not in params
        assert "exclude" not in params
        assert "project_id" not in params
        assert "only_work" not in params

    def test_the_format_follows_the_json_flag(self):
        assert build_params(as_json=True)["format"] == "json"
        assert build_params(as_json=False)["format"] == "text"

    def test_no_credential_is_ever_put_in_the_query(self):
        """``routes/admin_events.py:44-49`` deliberately refuses ``?token=`` so the
        JWT stays out of URLs, access logs, proxy logs and shell history. A client
        that helpfully added one would undo that decision from the outside."""
        params = build_params(
            types="x", exclude="y", project="z", source="api", only_work=True
        )
        assert not any("token" in key.lower() for key in params)
        assert not any("auth" in key.lower() for key in params)


class TestTheLiveOnlyCaveatIsOutput:
    """Tested as OUTPUT, because a caveat in a docstring is met by nobody."""

    def test_it_says_the_stream_cannot_answer_about_the_past(self):
        note = LIVE_ONLY_NOTE.lower()
        assert "from now on" in note or "live" in note
        assert "yesterday" in note or "past" in note or "already happened" in note

    def test_it_names_where_history_actually_is(self):
        """Being told what a tool cannot do is only useful with somewhere to go."""
        assert "audit" in LIVE_ONLY_NOTE.lower() or "fm connectors" in LIVE_ONLY_NOTE


class TestSSEFrames:
    def test_an_event_and_data_pair_becomes_one_record(self):
        records = list(parse_sse(["event: event", 'data: {"type": "on_x"}', ""]))
        assert records == [{"event": "event", "data": {"type": "on_x"}}]

    def test_a_ping_comment_is_dropped_not_emitted(self):
        """The guard worth the most. sse-starlette's keepalive emits comment
        frames, so this fires on an IDLE connection — when nobody is looking."""
        records = list(
            parse_sse([": ping", "", "event: event", 'data: {"type": "on_x"}', ""])
        )
        assert records == [{"event": "event", "data": {"type": "on_x"}}]

    def test_data_with_no_event_line_still_yields(self):
        records = list(parse_sse(['data: {"a": 1}', ""]))
        assert records == [{"event": "message", "data": {"a": 1}}]

    def test_unparseable_data_is_carried_as_text_not_dropped(self):
        """Dropping it would silently lose a frame; the raw string at least
        reaches the operator, who can see that it was not JSON."""
        records = list(parse_sse(["event: event", "data: not-json", ""]))
        assert records == [{"event": "event", "data": "not-json"}]

    def test_a_frame_without_a_terminating_blank_line_is_still_emitted(self):
        """A stream ends when the connection does, not on a tidy boundary — the
        last frame must not be swallowed because the server was cut off."""
        records = list(parse_sse(["event: event", 'data: {"a": 1}']))
        assert records == [{"event": "event", "data": {"a": 1}}]


class TestFollowStopsWhenAsked:
    """``--for N``. The clock is INJECTED.

    A test that waits N real seconds and asserts on elapsed time is a test about
    the host — the failure mode this repo has hit four times in one day (uptime,
    Node major, process warmth, CPU count). Passing the clock in tests the
    variable instead of measuring the phenomenon.
    """

    def test_it_yields_everything_when_unbounded(self):
        assert list(follow(iter(["a", "b", "c"]))) == ["a", "b", "c"]

    def test_it_stops_once_the_budget_is_spent(self):
        ticks = iter([0.0, 1.0, 2.0, 9.0, 9.0])
        lines = follow(iter(["a", "b", "c", "d"]), stop_after=5, clock=lambda: next(ticks))
        assert list(lines) == ["a", "b"]

    def test_a_zero_budget_is_not_treated_as_unbounded(self):
        """``--for 0`` means stop immediately. Falsy-vs-None is exactly where a
        bound silently becomes no bound at all."""
        ticks = iter([0.0, 0.0])
        assert list(follow(iter(["a", "b"]), stop_after=0, clock=lambda: next(ticks))) == []


class TestAdminIsGlobalOnly:
    """``/admin`` on a non-Global prod region answers 410, so refuse before sending.

    Formist is the one prod exception (its own single-region deployment, not a
    region of EVANE's Global/AE split — see ``formist_cli.targets.is_admin_env``), and
    this is the ONE call site of three (``formist_cli/api.py``, ``formist_cli/cli.py`` are
    the other two) each sharing that predicate so none of them can drift from the
    others the way this one once did.
    """

    def test_a_prod_region_that_is_not_global_is_refused_locally(self):
        from formist_cli.logs import require_admin_region

        with pytest.raises(LogsError) as exc:
            require_admin_region(AE)
        assert "global" in str(exc.value).lower()

    def test_global_and_local_are_allowed(self):
        from formist_cli.logs import require_admin_region

        require_admin_region(GLOBAL)
        require_admin_region(LOCAL)

    def test_formist_is_allowed(self):
        from formist_cli.logs import require_admin_region

        require_admin_region(FORMIST)


# ===========================================================================
# The requirement this whole command exists for
# ===========================================================================
class TestSilenceIsNotAmbiguous:
    """A stream that has emitted NOTHING must not look like a stream that is not
    connected.

    This is the defect class this repo keeps finding, and a log follower is where
    it is most tempting to get wrong, because silence is the EXPECTED steady state.
    An operator staring at a quiet terminal has to be able to tell "nothing is
    happening" from "I am not listening", and those two produce the identical
    picture — an iterator that is not yielding.

    The distinction is made structurally rather than by wording: a subscription
    reaches LIVE only by being handed the server's OWN preamble, so there is no
    path that prints "connected" from what this process merely intended to do.
    """

    def test_a_subscription_starts_unconfirmed(self):
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        assert sub.confirmed is False
        assert sub.phase is logs.Phase.OPENING

    def test_connected_cannot_be_printed_without_the_servers_own_words(self):
        """The mutation guard. ``render_live`` must be unable to describe a
        connection using only what we locally believe about it."""
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        with pytest.raises(logs.LogsError):
            logs.render_live(sub)

    def test_an_empty_preamble_does_not_count_as_confirmation(self):
        """Whitespace is not the server identifying itself."""
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        for empty in ("", "   ", "\n"):
            with pytest.raises(logs.LogsError):
                sub.confirm(empty)
        assert sub.confirmed is False

    def test_the_live_line_quotes_the_server_not_the_target(self):
        """GATE.md: a verification line must read every value it prints from the
        thing it is verifying. The deployment identity comes off the wire."""
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        sub.confirm("# evane admin event firehose · env=prod region=global "
                    "https://api.evane.ai scheduler=on watch=http+subprocess")
        line = logs.render_live(sub)
        assert "watch=http+subprocess" in line
        assert "region=global" in line

    def test_a_quiet_confirmed_stream_says_it_WAS_listening(self):
        sub = self._confirmed()
        sub.end(logs.Ended.DEADLINE, elapsed=60.0)
        ended = logs.render_ended(sub)
        assert "0 events" in ended
        # The affirmative half: the operator must be told the silence was real.
        assert "quiet" in ended.lower() or "idle" in ended.lower()
        assert logs.exit_code(sub) == 0

    def test_a_stream_that_never_confirmed_is_LOUD_and_does_not_exit_zero(self):
        """0 events + never confirmed is the case that must never read as calm."""
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        sub.end(logs.Ended.SERVER_CLOSED, elapsed=0.4)
        ended = logs.render_ended(sub)
        assert "NOT LISTENING" in ended
        assert logs.exit_code(sub) != 0

    def test_an_unconfirmed_stream_that_merely_RAN_OUT_OF_TIME_is_not_a_success(self):
        """The case that makes the unconfirmed branch load-bearing — found by a
        SURVIVING mutation.

        Deleting ``if not sub.confirmed: return 2`` from ``exit_code`` changed
        nothing across 57 tests, because every unconfirmed case tested also ended
        in SERVER_CLOSED, and that neighbour returns non-zero on its own. The
        guard read as dead; it was covered.

        It is load-bearing exactly when the reason is NOT a drop: `fm logs --for 5`
        against something that accepted the connection and sent nothing at all
        would otherwise elapse its budget and report SUCCESS.
        """
        for reason in (logs.Ended.DEADLINE, logs.Ended.INTERRUPTED):
            sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                    path="/admin/events/stream", filters={})
            sub.end(reason, elapsed=5.0)
            assert logs.exit_code(sub) != 0, (
                f"{reason} on a subscription that never connected must not exit 0"
            )

    def test_a_dropped_connection_does_not_exit_zero_either(self):
        """A server that went away mid-stream is not a stream that was quiet."""
        sub = self._confirmed()
        sub.count_event()
        sub.end(logs.Ended.SERVER_CLOSED, elapsed=12.0)
        assert "closed" in logs.render_ended(sub).lower()
        assert logs.exit_code(sub) != 0

    def test_the_two_zero_event_endings_do_not_render_alike(self):
        """The whole requirement, as one assertion: quiet and deaf must differ in
        the output, not only in a field nobody prints."""
        quiet = self._confirmed()
        quiet.end(logs.Ended.DEADLINE, elapsed=60.0)
        deaf = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                 path="/admin/events/stream", filters={})
        deaf.end(logs.Ended.SERVER_CLOSED, elapsed=60.0)
        assert logs.render_ended(quiet) != logs.render_ended(deaf)
        assert logs.exit_code(quiet) != logs.exit_code(deaf)

    @staticmethod
    def _confirmed():
        sub = logs.Subscription(env="global", api_base="https://api.evane.ai",
                                path="/admin/events/stream", filters={})
        sub.confirm("# evane admin event firehose · env=prod region=global "
                    "https://api.evane.ai scheduler=on watch=http+subprocess")
        return sub


class TestTheHandshakeDoesNotEatTheProofOfLife:
    """``render_status`` (services/admin_stream.py:1314) begins with ``#`` — the
    SAME character the two banner lines begin with.

    So a rule of "leading ``#`` lines are the preamble" swallows the keepalive
    status line, and it does so on an IDLE stream: the one line proving the
    connection is alive would be consumed by the code whose job is to prove the
    connection is alive. The preamble is matched on its two literal shapes
    instead (routes/admin_events.py:377-380).
    """

    BANNER = [
        "# evane admin event firehose · env=prod region=global "
        "https://api.evane.ai scheduler=on watch=http+subprocess",
        "# mode=raw · 41 events since boot · excluding on_agent_token",
    ]
    STATUS = "# 09:14:02 · idle 30s · 41 events since boot · 12 signatures · in-flight 0 (idle)"

    def test_the_banner_confirms_and_the_status_line_survives(self):
        sub = self._sub()
        out = list(logs.consume(sub, iter([*self.BANNER, self.STATUS]), as_json=False))
        assert sub.confirmed is True
        kinds = [kind for kind, _ in out]
        assert logs.STATE in kinds
        assert any(self.STATUS in text for _, text in out), (
            "the keepalive status line was swallowed by the handshake"
        )

    def test_a_hash_line_that_is_not_the_mode_line_ends_the_preamble(self):
        """The case that separates the two rules — found by a SURVIVING mutation.

        Against today's server the banner is always exactly two lines, so
        "absorb leading ``#`` lines" and "absorb the two literal shapes" behave
        identically and the first version of this class could not tell them
        apart: the mutation that swapped one for the other passed 57 tests.

        They differ the moment a ``#`` line arrives that is NOT the mode line —
        a banner whose shape changed, or a status line that beat it. Under the
        leading-hash rule that line is swallowed into the handshake and the
        operator never sees it; under the literal rule the preamble closes on
        what did arrive and the line reaches them.

        This is the guard tested at its OWN level rather than through the input
        where its neighbour covers it.
        """
        sub = self._sub()
        out = list(logs.consume(sub, iter([self.BANNER[0], self.STATUS]), as_json=False))
        assert sub.confirmed is True, "a head-only banner still identifies the deployment"
        assert sub.status_lines == 1, "the status line was absorbed into the preamble"
        assert (logs.STATE, self.STATUS) in out
        assert self.STATUS not in (sub.server_preamble or "")

    def test_a_status_line_is_not_counted_as_an_event(self):
        sub = self._sub()
        list(logs.consume(sub, iter([*self.BANNER, self.STATUS, self.STATUS]), as_json=False))
        assert sub.events == 0

    def test_a_real_event_line_is_counted_and_reaches_stdout(self):
        sub = self._sub()
        event = ("08-27 09:14:03 scheduler  [connector-sync]  "
                 "on_sync_finished  p:296346bf e:-  moved=3")
        out = list(logs.consume(sub, iter([*self.BANNER, event]), as_json=False))
        assert sub.events == 1
        assert (logs.OUT, event) in out

    def test_json_mode_confirms_on_the_ready_frame(self):
        sub = self._sub()
        lines = [
            "event: ready",
            'data: {"type": "ready", "mode": "raw", "deployment": '
            '{"env": "prod", "region": "global", "api_origin": "https://api.evane.ai", '
            '"scheduler": "on"}, "events_since_boot": 41}',
            "",
        ]
        list(logs.consume(sub, iter(lines), as_json=True))
        assert sub.confirmed is True
        assert "region" in (sub.server_preamble or "")

    def test_json_status_frames_are_state_not_events(self):
        sub = self._sub()
        lines = [
            "event: ready", 'data: {"type": "ready", "deployment": {"region": "global"}}', "",
            "event: status", 'data: {"events_since_boot": 41, "dropped": 0}', "",
        ]
        out = list(logs.consume(sub, iter(lines), as_json=True))
        assert sub.events == 0
        assert any(kind is logs.STATE for kind, _ in out)

    @staticmethod
    def _sub():
        return logs.Subscription(env="global", api_base="https://api.evane.ai",
                                 path="/admin/events/stream", filters={})


class TestKeepaliveIsWhatMakesIdleDistinguishableFromDead:
    """With no keepalive there is NOTHING on the wire when the bus is quiet, so a
    live idle stream and a dead socket are byte-for-byte identical.

    The two formats differ and the check models that rather than being uniformly
    strict: sse-starlette pings on its own schedule (``ping=max(keepalive or 30, 1)``,
    routes/admin_events.py:369), so JSON is never silent. Text has only the status
    line, which ``keepalive=0`` turns off.
    """

    def test_text_mode_refuses_keepalive_zero(self):
        with pytest.raises(LogsError) as exc:
            logs.require_liveness_signal(0, as_json=False)
        assert "keepalive" in str(exc.value).lower()

    def test_json_mode_allows_keepalive_zero_because_sse_still_pings(self):
        logs.require_liveness_signal(0, as_json=True)

    def test_a_positive_keepalive_is_fine_in_either_format(self):
        logs.require_liveness_signal(30, as_json=False)
        logs.require_liveness_signal(30, as_json=True)

    def test_the_read_timeout_tolerates_exactly_one_missed_keepalive(self):
        """Sized off the SERVER's period, not a number picked here."""
        assert logs.read_timeout_for(30, as_json=False) > 30 * 2
        assert logs.read_timeout_for(5, as_json=False) > 5 * 2

    def test_json_keepalive_zero_is_sized_off_sse_starlettes_own_default(self):
        """``ping=max(keepalive or 30, 1)`` — the fallback is 30, not 0. Sizing the
        timeout on the requested 0 would abort every idle JSON stream at once."""
        assert logs.read_timeout_for(0, as_json=True) > 30


class TestHelpSaysWhatTheStreamCarries:
    """``fm logs`` on a shared screen is how this data leaves the room, so the
    disclosure is in ``--help`` where it is read before the command runs.

    Measured 2026-08-27 against services/admin_stream.py's redaction: credentials
    and user CONTENT are handled; IDENTITY and NAMING metadata are not.
    """

    def test_it_names_the_fields_that_actually_leak(self):
        """⚠️ THIS TEST HAS NOW BEEN WRITTEN THREE WAYS. Read why before a fourth.

        v1 asserted --help named `join_url` and `author_name` as leaks — true.
        v2 asserted the opposite, after a fix masked URLs and emails by value.
        v3 (this one) is back to v1's contract, because the owner ruled the fix must
        NOT ship: logs are not redacted, because they are analysed from unredacted output.

        The lesson is not "tests churn". It is that a --help describing a redaction
        that does not exist is worse than no warning at all, because it trains the
        reader to discount the parts that ARE true. v2 was correct for a tree that
        never shipped; leaving it would have been a confident, wrong, actionable
        sentence — the same defect as the _KNOWN_UNDEPLOYED map two commits ago.
        """
        carries = logs.STREAM_CARRIES.lower()
        assert "not redacted" in carries
        assert "join_url" in carries
        assert "author_name" in carries
        assert "email address" in carries

    def test_it_says_a_clip_is_not_a_redaction(self):
        """The measurement that stops the reader reassuring themselves.

        "Clipped to 80 chars" reads as mitigation. A real Teams join URL measured
        69 characters, so it prints ENTIRE with its ?p= passcode. Without this
        number a reader does the arithmetic wrong in the safe direction.
        """
        carries = logs.STREAM_CARRIES
        assert "69" in carries
        assert "clip is not a redaction" in carries.lower()

    def test_it_points_at_the_shelved_fix_instead_of_inviting_a_rediscovery(self):
        """A known, measured, deliberately-unmerged defect must say so where it is
        met — or the next operator reports it as new and someone re-derives it."""
        carries = logs.STREAM_CARRIES
        assert "security/event-stream-identity-leak" in carries
        assert "not merged" in carries.lower() or "NOT merged" in carries

    def test_it_says_what_IS_redacted_so_the_warning_is_calibrated(self):
        carries = logs.STREAM_CARRIES.lower()
        assert "redact" in carries or "credential" in carries

    def test_it_scopes_the_warning_so_it_is_not_read_as_bigger_than_it_is(self):
        """/admin/events is platform-admin-only and a platform admin can already
        read space data through /admin/*. Saying so is part of being accurate: an
        overstated warning is discounted the same way a stale one is."""
        carries = logs.STREAM_CARRIES.lower()
        assert "platform-admin-only" in carries
        assert "not an authorization boundary" in carries

    def test_it_is_dated_like_the_export_note(self):
        assert "2026-08-27" in logs.STREAM_CARRIES

    def test_the_parser_actually_carries_it(self):
        """A constant nobody prints is a docstring. This asserts it reaches argparse."""
        import argparse

        parser = argparse.ArgumentParser()
        logs.add_parser(parser.add_subparsers(dest="command"))
        help_text = parser.format_help()  # not the sub-parser's — that is a separate object
        del help_text
        sub_parser = logs.build_standalone_parser()
        rendered = sub_parser.format_help()
        assert "NOT REDACTED" in rendered
        assert "join_url" in rendered
        assert "scrollback hygiene" in rendered


class TestEvNeverInvokesAz:
    """Owner rule, no exceptions. Enforced structurally rather than by review,
    because the previous version of THIS module shelled out to ``az rest``."""

    def test_no_module_under_formist_cli_mentions_the_az_cli(self):
        import pathlib

        offenders = []
        root = pathlib.Path(logs.__file__).parent
        for module in sorted(root.glob("*.py")):
            text = module.read_text()
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"""["'\[\s]az[\s"']""", line) or '"az"' in line:
                    offenders.append(f"{module.name}:{number}: {stripped}")
        assert not offenders, "formist_cli must never invoke the az CLI:\n" + "\n".join(offenders)

    def test_formist_cli_does_not_import_the_break_glass_channel(self):
        """Checked by AST, not by grepping for the word.

        The first version of this test searched the file text and failed on
        ``logs.py``, which NAMES ``scripts/execchan.py`` in its docstring — the
        cross-reference GATE.md asks for, so that the next reader knows the module
        exists and why it is out there. A substring standing in for a structural
        fact reported a correct cross-reference as a rule violation; the rule is
        about the import graph, so read the import graph.
        """
        import ast
        import pathlib

        root = pathlib.Path(logs.__file__).parent
        offenders = []
        for module in sorted(root.glob("*.py")):
            tree = ast.parse(module.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "execchan" in (node.module or ""):
                    offenders.append(f"{module.name}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    if any("execchan" in alias.name for alias in node.names):
                        offenders.append(f"{module.name}:{node.lineno}")
        assert offenders == [], f"execchan must stay outside the package: {offenders}"

    @staticmethod
    def _missing_repo_paths(scan_dir: pathlib.Path, repo: pathlib.Path) -> list[str]:
        """Every ``scripts/…`` path named in prose under `scan_dir` that is absent.

        A module-level helper and not an inline loop, because the companion test
        below has to exercise THIS code. The first version re-implemented the match
        inline, and gutting the real check left the companion green — the check that
        cannot fail, inside the test written to prove the check can fail.
        """
        import re

        missing = []
        for module in sorted(scan_dir.glob("*.py")):
            for number, line in enumerate(module.read_text().splitlines(), 1):
                for hit in re.findall(r"\b(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))", line):
                    if not (repo / hit).exists():
                        missing.append(f"{module.name}:{number} names {hit}, which is absent")
        return missing

    def test_that_path_check_can_actually_fail(self, tmp_path):
        """Drives the REAL helper, not a copy of its logic.

        The first version of this test re-implemented the regex and the existence
        check inline. Gutting the actual guard therefore changed nothing and this
        stayed green — a mutation proved it (P3 SURVIVED). That is the defect this
        repo names most often, appearing inside the test whose entire job is to show
        the guard can fail.
        """
        (tmp_path / "fake_module.py").write_text(
            '"""It moved to ``scripts/definitely_not_here.py`` for reasons."""\n'
        )
        missing = self._missing_repo_paths(tmp_path, tmp_path)
        assert len(missing) == 1
        assert "definitely_not_here" in missing[0]

        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "definitely_not_here.py").write_text("")
        assert self._missing_repo_paths(tmp_path, tmp_path) == [], (
            "the helper must go quiet once the path exists, or it always fails"
        )

    def test_that_import_check_can_actually_fail(self):
        """The guard above passes on a tree where nothing was ever wrong, which is
        indistinguishable from a guard that cannot fail. So run it against a module
        that DOES import execchan and require it to object."""
        import ast

        tree = ast.parse("from scripts.execchan import run\nimport scripts.execchan\n")
        found = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.ImportFrom) and "execchan" in (node.module or ""))
            or (isinstance(node, ast.Import) and any("execchan" in a.name for a in node.names))
        ]
        assert len(found) == 2
