"""``api.put_bytes`` / ``api.patch`` / ``api.delete``: the transport half of
``fm upload`` and ``fm skill-attach``.

Its own file, not an addition to ``test_formist_cli_api_and_tokens.py``: the same
reasoning ``test_formist_cli_stream_transport.py`` gives: ``api.py`` gets edited by
several lanes at once and a new file cannot conflict with any of them.

``post``, ``put_bytes``, ``patch`` and ``delete`` now share one ``_send`` helper
(see ``formist_cli/api.py``), so the tests here exist to prove that sharing did not
lose anything a caller of the new verbs needs: the method actually sent, the
body actually sent, the 421-misdirected-write refusal, and the ``Content-Type``
each verb declares.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from formist_cli import api
from formist_cli.targets import resolve as resolve_target

LOCAL = resolve_target("127.0.0.1:8160")


@pytest.fixture
def fake_urlopen(monkeypatch):
    captured = {}

    def install(response=None, *, raise_error=None):
        def _urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            if raise_error is not None:
                raise raise_error
            return response

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        return captured

    return install


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def read(self):
        return self.getvalue()


def _ok_response(payload: object) -> _Resp:
    return _Resp(json.dumps(payload).encode())


def _raw_response(body: bytes) -> _Resp:
    return _Resp(body)


def _http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode()
    return urllib.error.HTTPError(
        "https://x/y",
        code,
        "err",
        {},
        io.BytesIO(body),  # type: ignore[arg-type]
    )


class TestPutBytesSendsRawBytesNotJSON:
    def test_the_method_is_PUT(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.put_bytes(LOCAL, "/x", b"\x00\x01\xff", token="t")
        assert captured["method"] == "PUT"

    def test_the_body_is_the_EXACT_bytes_unmodified(self, fake_urlopen):
        """The whole point: a chunk is opaque binary, not something to encode.

        A control alongside it: `post`'s body for the SAME payload is JSON-encoded
        and therefore different bytes: proving `put_bytes` really did skip the
        JSON step rather than the fake just echoing back whatever it was given.
        """
        raw = bytes(range(256)) * 4  # non-JSON, non-UTF8-safe binary
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.put_bytes(LOCAL, "/x", raw, token="t")
        assert captured["body"] == raw

        captured2 = fake_urlopen(_ok_response({"ok": True}))
        api.post(LOCAL, "/x", {"data": "not the same bytes"}, token="t")
        assert captured2["body"] != raw

    def test_content_type_defaults_to_octet_stream(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.put_bytes(LOCAL, "/x", b"abc", token="t")
        assert captured["headers"]["content-type"] == "application/octet-stream"

    def test_content_type_is_overridable(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.put_bytes(LOCAL, "/x", b"abc", token="t", content_type="text/plain")
        assert captured["headers"]["content-type"] == "text/plain"

    def test_the_token_goes_in_the_header_never_the_url(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.put_bytes(LOCAL, "/x", b"abc", token="secret-token")
        assert captured["headers"]["authorization"] == "Bearer secret-token"
        assert "secret-token" not in captured["request"].full_url

    def test_the_decoded_response_is_returned(self, fake_urlopen):
        fake_urlopen(_ok_response({"received_bytes": 3}))
        result = api.put_bytes(LOCAL, "/x", b"abc", token="t")
        assert result == {"received_bytes": 3}


class TestPatchSendsJSON:
    def test_the_method_is_PATCH(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"item": {}}))
        api.patch(LOCAL, "/x", {"files": ["a"]}, token="t")
        assert captured["method"] == "PATCH"

    def test_the_body_is_the_JSON_encoded_payload(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"item": {}}))
        api.patch(LOCAL, "/x", {"files": ["a", "b"]}, token="t")
        assert json.loads(captured["body"]) == {"files": ["a", "b"]}

    def test_content_type_is_json(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"item": {}}))
        api.patch(LOCAL, "/x", {}, token="t")
        assert captured["headers"]["content-type"] == "application/json"


class TestDeleteSendsNoMeaningfulBody:
    def test_the_method_is_DELETE(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.delete(LOCAL, "/x", token="t")
        assert captured["method"] == "DELETE"

    def test_a_204_style_empty_response_decodes_to_None(self, fake_urlopen):
        fake_urlopen(_raw_response(b""))
        assert api.delete(LOCAL, "/x", token="t") is None


class TestWriteVerbsShareTheSameRegionAndBulkRefusalHandling:
    """`_send` is the ONE place these are handled now: one test per new verb is
    enough to prove the sharing didn't silently narrow to POST-only, without
    re-testing the translation logic itself (already covered for `post` in
    ``test_formist_cli_api_and_tokens.py``)."""

    def test_put_bytes_raises_Misdirected_on_421(self, fake_urlopen):
        fake_urlopen(raise_error=_http_error(421, {"api_base": "https://ae.api.evane.ai"}))
        with pytest.raises(api.Misdirected):
            api.put_bytes(LOCAL, "/x", b"abc", token="t")

    def test_patch_raises_Misdirected_on_421(self, fake_urlopen):
        fake_urlopen(raise_error=_http_error(421, {"api_base": "https://ae.api.evane.ai"}))
        with pytest.raises(api.Misdirected):
            api.patch(LOCAL, "/x", {}, token="t")

    def test_put_bytes_translates_401_like_post_does(self, fake_urlopen):
        fake_urlopen(raise_error=_http_error(401, {"error": "not-authenticated"}))
        with pytest.raises(api.NotAuthenticated):
            api.put_bytes(LOCAL, "/x", b"abc", token=None)

    def test_a_transport_failure_is_reported_as_unreachable(self, fake_urlopen):
        fake_urlopen(raise_error=urllib.error.URLError("connection refused"))
        with pytest.raises(api.ApiError, match="could not reach"):
            api.put_bytes(LOCAL, "/x", b"abc", token="t")


class TestPostMultipartSharesTheSameTransport:
    """``fm import`` sends a whole-workspace ZIP as ``multipart/form-data``.

    ⚠️ THIS CLASS EXISTS BECAUSE THE MERGE THAT ADDED THE VERB NEARLY SHIPPED A
    SECOND COPY OF THE REFUSAL. ``post_multipart`` arrived on a branch that had
    extracted its own ``write_http_error`` helper for exactly the right reason —
    a second transport is where a rule gets duplicated: while ``_send`` had
    landed on main meanwhile with the identical intent. Git conflicted on the
    single ``def`` line and auto-merged the rest, so the naive resolution keeps
    BOTH, which is the duplicated-abstraction merge the project rules records from
    2026-08-26. Only the ENCODING differs between the two, so the reconciliation
    routes ``post_multipart`` through ``_send`` and these tests are what says the
    routing is real rather than intended.
    """

    def test_the_method_is_POST_and_the_body_is_the_encoded_multipart(self, fake_urlopen):
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.post_multipart(
            LOCAL, "/x", fields={"merge": "overwrite"}, files={"file": ("b.zip", b"PK\x03\x04")}, token="t"
        )
        assert captured["method"] == "POST"
        assert captured["headers"]["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="merge"' in captured["body"]
        assert b"PK\x03\x04" in captured["body"]

    def test_it_raises_Misdirected_on_421_like_every_other_write(self, fake_urlopen):
        """A bundle sent to a misresolved region must be REFUSED, not followed.

        The whole reason the region redirect is never followed is that a redirect
        means your belief about WHERE was wrong: and this verb carries an entire
        workspace, so following one would import a customer's whole space into a
        database nobody named. Per the class above: one test per verb, to prove
        the sharing did not silently narrow to the verbs that existed first.
        """
        fake_urlopen(raise_error=_http_error(421, {"api_base": "https://ae.api.evane.ai"}))
        with pytest.raises(api.Misdirected):
            api.post_multipart(LOCAL, "/x", fields={}, files={"file": ("b.zip", b"PK")}, token="t")

    def test_it_uses_the_upload_timeout_not_the_default_one(self, fake_urlopen):
        """30s is a limit a megabyte-scale bundle hits on SIZE, not on anything
        being wrong: the wrong cause attached to a real failure."""
        captured = fake_urlopen(_ok_response({"ok": True}))
        api.post_multipart(LOCAL, "/x", fields={}, files={"file": ("b.zip", b"PK")}, token="t")
        assert captured["timeout"] == api.UPLOAD_TIMEOUT
        assert api.UPLOAD_TIMEOUT > api.DEFAULT_TIMEOUT


def test_each_write_refusal_is_constructed_in_exactly_one_place():
    """The PROPERTY, not another per-verb case: one spelling of each refusal.

    A per-verb test proves the verb that exists behaves; it cannot see a SECOND
    implementation appear beside it, and that is the failure that actually
    happened here: two lanes independently extracting the same write-error
    translation, git conflicting on one line and auto-merging the other 172.
    Clean in git, two rules in behaviour.

    ``Misdirected`` and ``BulkRefused`` are the two refusals worth pinning: 421
    decides whether a write lands in the region the operator named, and 400
    ``bulk-refused`` carries the per-op report that says which op was wrong. A
    second construction site for either means a caller can get one spelling and
    a caller one verb over can get the other.

    Counted over the AST rather than by grepping text: a string search cannot
    tell a construction from a mention in a docstring, and this module's
    docstrings mention both by name several times.
    """
    import ast
    import pathlib

    source = pathlib.Path(api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for name in ("Misdirected", "BulkRefused"):
        sites = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]
        assert len(sites) == 1, (
            f"{name} is constructed at {len(sites)} places in formist_cli/api.py "
            f"(lines {sites}). Every write verb must reach the SAME refusal: "
            "route the new transport through `_send` rather than giving it its "
            "own translation."
        )
