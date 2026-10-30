"""Stdlib HTTP client for the Evane admin API, with errors an operator can act on.

``ev`` must add no runtime dependency — the production image builds with
``uv sync --frozen --no-dev`` — so this is ``urllib`` rather than ``httpx``.

The value here is not the transport, it is the failure translation. Three of the
statuses this surface returns are routinely misread:

**410** is not "gone". ``/auth`` and ``/admin`` are served from the **Global**
region only, and the region gate installs *only on non-Global* instances. So a 410
means "you pointed an admin call at ``ae``" — and the fix is to call Global, which
already reads the AE database cross-region (``AE_DATABASE_URL`` +
``session_for_region``). Space-scoped admin data is NOT per-region.

**404** on this API does not always mean the path is wrong: a router can exist on
``main`` and not be in the deployed image, and it then 404s in prod while working
locally. ``ev`` used to carry a list of which routes those were; the list rotted
(all eight entries measured as deployed on 2026-08-27) and a stale entry tells an
operator to wait for a release that already shipped. The message now names both
possibilities and points at ``fm instance``, which measures the deployed build
rather than remembering it.

**401** means the stored session is missing or stale, not that the endpoint is
protected in some new way.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator

from formist_cli.targets import Target, check_api_base, is_admin_env

#: ⚠️ THERE IS DELIBERATELY NO "KNOWN UNDEPLOYED PATHS" MAP HERE ANY MORE.
#:
#: There used to be one: eight admin paths plus a suffix pattern, each mapped to a
#: description, so a 404 could say "this exists in the codebase but is NOT DEPLOYED
#: to this region yet — it will start working after the next release."
#:
#: Measured 2026-08-27, ALL EIGHT answered 401 in Global prod, i.e. every one was
#: deployed and the whole map was wrong. Re-measured independently by the ev-cli
#: integration lane the same day, with a CONTROL: a path that genuinely does not
#: exist answers 404, so the probe could tell the two apart and did. That is what
#: makes "fm cannot distinguish them from here" a measurement rather than a hedge. The suffix pattern was stale too, and its
#: own comment said so — "is live in BOTH prod regions today".
#:
#: The map's real defect was not the data. It was that the sentence is a PREDICTION
#: ABOUT A DEPLOYMENT'S FUTURE, made from a hardcoded list inside a client that has
#: never spoken to that deployment. A client cannot know it. And the sentence was
#: confident, wrong and ACTIONABLE — strictly worse than an unhandled error, because
#: an operator acts on it and stops looking.
#:
#: **Can anything pin it against going stale again? No, and the reason is
#: structural: the fact lives in a running deployment, not in this tree.** No test
#: can reach prod; a test that could would be asserting about a system that changes
#: under it with every release on either side. This is the same class the project rules
#: already records for the prod/local schema relationship — a MEASUREMENT, not a
#: fact that can be stored.
#:
#: So the pin is to delete the thing that can go stale. A 404 now states the
#: ambiguity honestly and names the instrument that MEASURES the answer, instead of
#: predicting it.

DEFAULT_TIMEOUT = 30

#: Timeout for an upload. Separate from :data:`DEFAULT_TIMEOUT` because a
#: whole-workspace bundle is megabytes and would otherwise fail on SIZE while
#: reporting a timeout, i.e. the wrong cause attached to a real failure.
UPLOAD_TIMEOUT = 600


class ApiError(Exception):
    """An API call that failed, phrased for whoever has to fix it."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        #: The HTTP status behind this, when there was one — ``None`` for a
        #: transport failure, or for a refusal ``ev`` made before sending
        #: anything.
        #:
        #: Carried as a NUMBER on purpose. A caller that needs to branch on the
        #: status (``evtool/chats.py`` translates 404 into the chat privacy rule
        #: and must not dress up any other status that way) would otherwise read
        #: it back out of the message text — a substring standing in for a value,
        #: which is the shape the project rules records as a check that cannot fail.
        self.status = status


class NotAuthenticated(ApiError):
    pass


class StreamClosed(ApiError):
    """A stream that was open and is not any more.

    Its own class because the caller must be able to tell it apart from "the
    stream was quiet". Both look identical at the terminal — an iterator that
    stopped yielding — and only one of them means you are no longer being told
    anything. See ``evtool/logs.py``.
    """


def _build_request(
    target: Target,
    path: str,
    *,
    token: str | None,
    params: dict[str, str] | None,
    accept: str | None = None,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
) -> urllib.request.Request:
    """The one place a request to this API is constructed.

    Extracted rather than copied because :func:`get`, :func:`download` and
    :func:`stream` differ ONLY in how they consume the response — same host check,
    same param encoding, same credential placement. Three copies of that would be
    three places for the allow-list check to go missing, and the one that lost it
    would still work against every host that is fine.

    ``check_api_base`` runs FIRST and before any socket is opened: the retired
    we-carbon stack answers 200 with stale data, so an unknown host must be refused
    rather than tried.

    The credential goes in the ``Authorization`` header, never the query string —
    ``routes/admin_events.py:41-45`` refuses ``?token=`` for exactly this reason, and
    a client that helpfully added one would undo that decision from the outside.

    ``method``/``data``/``content_type`` exist for :func:`stream_post` — the one
    other caller on this shared path that needs a request BODY (a streaming GET
    has none). :func:`get`, :func:`download` and :func:`stream` never pass them,
    so the ``urllib.request.Request`` they get is byte-identical to before this
    parameter existed.
    """
    check_api_base(target.api_base)

    url = f"{target.api_base}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"

    request = urllib.request.Request(url, data=data, method=method)
    if accept:
        request.add_header("Accept", accept)
    if content_type:
        request.add_header("Content-Type", content_type)
    if token:
        # Bearer, not a cookie: utils/auth.current_user accepts either, and a
        # header keeps the credential out of any URL that might be logged.
        request.add_header("Authorization", f"Bearer {token}")
    return request


def get(
    target: Target,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """GET ``path`` from ``target`` and return the decoded JSON body."""
    request = _build_request(target, path, token=token, params=params, accept="application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        raise _translate(exc, target, path) from None
    except urllib.error.URLError as exc:
        raise ApiError(_unreachable(target, exc)) from None

    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def download(
    target: Target,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bytes, str | None]:
    """GET ``path`` as RAW BYTES plus its ``Content-Disposition``.

    Separate from :func:`get` because an export is not JSON in either direction: a
    bundle ZIP cannot survive ``json.loads``, and even the JSON bundle should land
    on disk as the server serialized it rather than as this process would
    re-serialize it. Round-tripping through a dict and back would change key order
    and whitespace, so the file would no longer be the bytes the server sent —
    which is precisely what a portability export is for.

    The header comes back rather than the filename, because parsing it is the
    caller's business (``evtool/export.py`` sanitises it — a server-supplied
    filename must not choose a path).
    """
    request = _build_request(target, path, token=token, params=params)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Disposition")
    except urllib.error.HTTPError as exc:
        raise _translate(exc, target, path) from None
    except urllib.error.URLError as exc:
        raise ApiError(_unreachable(target, exc)) from None


def _open_stream(
    request: urllib.request.Request, target: Target, path: str, read_timeout: float
):
    """``urlopen`` a streaming request, translating a failure to open it.

    Shared by :func:`stream` and :func:`stream_post`: the two build a different
    request (GET vs POST+body — see :func:`_build_request`), but what a failure
    to even open the socket MEANS is identical, and a second copy of that
    translation is exactly the drift ``_build_request`` was extracted to prevent
    for the request-construction half.
    """
    try:
        return urllib.request.urlopen(request, timeout=read_timeout)
    except urllib.error.HTTPError as exc:
        raise _translate(exc, target, path) from None
    except urllib.error.URLError as exc:
        raise ApiError(_unreachable(target, exc)) from None


def _stream_lines(response, target: Target) -> Iterator[str]:
    """Decode an already-open response into lines, one at a time.

    Raises :class:`StreamClosed` on a mid-stream drop rather than letting the
    iterator simply stop — the alternative would render a dropped connection as
    a quiet one, which is the failure both :func:`stream` and :func:`stream_post`
    exist to make impossible. See ``evtool/logs.py`` for why that distinction is
    the whole point of a follower.
    """
    try:
        for raw in response:
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
    except (TimeoutError, OSError) as exc:
        # Not a normal end. A read timeout means the keepalive (or, for
        # stream_post, the operator's read_timeout) we sized this against did
        # not arrive, which is a dropped connection wearing a quiet stream's
        # clothes.
        raise StreamClosed(
            f"the stream to {target.name} stopped mid-connection ({exc}).\n"
            "This is NOT the same as 'nothing happened' — the server went away "
            "or the network did, and events emitted from now on are lost.\n"
            "→ Re-run to reconnect. There is no replay: the bus is in-process "
            "and nothing buffers for a disconnected subscriber."
        ) from None


@contextlib.contextmanager
def stream(
    target: Target,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    read_timeout: float,
) -> Iterator[Iterator[str]]:
    """Open a long-lived GET and yield decoded LINES as the server writes them.

    A third consumer of :func:`_build_request`, not a second transport. Everything
    up to ``urlopen`` is shared with :func:`get` and :func:`download`; what differs
    is that the response is NOT read to completion — a firehose has no completion —
    so the socket stays open and the caller iterates.

    That single difference is why this cannot simply be ``download`` in a loop:
    ``download`` calls ``response.read()``, which by definition returns when the
    body ends, and this body is not supposed to end.

    ⚠️ ``read_timeout`` IS REQUIRED AND HAS NO DEFAULT. It is the only thing that
    can tell a live-but-idle stream from a dead socket, and the two are otherwise
    indistinguishable: both are a file object that yields nothing. A default here
    would be a number picked without knowing the server's keepalive period, which
    is the one fact that makes the distinction possible — so the caller, which does
    know it, must state it. See ``evtool/logs.py::read_timeout_for``.

    Raises :class:`StreamClosed` when the connection drops mid-stream. The
    alternative — letting the iterator simply stop — would render a dropped
    connection as a quiet one, which is the failure this whole command exists to
    make impossible.
    """
    request = _build_request(target, path, token=token, params=params)
    response = _open_stream(request, target, path, read_timeout)
    try:
        yield _stream_lines(response, target)
    finally:
        response.close()


@contextlib.contextmanager
def stream_post(
    target: Target,
    path: str,
    body: object,
    *,
    token: str | None = None,
    params: dict[str, str] | None = None,
    read_timeout: float,
) -> Iterator[Iterator[str]]:
    """Open a long-lived POST carrying a JSON body and yield decoded LINES.

    :func:`stream` cannot serve an endpoint like the agent run route
    (``POST /projects/{id}/chats/{id}/run``, ``routes/agent.py``): the turn's
    text has to travel in the request BODY, and ``stream``'s request is GET-only
    by construction. This is that same transport for a POST — same host
    allow-list (:func:`_build_request`), same "a drop is not a quiet stream"
    guarantee (:class:`StreamClosed`, via :func:`_stream_lines`), same required
    ``read_timeout`` and for the same reason: idle and dead are indistinguishable
    without one.

    The body is JSON-encoded here rather than accepted pre-encoded, matching
    :func:`post` — ``ev`` has one write encoding, not one per caller.
    """
    request = _build_request(
        target,
        path,
        token=token,
        params=params,
        accept="text/event-stream",
        method="POST",
        data=json.dumps(body).encode(),
        content_type="application/json",
    )
    response = _open_stream(request, target, path, read_timeout)
    try:
        yield _stream_lines(response, target)
    finally:
        response.close()


def _translate(exc: urllib.error.HTTPError, target: Target, path: str) -> ApiError:
    """Translate, then stamp the status on whatever came back.

    Stamping HERE rather than at each ``return`` is deliberate: this function has
    six branches and a seventh will be added, and a per-branch ``status=`` is a
    thing to remember. Setting it once on the way out cannot miss one.
    """
    error = _translated(exc, target, path)
    error.status = exc.code
    return error


#: The refusal a CLI credential gets when its OWNER is a platform admin but THIS
#: credential was not granted the admin scope (routes/admin.py's
#: CLI_ADMIN_SCOPE_REQUIRED). Unlike the two ``platform-admin-required`` messages
#: below — which describe the same gate from a GET and a POST and famously drifted
#: apart — this one is a single function called from both branches: the answer does
#: not depend on the method, so there is nothing for a second copy to say.
#:
#: It deliberately offers no way around the refusal beyond re-authorizing, because
#: there is none: the server checks the scope on every call and the CLI holds no
#: credential that could bypass it.
def _cli_scope_refusal(target: Target, detail: str | None) -> ApiError:
    return ApiError(
        f"403 from {target.name}: {detail or 'this credential lacks the admin scope'}\n"
        "Your ACCOUNT may well be a platform admin — this particular `fm` "
        "credential was not granted platform-admin access when you authorized it.\n"
        f"→ `fm login --host {target.name}` again and tick 'Also allow platform-admin "
        "actions' on the authorization page. The box is only shown to platform "
        "admins, and the server re-checks both facts on every call."
    )


def _translated(exc: urllib.error.HTTPError, target: Target, path: str) -> ApiError:
    try:
        payload = json.loads(exc.read().decode())
    except Exception:
        payload = {}
    code = payload.get("error") if isinstance(payload, dict) else None
    detail = payload.get("message") if isinstance(payload, dict) else None

    if exc.code == 410 or code == "auth-not-served-in-region":
        return ApiError(
            f"410 from {target.name}: {path} is served from the GLOBAL region only.\n"
            "The /auth and /admin surfaces are Global-only; the region gate runs on "
            "the non-Global instances.\n"
            "→ Retry with `--host evane`. You do NOT lose AE data by doing so: the "
            "Global admin API reads the AE database cross-region, so space-scoped "
            "admin data is not per-region."
        )

    if exc.code == 404:
        # ⚠️ SIBLING: `_translated_body`'s 404 branch, ~500 lines below, answers
        # for the exact same ambiguity on the write path. If you change one,
        # change the other.
        #
        # A domain 404 (space/user/chat/skill/invitation not found — every
        # `ErrorResult(404, ...)` in routes/admin.py) carries a structured
        # `{"error": ..., "message": ...}` body; a route FastAPI never registered
        # answers `{"detail": "Not Found"}`, which has no `error` key. `code`
        # tells the two apart, so a typo'd id in `fm users add-space` or `ev
        # spaces delete-conversation` gets the real reason instead of a prompt to
        # go compare build revisions for something that was never a deploy gap.
        if code:
            return ApiError(f"404 from {target.name} on {path}: {detail or code}")
        return ApiError(
            f"404 from {target.name}: {path} is not served there.\n"
            "Two different things look identical here and fm cannot tell them "
            "apart: the path may be wrong, or it may be right and simply not in "
            "the image this region is running.\n"
            f"→ `fm instance --host {target.name}` reports the deployed build and "
            "migration revision. Compare it with the code you are holding; that "
            "MEASURES the answer, where fm could only guess it."
        )

    if exc.code == 401 or code == "not-authenticated":
        return NotAuthenticated(
            f"401 from {target.name}: no valid session.\n"
            f"→ `fm login --host {target.name}`  (check the current one with "
            f"`fm whoami --host {target.name}`)"
        )

    if code == "cli-admin-scope-required":
        return _cli_scope_refusal(target, detail)

    if exc.code == 403 or code == "platform-admin-required":
        # ⚠️ SIBLING: `_translated_body`'s 403 branch answers for the SAME gate, 160
        # lines below. Both 404 branches already carry this warning; the 403 pair did
        # not, and that is exactly how the POST one went on describing
        # `require_admin_opaque` after the endpoint stopped using it — every clause
        # false, in the merge only, because neither branch was wrong alone. If you
        # change one of these two messages, change the other.
        return ApiError(
            f"403 from {target.name}: this needs User.is_platform_admin on your row.\n"
            "A valid session is not enough — the flag is checked fresh on every call."
        )

    return ApiError(f"{exc.code} from {target.name} on {path}: {detail or code or exc.reason}")


def _unreachable(target: Target, exc: urllib.error.URLError) -> str:
    if target.env == "local":
        return (
            f"could not reach {target.api_base} ({exc.reason}).\n"
            "Is the server running on that port?"
        )
    return (
        f"could not reach {target.api_base} ({exc.reason}).\n"
        f"Check the deployment is up: `fm health --host {target.name}`."
    )


#: ``/admin`` prefixes the SERVER serves from every region, so this client must
#: not refuse them. Mirrors the routers' own
#: ``declare_admin_region_scope(..., ADMIN_REGION_LOCAL)`` calls; the two are held
#: equal by ``tests/test_ev_admin_parity.py::
#: test_the_cli_knows_every_region_local_admin_prefix_the_server_declares``, which
#: imports the routers and compares. A local tuple rather than a live read because
#: importing ``routes.*`` would pull the whole server into a CLI start-up.
#:
#: ⚠️ THE REASON THIS EXISTS AT ALL (D349): before it, `fm messages dedupe-scan
#: --env ae` was refused HERE, by the client, without a request being sent — and
#: `--host evane` was allowed and answered "OUTLOOK connections: 0" off Global's
#: own database, which never held that space's mail. Two wrong answers, and the
#: permitted one was the plausible one. The real figure was 36 duplicate groups.
REGION_LOCAL_ADMIN_PREFIXES = (
    "/admin/alerts",
    "/admin/instance",
    "/admin/messages",
    "/admin/whatsapp",
)


def _is_region_local_admin(path: str) -> bool:
    """Segment-aware, exactly like the server's gate: ``/admin/messages`` covers
    ``/admin/messages/dedupe`` and never ``/admin/messages-export``."""
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in REGION_LOCAL_ADMIN_PREFIXES
    )


def _check_admin_target(target: Target, path: str, token: str | None) -> None:
    """The two refusals every ``/admin`` call shares, in ONE place.

    Extracted when the plan endpoints added a WRITE verb to this surface: a second
    copy of these two checks is the shape this codebase has been bitten by
    repeatedly (``_translated`` / ``_translated_body`` below still carry the same
    403 message twice, each naming the other, because they could not be merged).
    These could, so they are.

    Failing here rather than at the server saves a round trip and, more usefully,
    explains the topology instead of returning a status code.

    ⚠️ ``REGION_FORMIST`` is exempt from the Global-only gate below for a
    different reason than the region-local-admin prefixes are: this gate exists
    because EVANE is multi-region and only Global mounts the full `/admin`
    router. Formist is a wholly separate, single-region deployment with no
    Global/AE split of its own — its one API host serves the whole `/admin`
    surface locally, the same as a local dev server does. Gating it behind
    "must be Global" would refuse every Formist admin call outright. See
    ``formist_cli.targets.is_admin_env`` — the one predicate this and the two other
    admin-region gates (``evtool/logs.py``, ``evtool/cli.py``) all share.
    """
    if (
        target.is_prod
        and not is_admin_env(target.env)
        and not _is_region_local_admin(path)
    ):
        raise ApiError(
            f"{path} is served from the GLOBAL region only — sending this to "
            f"{target.name} would return 410.\n"
            "→ Use `--host evane`. The Global admin API reads the AE database "
            "cross-region, so you still see AE spaces from there."
        )
    if not token:
        # A LIBRARY-layer guard: this module can be imported directly by a script
        # that never came through the CLI, so it cannot assume the resolver ran.
        # evtool/context.py::resolve raises the same refusal earlier for callers
        # that did. Both exist deliberately — removing either changes nothing
        # observable for an /admin command, which is why a mutation of one
        # survives. Neither is dead; each covers a caller the other cannot see.
        raise NotAuthenticated(
            f"no stored session for {target.name}. → `fm login --host {target.name}`"
        )


def admin_get(target: Target, path: str, *, token: str | None, **kwargs) -> object:
    """GET an ``/admin`` path, refusing up front to send it to a gated region."""
    _check_admin_target(target, path, token)
    return get(target, path, token=token, **kwargs)


def admin_put(target: Target, path: str, body: object, *, token: str | None) -> object:
    """PUT JSON to an ``/admin`` path, behind the same two refusals as the GET.

    ⚠️ This sends a WRITE to whatever ``target`` names, so the caller must have
    resolved ``--env`` from the command line and never from ``fm use`` — see
    ``evtool/prefs.py``. The one write on this surface today is
    ``PUT /admin/spaces/{id}/plan``, which is itself a dry run unless the body says
    ``apply: true``.
    """
    _check_admin_target(target, path, token)
    return put(target, path, body, token=token)


def admin_put_multipart(
    target: Target,
    path: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes]],
    token: str | None,
) -> object:
    """PUT ``multipart/form-data`` to an ``/admin`` path, behind the same two
    refusals as the GET.

    ⚠️ Same caveat as :func:`admin_put`: this is a WRITE, so the caller must have
    resolved ``--env`` from the command line and never from ``fm use``. The one
    caller today is ``fm skill-library-cover set`` —
    ``PUT /admin/skill-library/packages/{id}/cover``, which takes an image as an
    ``UploadFile`` rather than as JSON.
    """
    _check_admin_target(target, path, token)
    return put_multipart(target, path, fields=fields, files=files, token=token)


def admin_post(target: Target, path: str, body: object, *, token: str | None, **kwargs) -> object:
    """POST JSON to an ``/admin`` path, behind the same two refusals as the GET.

    ⚠️ Same caveat as :func:`admin_put`: this can send a WRITE, so the caller must
    have resolved ``--env`` from the command line and never from ``fm use``. The
    write on this surface is ``POST /admin/spaces/{id}/i18n/backfill``, itself a
    dry run unless the body says ``dry_run: false``.

    ``**kwargs`` forwards to :func:`post` (e.g. ``timeout`` — see
    ``evtool/admin_ops.py::BACKFILL_CHAT_TITLES_TIMEOUT`` for a caller that
    needs longer than :data:`DEFAULT_TIMEOUT`, the same reasoning
    ``evtool/benchmark.py`` documents for its own per-call overrides).
    """
    _check_admin_target(target, path, token)
    return post(target, path, body, token=token, **kwargs)


def admin_post_multipart(
    target: Target,
    path: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes]],
    token: str | None,
    timeout: int = UPLOAD_TIMEOUT,
) -> object:
    """POST ``multipart/form-data`` to an ``/admin`` path, behind the same two
    refusals as :func:`admin_get`.

    ev-absorb L5 (2026-09-07): the roster import/reconcile endpoints are the first
    ``/admin`` writes that take an uploaded file rather than a JSON body — every
    prior admin write (``i18n-backfill``, ``opendata refresh``, ``space plan``,
    ``fm seed``) sends JSON. Behind the identical ``_check_admin_target`` guard as
    every other ``admin_*`` function; only the encoding differs, same relationship
    :func:`post_multipart` has to :func:`post`.
    """
    _check_admin_target(target, path, token)
    return post_multipart(target, path, fields=fields, files=files, token=token, timeout=timeout)


def admin_post_download(
    target: Target, path: str, *, token: str | None, timeout: int = UPLOAD_TIMEOUT
) -> bytes:
    """POST (empty body) to an ``/admin`` path and return the RAW response bytes. For an
    operator export that the server audits as an action (``POST
    /admin/skill-library/packages/{id}/export``): a zip cannot go through
    :func:`_send`, which decodes the body as UTF-8 JSON. Same guard as every
    ``admin_*``."""
    _check_admin_target(target, path, token)
    request = urllib.request.Request(f"{target.api_base}{path}", data=b"", method="POST")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise _translate(exc, target, path) from None
    except urllib.error.URLError as exc:
        raise ApiError(_unreachable(target, exc)) from None


def admin_patch(target: Target, path: str, body: object, *, token: str | None) -> object:
    """PATCH JSON to an ``/admin`` path, behind the same two refusals as the GET.

    ⚠️ Same caveat as :func:`admin_put`: every call through here is a WRITE (``ev
    users set``, ``fm spaces rename``), so the caller must have resolved ``--env``
    from the command line and never from ``fm use``.
    """
    _check_admin_target(target, path, token)
    return patch(target, path, body, token=token)


def admin_delete(
    target: Target, path: str, *, token: str | None, params: dict[str, str] | None = None
) -> object:
    """DELETE an ``/admin`` path, behind the same two refusals as the GET.

    ⚠️ Same caveat as :func:`admin_put`: every call through here is a WRITE (``ev
    users remove-space``, ``fm spaces delete``), so the caller must have resolved
    ``--env`` from the command line and never from ``fm use``. ``params`` exists for
    ``DELETE /admin/spaces/{id}?confirm=true`` — the one route on this surface that
    reads a query-string confirmation rather than a body.
    """
    _check_admin_target(target, path, token)
    if params:
        url_path = f"{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    else:
        url_path = path
    return delete(target, url_path, token=token)


class BulkRefused(ApiError):
    """The bulk endpoint refused a batch and told us why, per op.

    Carries the parsed report rather than a string, because the refusal IS the
    product here: `refusals[].index` names the offending op and `reason` is the
    machine-readable code a caller branches on
    (`routes/admin_data.py` — "One status code with reason codes").
    """

    def __init__(self, message: str, payload: dict):
        super().__init__(message)
        self.payload = payload


class Misdirected(ApiError):
    """421 — this space lives in another region. NEVER followed automatically."""

    def __init__(self, message: str, retry_at: str | None):
        super().__init__(message)
        self.retry_at = retry_at


def _send(
    target: Target,
    path: str,
    data: bytes,
    *,
    method: str,
    content_type: str,
    token: str | None,
    timeout: int,
) -> object:
    """Send a body to ``path`` and return the decoded response.

    The one place a WRITE is transmitted to this API — :func:`post` (JSON) and
    :func:`put_bytes` / :func:`patch` / :func:`delete` differ only in HOW the body
    is encoded, not in how the request is sent or how its errors are read. A
    second copy of the error translation below is exactly the kind of drift
    ``_build_request`` above was extracted to prevent for GET, one write verb over.

    Two statuses get their own exception rather than the generic translation,
    because both carry something the operator must see:

    **400 ``bulk-refused``** carries the full per-op report. Flattening it into a
    message would throw away the index and reason that say which op was wrong.
    Only the bulk endpoint ever answers this way, so the check is a no-op for any
    other path.

    **421** is the region registry saying the space lives elsewhere
    (``services/space_registry.py:412``). It is raised, never followed: a redirect
    is the server telling you your belief about WHERE was wrong, and that is the
    one belief a write must not be wrong about. Following it would turn a wrong
    guess into a successful write against a database the operator did not name.
    This applies to any project-scoped write, not only the bulk endpoint — a
    resumable-upload chunk sent to a misresolved region can answer it too.
    """
    check_api_base(target.api_base)

    request = urllib.request.Request(f"{target.api_base}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", content_type)
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode()
        try:
            payload = json.loads(raw_error)
        except json.JSONDecodeError:
            payload = {}

        if exc.code == 400 and isinstance(payload, dict) and (
            payload.get("error") == "bulk-refused" or "refusals" in payload
        ):
            raise BulkRefused(
                f"the batch was refused by {target.name} and NOTHING was applied.",
                payload,
            ) from None

        if exc.code == 421:
            retry_at = payload.get("api_base") if isinstance(payload, dict) else None
            raise Misdirected(
                f"421 from {target.name}: this space is resident in another region"
                + (f" ({retry_at})" if retry_at else "")
                + ".\n"
                "fm does not follow a region redirect into a write. A redirect means "
                "your belief about WHERE was wrong, which is the one belief a write "
                "must not be wrong about — following it would write a database you "
                "did not name.\n"
                "→ Re-run with the --env that owns this space, after checking "
                "`fm spaces --host evane` for its region.",
                retry_at,
            ) from None

        raise _translate_body(exc, payload, target, path) from None
    except urllib.error.URLError as exc:
        raise ApiError(_unreachable(target, exc)) from None

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def post(
    target: Target,
    path: str,
    body: object,
    *,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """POST JSON to ``path`` and return the decoded body. See :func:`_send`."""
    return _send(
        target,
        path,
        json.dumps(body).encode(),
        method="POST",
        content_type="application/json",
        token=token,
        timeout=timeout,
    )


def encode_multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes]]
) -> tuple[bytes, str]:
    """Encode ``fields`` + ``files`` as ``multipart/form-data``.

    Returns ``(body, content_type)``. Stdlib only — there is no ``requests`` here
    and there must not be.

    ⚠️ THE BOUNDARY IS VERIFIED ABSENT FROM THE BODY, NOT ASSUMED ABSENT. A
    boundary that also occurs inside the uploaded bytes truncates the upload at
    that point, and the server then receives a SHORTER, STRUCTURALLY VALID
    request — the failure does not raise here and does not 400 there; it lands as
    a bundle missing its tail. That is this repo's dominant bug shape (a report of
    a correct write, over a write that was not correct), so the check is an
    assertion rather than a comment about how unlikely a 128-bit collision is.
    """
    for _ in range(4):
        boundary = "----evbnd" + uuid.uuid4().hex + uuid.uuid4().hex
        marker = boundary.encode()
        if not any(marker in blob for _, blob in files.values()):
            break
    else:  # pragma: no cover - four independent 128-bit collisions
        raise ApiError(
            "could not choose a multipart boundary absent from the payload."
        )

    out = bytearray()
    for name, value in fields.items():
        out += b"--" + marker + b"\r\n"
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode() + b"\r\n"
    for name, (filename, blob) in files.items():
        out += b"--" + marker + b"\r\n"
        out += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        ).encode()
        out += b"Content-Type: application/octet-stream\r\n\r\n"
        out += blob + b"\r\n"
    out += b"--" + marker + b"--\r\n"
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def post_form(
    target: Target,
    path: str,
    fields: dict[str, str],
    *,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """POST ``application/x-www-form-urlencoded`` — the OAuth token endpoint.

    OAuth 2.1 specifies form encoding at ``/token`` (RFC 6749 §4.1.3), not JSON, and
    the MCP SDK's handler reads ``request.form()``. So this cannot go through
    :func:`post`. It goes through :func:`_send` like every other write, which is the
    point of it being here rather than a bare ``urlopen`` inside
    ``evtool/login.py``: the host allow-list, the 410/404/403 translation and the
    421 refusal are the SAME code as for every other call, and a second transport
    written beside them is exactly the drift ``_build_request`` was extracted to
    prevent.
    """
    return _send(
        target,
        path,
        urllib.parse.urlencode(fields).encode(),
        method="POST",
        content_type="application/x-www-form-urlencoded",
        token=token,
        timeout=timeout,
    )


def post_multipart(
    target: Target,
    path: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes]],
    token: str | None = None,
    timeout: int = UPLOAD_TIMEOUT,
) -> object:
    """POST ``multipart/form-data`` to ``path`` and return the decoded body.

    Goes through :func:`_send` like every other write, so the 421 region-redirect
    refusal and the ``bulk-refused`` unpacking are the SAME code here as in
    :func:`post` — not a second spelling of them. The branch that first added this
    transport arrived with its own ``write_http_error`` helper extracted for the
    purpose; ``_send`` had landed on main meanwhile with the identical intent, and
    keeping both is precisely the duplicated-abstraction merge the project rules records.
    Only the ENCODING differs, which is all this function does.

    The default timeout is :data:`UPLOAD_TIMEOUT` rather than
    :data:`DEFAULT_TIMEOUT`: this carries a whole-workspace archive, and 30s is a
    limit the bundle would hit on size rather than on anything being wrong.
    """
    data, content_type = encode_multipart(fields, files)
    return _send(
        target,
        path,
        data,
        method="POST",
        content_type=content_type,
        token=token,
        timeout=timeout,
    )


def put_multipart(
    target: Target,
    path: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes]],
    token: str | None = None,
    timeout: int = UPLOAD_TIMEOUT,
) -> object:
    """PUT ``multipart/form-data`` to ``path`` and return the decoded body.

    The verb, and only the verb, is what separates this from
    :func:`post_multipart` — same encoder, same :func:`_send`, so the 421
    region-redirect refusal and the ``bulk-refused`` unpacking are one
    implementation and not two spellings of one (the merge hazard that
    function's own docstring records). It exists because FastAPI routes an
    image REPLACEMENT as PUT: ``PUT /admin/skill-library/packages/{id}/cover``
    takes an ``UploadFile``, so neither :func:`admin_put` (JSON) nor
    :func:`put_bytes` (a raw octet-stream, for resumable-upload chunks) can
    reach it.
    """
    data, content_type = encode_multipart(fields, files)
    return _send(
        target,
        path,
        data,
        method="PUT",
        content_type=content_type,
        token=token,
        timeout=timeout,
    )


def patch(
    target: Target,
    path: str,
    body: object,
    *,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """PATCH JSON to ``path`` and return the decoded body. See :func:`_send`."""
    return _send(
        target,
        path,
        json.dumps(body).encode(),
        method="PATCH",
        content_type="application/json",
        token=token,
        timeout=timeout,
    )


def put(
    target: Target,
    path: str,
    body: object,
    *,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """PUT JSON to ``path`` and return the decoded body. See :func:`_send`.

    Distinct from :func:`put_bytes`, which sends an unparsed byte stream for a
    resumable-upload chunk. Same verb, different content type; keeping them as one
    function with a mode flag would put the upload path and a JSON admin write on
    one branch, which is not a saving worth making.
    """
    return _send(
        target,
        path,
        json.dumps(body).encode(),
        method="PUT",
        content_type="application/json",
        token=token,
        timeout=timeout,
    )


def delete(
    target: Target,
    path: str,
    *,
    token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """DELETE ``path`` and return the decoded body. See :func:`_send`."""
    return _send(
        target,
        path,
        b"",
        method="DELETE",
        content_type="application/json",
        token=token,
        timeout=timeout,
    )


def put_bytes(
    target: Target,
    path: str,
    data: bytes,
    *,
    token: str | None = None,
    content_type: str = "application/octet-stream",
    timeout: int = DEFAULT_TIMEOUT,
) -> object:
    """PUT raw bytes to ``path`` and return the decoded JSON body.

    For a resumable-upload chunk (``routes/upload_sessions.py::put_upload_chunk``),
    which reads the request body as an unparsed byte stream — the SAME reason
    :func:`post` sends JSON rather than multipart: no multipart/form-data encoder
    exists in the stdlib, and ``ev`` may add no runtime dependency to get one. The
    chunk route takes raw bytes precisely because it does not need one either.
    """
    return _send(
        target,
        path,
        data,
        method="PUT",
        content_type=content_type,
        token=token,
        timeout=timeout,
    )


def _translate_body(exc, payload: dict, target: Target, path: str) -> ApiError:
    """`_translate`, for a caller that already consumed the error body."""
    error = _translated_body(exc, payload, target, path)
    error.status = exc.code
    return error


def _translated_body(exc, payload: dict, target: Target, path: str) -> ApiError:
    """`_translate`, for a caller that already consumed the error body.

    `HTTPError.read()` can only be called once, so POST parses the body itself and
    hands it here rather than letting `_translate` read an exhausted stream — which
    would silently produce an empty payload and a message missing its reason.
    """
    code = payload.get("error") if isinstance(payload, dict) else None
    detail = payload.get("message") if isinstance(payload, dict) else None

    if exc.code == 401 or code == "not-authenticated":
        return NotAuthenticated(
            f"401 from {target.name}: no valid session.\n"
            f"→ `fm login --host {target.name}`"
        )
    if code == "cli-admin-scope-required":
        return _cli_scope_refusal(target, detail)

    if exc.code == 403 or code == "platform-admin-required":
        # ⚠️ SIBLING: `_translated`'s 403 branch answers for the SAME gate, and the
        # 404 branch below already carries this warning for the same reason.
        # This message IS that failure, realised: the GET sibling was corrected and
        # this one was not, so it went on describing `require_admin_opaque` after the
        # endpoint had stopped using it. If you change either 403 message, change the
        # other.
        return ApiError(
            f"403 from {target.name} on {path}: this needs User.is_platform_admin "
            "on your user row.\n"
            "The gate is require_platform_admin_for_space (routes/admin_data.py): "
            "SITE-WIDE platform admin, re-checked on every call. It never consults "
            "the project, so being OWNER or ADMIN of this space — or a member of it "
            "at all — is neither necessary nor sufficient.\n"
            "→ `fm spaces` reads /admin/spaces behind the identical flag, so a "
            "working `fm spaces` is what proves you have it. `fm whoami` CANNOT: it "
            "decodes the stored token locally and never asks the server.\n"
            "⚠️ Do NOT retry THIS command with `--host evane`. That flag is correct "
            "for `fm spaces` (/admin is served from Global only) and wrong here: this "
            "endpoint is region-scoped, and a bulk WRITE aimed at the wrong region is "
            "the one thing it exists to refuse (421, naming where to retry).\n"
            "The flag is granted by another platform admin via "
            "PATCH /admin/users/{id} (routes/admin.py)."
        )
    if exc.code == 404:
        # Same wording and same reason as the GET path above. This branch used to
        # name a BRANCH and a PR NUMBER — "ships with deploy/admin-bulk-data-api
        # (PR #232) and is NOT deployed yet" — which was a prediction about a
        # deployment's future made from a string inside a client. PR #232 merged on
        # 2026-08-27 (azure/main 6840b28), so half of that sentence was already
        # false while the other half stayed unverifiable from here.
        #
        # ⚠️ IT WAS MISSED WHEN THE GET PATH WAS FIXED. _translated and
        # _translated_body are siblings and the pattern lived in both; removing it
        # from one is how a defect survives its own fix. If you change one of these
        # two messages, change the other.
        #
        # 2026-09-06: the SAME miss, the other direction — `_translated` learned
        # to check `code` first (a domain 404 like `space-not-found` carries a
        # structured body; a route that was never registered does not) and this
        # branch did not, so `fm users add-space` against a valid-looking but
        # drifted space id showed "may not be deployed yet" for what was actually
        # `space-not-found`. Fixed in both at once this time.
        if code:
            return ApiError(f"404 from {target.name} on {path}: {detail or code}")
        return ApiError(
            f"404 from {target.name}: {path} is not served there.\n"
            "Two different things look identical here and fm cannot tell them "
            "apart: the path may be wrong, or it may be right and simply not in "
            "the image this region is running.\n"
            f"→ `fm instance --host {target.name}` reports the deployed build and "
            "migration revision. Compare it with the code you are holding; that "
            "MEASURES the answer, where fm could only guess it."
        )
    return ApiError(f"{exc.code} from {target.name} on {path}: {detail or code or exc.reason}")
