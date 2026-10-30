"""``fm chats``: reading an agent run's transcript without misreporting it.

Two things this command can get wrong, and only one of them is obvious.

THE OBVIOUS ONE is access: a background agent run is NOT uniformly space-wide.
``services/agent_sessions.py:240-249`` sets ``private = triggered_by_user_id is not
None``, so a scheduler- or webhook-initiated run is ``owner_scope=WORKSPACE`` and
readable by every member, while a user-initiated one is ``owner_scope=USER`` and
private to that user. ``routes/chats.py:108-125`` answers 404: never 403: for
another member's private chat, deliberately, so the API never reveals it exists.
``require_project_member`` (``routes/chats.py:78-96``) has no platform-admin
bypass. So ``ev`` cannot reach those transcripts, must not try, and must explain
the 404 rather than letting it read as a missing row.

THE NON-OBVIOUS ONE is completeness, and it is the one these tests are mostly
about. ``list_messages`` pages through ``run_list_query`` → ``parse_pagination``,
capped at ``MAX_LIMIT = 200`` (``utils/list_query.py:60``). A long agent run has
more turns than that. So the failure available here is the one
``formist_cli/resolve.py`` rule 2 already names: **a page is not a table**: except
worse: a resolver that measures the page REFUSES, whereas a transcript that
measures the page PRINTS, and 200 turns of real transcript look exactly like a
complete one. Nothing in the output says otherwise unless something puts it there.

Hence the property under test: **completeness is a claim, and it must come from
``total``, not from the reader's impression.** The case that carries it is the
third one: a response with NO ``total`` at all. "I could not tell" and "it is
complete" are different facts, and the direction a forgetful implementation falls
in is the wrong one: ``len(messages) >= total`` with ``total`` defaulting to 0 is
complete for every input, including the truncated ones.
"""

from __future__ import annotations

import pytest

from formist_cli.chats import (
    ChatsError,
    Transcript,
    canonical_kind,
    fetch_messages,
    latest_chat,
    list_chats,
)
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")
SPACE = "452093ea-7b91-491e-88b6-3285edd48a31"
CHAT = "7f3a1c22-0000-4000-8000-000000000001"


def _envelope(items, total=None, *, omit_total=False):
    body = {"items": items, "limit": 200, "offset": 0}
    if not omit_total:
        body["total"] = len(items) if total is None else total
    return body


def _fetcher(*pages):
    """Return each canned envelope in turn, recording the params it was called with."""
    calls: list[dict] = []
    remaining = list(pages)

    def fetch(target, path, *, token=None, params=None, **kwargs):
        calls.append({"path": path, "params": dict(params or {})})
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _msg(n: int) -> dict:
    return {
        "id": f"m{n}",
        "chat_id": CHAT,
        "role": "ASSISTANT",
        "content": f"turn {n}",
        "created_at": "2026-08-27T00:00:00",
    }


class TestCompletenessIsAClaim:
    """A transcript that measured its page must not read as a whole transcript."""

    def test_all_turns_present_is_complete(self):
        fetch = _fetcher(_envelope([_msg(i) for i in range(5)]))
        transcript = fetch_messages(
            LOCAL, SPACE, CHAT, token="t", fetch=fetch
        )
        assert isinstance(transcript, Transcript)
        assert transcript.complete is True
        assert transcript.total == 5

    def test_a_page_short_of_the_total_is_not_complete(self):
        fetch = _fetcher(_envelope([_msg(i) for i in range(200)], total=640))
        transcript = fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        assert transcript.complete is False
        assert transcript.total == 640
        assert len(transcript.messages) == 200

    def test_a_missing_total_is_NOT_complete(self):
        """The guard with no neighbour covering it.

        An envelope with no ``total`` supports no completeness claim in either
        direction. The tempting implementation: ``len(messages) >= (total or 0)``
       : reports complete here, and reports complete for a truncated page too,
        because 0 is not a count, it is the absence of one.
        """
        fetch = _fetcher(_envelope([_msg(i) for i in range(200)], omit_total=True))
        transcript = fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        assert transcript.total is None
        assert transcript.complete is False

    def test_the_rendering_says_so_when_it_truncated(self):
        """Not decoration: this string is the only thing standing between the
        operator and a confident misreading of 200 turns."""
        fetch = _fetcher(_envelope([_msg(i) for i in range(200)], total=640))
        transcript = fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        rendered = transcript.render()
        assert "640" in rendered
        assert "200" in rendered
        assert "--all-messages" in rendered

    def test_the_rendering_of_an_unknown_total_does_not_claim_completeness(self):
        fetch = _fetcher(_envelope([_msg(0)], omit_total=True))
        transcript = fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        rendered = transcript.render().lower()
        assert "unknown" in rendered or "could not" in rendered
        assert "complete" not in rendered.replace("incomplete", "")


class TestPagingStopsHonestly:
    """``--all-messages`` pages until it has them all, and stops if it cannot."""

    def test_pages_until_the_total_is_reached(self):
        pages = [
            _envelope([_msg(i) for i in range(200)], total=450),
            _envelope([_msg(i) for i in range(200, 400)], total=450),
            _envelope([_msg(i) for i in range(400, 450)], total=450),
        ]
        fetch = _fetcher(*pages)
        transcript = fetch_messages(
            LOCAL, SPACE, CHAT, token="t", all_pages=True, fetch=fetch
        )
        assert transcript.complete is True
        assert len(transcript.messages) == 450
        assert [c["params"].get("offset") for c in fetch.calls] == ["0", "200", "400"]

    def test_a_server_that_stops_returning_rows_ends_the_loop(self):
        """A page that adds nothing must terminate the walk rather than spin.

        The server claiming 450 while handing back an empty page is a server bug,
        but an infinite loop in the client is a worse way to discover it: and the
        transcript stays honestly marked incomplete.

        ⚠️ THE CALL COUNT IS THE ASSERTION. The two obvious ones: incomplete, 200
        turns: are both true when the empty-page guard is DELETED, because
        ``MAX_PAGES`` then bounds the walk instead and the extra iterations add no
        rows. That is a neighbouring guard covering the one under test: the
        mutation survives, and the test reads as if it were watching. Only the
        number of requests distinguishes "stopped when the server went quiet"
        from "hammered it 200 times and gave up".
        """
        fetch = _fetcher(
            _envelope([_msg(i) for i in range(200)], total=450),
            _envelope([], total=450),
        )
        transcript = fetch_messages(
            LOCAL, SPACE, CHAT, token="t", all_pages=True, fetch=fetch
        )
        assert transcript.complete is False
        assert len(transcript.messages) == 200
        assert len(fetch.calls) == 2


class TestKindVocabulary:
    """The command line speaks kebab-case; the API speaks the enum."""

    def test_alias_maps_to_the_enum(self):
        assert canonical_kind("background-agent") == "BACKGROUND_AGENT"
        assert canonical_kind("user-chat") == "USER_CHAT"

    def test_the_enum_spelling_is_accepted_as_itself(self):
        assert canonical_kind("BACKGROUND_AGENT") == "BACKGROUND_AGENT"

    def test_none_stays_none(self):
        assert canonical_kind(None) is None

    def test_an_unknown_kind_is_refused_with_the_valid_ones(self):
        with pytest.raises(ChatsError) as exc:
            canonical_kind("agent")
        assert "background-agent" in str(exc.value)

    def test_the_kind_filter_reaches_the_server(self):
        """Filtered server-side, not in the client.

        ``routes/chats.py:239`` takes ``?kind=`` natively and applies it *inside*
        the privacy predicate. Filtering a page client-side would both measure the
        page and re-derive a filter the server already owns.
        """
        fetch = _fetcher(_envelope([]))
        list_chats(
            LOCAL, SPACE, token="t", kind="background-agent", fetch=fetch
        )
        assert fetch.calls[0]["params"]["kind"] == "BACKGROUND_AGENT"


class TestLatest:
    """``--latest`` removes a transcription. It must not invent a row."""

    def test_picks_the_first_row_the_server_ordered(self):
        """Newest-first is the SERVER's order (``routes/chats.py:288`` orders by
        ``updated_at`` desc). The client takes the first row rather than re-sorting
        on a field it would have to parse: a second ordering that can disagree
        with the first is how the wrong run gets shown."""
        fetch = _fetcher(
            _envelope([
                {"id": "newer", "updated_at": "2026-08-27T10:00:00"},
                {"id": "older", "updated_at": "2026-08-26T10:00:00"},
            ])
        )
        chat = latest_chat(LOCAL, SPACE, token="t", fetch=fetch)
        assert chat["id"] == "newer"

    def test_refuses_when_nothing_matches(self):
        fetch = _fetcher(_envelope([]))
        with pytest.raises(ChatsError) as exc:
            latest_chat(LOCAL, SPACE, token="t", kind="background-agent", fetch=fetch)
        assert "background-agent" in str(exc.value)


class TestTheSingleChatEnvelope:
    """The shape the SERVER sends, captured from it rather than assumed.

    This class exists because of a bug that no fake-fetcher test could have
    caught. ``get_chat`` returned the response body directly, but
    ``GET /chats/{id}`` answers ``{"chat": {...}, "messages": [...]}``
    (``routes/chats.py:366-371``) while the LIST endpoint's items are bare chat
    dicts. Every field rendered as ``None`` and the suite stayed green: the fake
    returned whatever shape this module imagined, so the test and the code agreed
    with each other and not with the server. It was found by running the command
    against a live dev server.

    So the key list below is TRANSCRIBED FROM A REAL RESPONSE, not written from
    memory of the route: a fixture quoting a source from memory agrees with
    itself, which is the same failure one level down.
    """

    #: Captured 2026-08-27 from GET /projects/{space}/chats/{id} on local dev.
    REAL_CHAT_KEYS = [
        "agent_status", "archived", "cancel_requested", "created_at",
        "help_article_id", "help_locale", "id", "kind", "knowledge_scope",
        "last_error", "model", "owner_scope", "project_id", "run_ended_at",
        "run_started_at", "title", "trigger_kind", "trigger_ref_id",
        "triggered_by_name", "triggered_by_user_id", "updated_at", "user_id",
    ]

    def test_the_chat_is_unwrapped_from_the_envelope(self):
        from formist_cli.chats import get_chat

        body = {
            "chat": {k: f"<{k}>" for k in self.REAL_CHAT_KEYS},
            "messages": [_msg(0)],
        }
        chat = get_chat(
            LOCAL, SPACE, CHAT, token="t", fetch=_fetcher(body)
        )
        assert chat["id"] == "<id>"
        assert chat["owner_scope"] == "<owner_scope>"

    def test_an_envelope_without_a_chat_is_REFUSED_not_rendered_as_None(self):
        """The guard whose absence was the original bug.

        A tolerant ``payload.get("chat", payload)`` would read as defensive and
        would restore the exact failure this replaced: a shape mismatch printing a
        row of ``None`` instead of saying anything. Refusing is what makes the
        next envelope change visible on the first run rather than never.
        """
        from formist_cli.chats import get_chat

        with pytest.raises(ChatsError) as exc:
            get_chat(LOCAL, SPACE, CHAT, token="t", fetch=_fetcher({"messages": []}))
        assert "no 'chat' object" in str(exc.value)


class TestPrivateChatIsExplained:
    """A 404 on an id ``fm chats`` just listed reads like a bug. It is a rule."""

    def test_the_404_names_the_privacy_rule_and_the_absent_bypass(self):
        from formist_cli import api

        def fetch(target, path, *, token=None, params=None, **kwargs):
            raise api.ApiError(f"404 from local: no route at {path}.", status=404)

        with pytest.raises(ChatsError) as exc:
            fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        message = str(exc.value)
        assert "owner_scope" in message
        assert "platform-admin" in message

    def test_the_status_a_real_response_carries_is_actually_stamped(self):
        """The joint between the two halves, which neither half's tests cover.

        Every other test here hands ``fetch`` an ``ApiError`` built with
        ``status=`` by hand, so they prove ``chats.py`` branches correctly on a
        status that was set for it. Nothing in them proves ``api.py`` sets one off
        a real response: and if it does not, ``_explain_404`` silently stops
        firing while all sixteen tests stay green. That is a green suite blind to
        the only path production takes.
        """
        import io
        import urllib.error

        from formist_cli import api

        error = api._translate(
            urllib.error.HTTPError(
                "http://127.0.0.1:8160/x", 404, "Not Found", {}, io.BytesIO(b"{}")
            ),
            LOCAL,
            "/projects/x/chats/y/messages",
        )
        assert error.status == 404

    def test_a_non_404_is_not_dressed_up_as_a_privacy_answer(self):
        """Only a 404 carries the privacy meaning. A 500 saying "this chat may be
        private" would be a confident wrong explanation, which is worse than the
        raw status it replaced."""
        from formist_cli import api

        def fetch(target, path, *, token=None, params=None, **kwargs):
            raise api.ApiError("500 from local: boom", status=500)

        with pytest.raises(api.ApiError) as exc:
            fetch_messages(LOCAL, SPACE, CHAT, token="t", fetch=fetch)
        assert "owner_scope" not in str(exc.value)
