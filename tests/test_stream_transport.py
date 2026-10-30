"""``api.stream``: the transport half of ``fm logs``, driven for real.

Everything else about ``fm logs`` is tested over a list of strings, which proves
the parsing and proves nothing about the socket. The one behaviour that only
exists at the transport is the one that matters most here: **a connection that
drops must not arrive as an iterator that politely stops.** Those are the same
event to a ``for`` loop and different facts about the system, and the whole
command is built on being able to tell them apart.

``check_api_base`` is asserted here too. ``get``, ``download`` and ``stream`` now
share ``_build_request``, and the reason to share it is that three copies would be
three places for the host allow-list to go missing: so the sharing is worth a test
that would notice.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from formist_cli import api
from formist_cli.targets import Target, TargetError
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")


class _FakeResponse:
    """Just enough of an ``http.client.HTTPResponse`` to iterate.

    ``chunks`` may contain an exception INSTANCE, which is raised at that point in
    the iteration: that is how a mid-stream drop is expressed, and it is the only
    way to write this test without an actual socket that dies on cue.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def fake_urlopen(monkeypatch):
    captured = {}

    def install(response):
        def _urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return response

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        return captured

    return install


class TestTheStreamYieldsLines:
    def test_bytes_become_stripped_text_lines(self, fake_urlopen):
        response = _FakeResponse([b"first\n", b"second\r\n"])
        fake_urlopen(response)
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            assert list(lines) == ["first", "second"]

    def test_undecodable_bytes_are_replaced_not_fatal(self, fake_urlopen):
        """A stream must not die on one bad byte: that would turn a cosmetic
        problem into a lost subscription."""
        fake_urlopen(_FakeResponse([b"ok\n", b"\xff\xfe bad\n"]))
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            out = list(lines)
        assert out[0] == "ok"
        assert "bad" in out[1]

    def test_the_response_is_closed_even_if_the_caller_stops_early(self, fake_urlopen):
        response = _FakeResponse([b"a\n", b"b\n", b"c\n"])
        fake_urlopen(response)
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            next(iter(lines))
        assert response.closed is True


class TestADropIsNotAPoliteEnd:
    """The reason this transport exists rather than a loop around ``download``."""

    def test_a_mid_stream_read_timeout_raises_rather_than_stopping(self, fake_urlopen):
        fake_urlopen(_FakeResponse([b"a\n", TimeoutError("timed out")]))
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            iterator = iter(lines)
            assert next(iterator) == "a"
            with pytest.raises(api.StreamClosed) as exc:
                next(iterator)
        assert "not the same as" in str(exc.value).lower()

    def test_a_socket_error_mid_stream_raises_too(self, fake_urlopen):
        fake_urlopen(_FakeResponse([OSError("connection reset by peer")]))
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            with pytest.raises(api.StreamClosed):
                list(lines)

    def test_stream_closed_is_an_ApiError_so_the_cli_handler_catches_it(self):
        assert issubclass(api.StreamClosed, api.ApiError)

    def test_it_says_there_is_no_replay(self, fake_urlopen):
        """An operator who reconnects must not believe they missed nothing."""
        fake_urlopen(_FakeResponse([TimeoutError("x")]))
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            with pytest.raises(api.StreamClosed) as exc:
                list(lines)
        assert "no replay" in str(exc.value).lower()


NO_HOST = Target(env="custom", api_base="https://", name="no-host")


class TestTheSharedRequestBuilderStillRefusesBadBases:
    """``_build_request`` is shared by get/download/stream precisely so this check
    cannot be present in two of them and missing from the third."""

    def test_stream_refuses_a_base_with_no_host(self):
        with pytest.raises(TargetError):
            with api.stream(NO_HOST, "/admin/events/stream", read_timeout=65):
                pass

    def test_get_and_download_refuse_it_too(self, monkeypatch):
        """All three readers, asserted separately rather than in a loop.

        The point of extracting ``_build_request`` was that the allow-list check
        cannot end up in two readers and missing from the third, so the test has
        to be able to name WHICH one lost it. A loop would print one dot either
        way.
        """
        target = NO_HOST

        with pytest.raises(TargetError):
            api.get(target, "/x")
        with pytest.raises(TargetError):
            api.download(target, "/x")
        with pytest.raises(TargetError):
            with api.stream(target, "/x", read_timeout=65):
                pass

    def test_the_token_goes_in_a_header_and_never_the_url(self, fake_urlopen):
        captured = fake_urlopen(_FakeResponse([b"x\n"]))
        with api.stream(
            LOCAL, "/admin/events/stream", token="s3cret", params={"format": "text"},
            read_timeout=65,
        ) as lines:
            list(lines)
        request = captured["request"]
        assert request.get_header("Authorization") == "Bearer s3cret"
        assert "s3cret" not in request.full_url

    def test_the_read_timeout_reaches_urlopen(self, fake_urlopen):
        """It is the only thing separating an idle socket from a dead one, so a
        value that never arrives at the socket is the same as no value at all."""
        captured = fake_urlopen(_FakeResponse([b"x\n"]))
        with api.stream(LOCAL, "/admin/events/stream", read_timeout=65) as lines:
            list(lines)
        assert captured["timeout"] == 65


class TestOpeningFailuresAreTranslatedLikeEveryOtherCall:
    def test_a_401_becomes_the_login_instruction(self, monkeypatch):
        def _raise(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
            )

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        with pytest.raises(api.NotAuthenticated) as exc:
            with api.stream(LOCAL, "/admin/events/stream", read_timeout=65):
                pass
        assert "fm login" in str(exc.value)

    def test_an_unreachable_server_says_how_to_start_it(self, monkeypatch):
        def _raise(request, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        with pytest.raises(api.ApiError) as exc:
            with api.stream(LOCAL, "/admin/events/stream", read_timeout=65):
                pass
        assert "8160" in str(exc.value)


class TestStreamPost:
    """``api.stream_post``: the same transport, for an endpoint that needs a
    request BODY (``POST /projects/{id}/chats/{id}/run``, ``fm chat``).

    Built on the SAME ``_open_stream``/`_stream_lines`` helpers as ``stream``
    (see the refactor in ``formist_cli/api.py``), so most of the transport's
    properties are proven once, above, for GET. What is specific to a POST is
    what these tests cover: the body is JSON-encoded and sent, the method is
    POST, and the same StreamClosed / host-allow-list guarantees still hold.
    """

    def test_the_body_is_json_encoded_and_the_method_is_post(self, fake_urlopen):
        import json

        captured = fake_urlopen(_FakeResponse([b"x\n"]))
        with api.stream_post(
            LOCAL, "/projects/x/chats/y/run", {"content": "hi"}, token="s3cret",
            read_timeout=65,
        ) as lines:
            list(lines)
        request = captured["request"]
        assert request.get_method() == "POST"
        assert json.loads(request.data.decode()) == {"content": "hi"}
        # urllib.request.Request.add_header stores under key.capitalize() and
        # get_header does an exact lookup (no capitalize() on the read side),
        # so this has to ask for the same capitalization add_header produced.
        assert request.get_header("Content-type") == "application/json"
        assert request.get_header("Authorization") == "Bearer s3cret"

    def test_a_mid_stream_drop_raises_streamclosed_same_as_get(self, fake_urlopen):
        fake_urlopen(_FakeResponse([b"event: on_agent_token\n", TimeoutError("timed out")]))
        with api.stream_post(
            LOCAL, "/projects/x/chats/y/run", {"content": "hi"}, read_timeout=65,
        ) as lines:
            iterator = iter(lines)
            assert next(iterator) == "event: on_agent_token"
            with pytest.raises(api.StreamClosed):
                next(iterator)

    def test_refuses_a_base_with_no_host(self):
        with pytest.raises(TargetError):
            with api.stream_post(
                NO_HOST, "/projects/x/chats/y/run", {}, read_timeout=65,
            ):
                pass

    def test_a_401_becomes_the_login_instruction(self, monkeypatch):
        def _raise(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
            )

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        with pytest.raises(api.NotAuthenticated):
            with api.stream_post(LOCAL, "/projects/x/chats/y/run", {}, read_timeout=65):
                pass
