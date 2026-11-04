"""Deterministic token lifecycle and transport regression tests."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from aiohttp import ClientConnectionError, ClientResponseError, ClientSession
from multidict import CIMultiDict
import pytest

from aioflo.api import API, SSO_TOKEN_URL
from aioflo.errors import FloError, RequestError

RESOURCE = "https://api-gw.meetflo.com/api/v2/test"


def http_error(status):
    """Return the transport's wrapped status error."""
    error = RequestError("request failed")
    error.__cause__ = ClientResponseError(None, (), status=status)
    return error


def ready_api(sso=True):
    """Create an authenticated client without network activity."""
    api = API("username", "password", use_sso=sso)
    api._token = "old"
    api._refresh_token = "refresh"
    api._token_expiration = datetime.now() + timedelta(hours=1)
    api._user_id = "user"
    return api


def new_token(sso):
    """Return a valid authentication response for the selected mode."""
    if sso:
        return {"access_token": "new", "expires_in": 3600}
    return {
        "token": "new",
        "tokenPayload": {
            "timestamp": datetime.now().timestamp(),
            "user": {"user_id": "user"},
        },
        "tokenExpiration": 3600,
    }


@pytest.mark.parametrize("sso", [True, False])
@pytest.mark.parametrize("state", ["expired", "missing", "rejected"])
async def test_concurrent_recovery(monkeypatch, sso, state):
    """All callers share a refresh and no resource request is anonymous."""
    api = ready_api(sso)
    if state == "expired":
        api._token_expiration = datetime.now() - timedelta(seconds=1)
    elif state == "missing":
        api._token = None
    else:
        api._token_rejected = True
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(method, url, *, token, **kwargs):
        calls.append((url, token))
        if url != RESOURCE:
            assert token is None
            entered.set()
            await release.wait()
            return new_token(sso)
        assert token == "new"
        return {"ok": True}

    monkeypatch.setattr(api, "_send_request", send)
    tasks = [asyncio.create_task(api._request("get", RESOURCE)) for _ in range(8)]
    await entered.wait()
    assert len(calls) == 1
    release.set()
    assert await asyncio.gather(*tasks) == [{"ok": True}] * 8
    assert len(calls) == 9


@pytest.mark.parametrize("sso", [True, False])
async def test_concurrent_and_late_401(monkeypatch, sso):
    """A delayed old-generation rejection reuses the completed refresh."""
    api = ready_api(sso)
    both_sent, refresh_entered, refresh_release, late_release = (
        asyncio.Event() for _ in range(4)
    )
    old_calls = 0
    auth_calls = 0

    async def send(method, url, *, token, **kwargs):
        nonlocal old_calls, auth_calls
        if url != RESOURCE:
            auth_calls += 1
            refresh_entered.set()
            await refresh_release.wait()
            return new_token(sso)
        if token == "old":
            old_calls += 1
            if old_calls == 2:
                both_sent.set()
                await late_release.wait()
            else:
                await both_sent.wait()
            raise http_error(401)
        assert token == "new"
        return {"ok": True}

    monkeypatch.setattr(api, "_send_request", send)
    first = asyncio.create_task(api._request("get", RESOURCE))
    second = asyncio.create_task(api._request("get", RESOURCE))
    await refresh_entered.wait()
    refresh_release.set()
    assert await first == {"ok": True}
    late_release.set()
    assert await second == {"ok": True}
    assert auth_calls == 1
    assert not api._token_rejected


@pytest.mark.parametrize("sso", [True, False])
async def test_persistent_401_bounded(monkeypatch, sso):
    """One request performs at most one authentication retry in either mode."""
    api = ready_api(sso)
    calls = []

    async def send(method, url, *, token, **kwargs):
        calls.append(url)
        if url != RESOURCE:
            return new_token(sso)
        raise http_error(401)

    monkeypatch.setattr(api, "_send_request", send)
    with pytest.raises(RequestError):
        await api._request("post", RESOURCE, json={"value": 1})
    assert len(calls) == 3
    assert api._token_rejected


async def test_waiter_cancellation_does_not_cancel_refresh(monkeypatch):
    """Cancellation leaves peers and the refresh operation running."""
    api = ready_api()
    api._token = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(method, url, *, token, **kwargs):
        if url == SSO_TOKEN_URL:
            entered.set()
            await release.wait()
            return new_token(True)
        assert token == "new"
        return {}

    monkeypatch.setattr(api, "_send_request", send)
    cancelled = asyncio.create_task(api._request("get", RESOURCE))
    await entered.wait()
    survivor = asyncio.create_task(api._request("get", RESOURCE))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not api._auth_task.cancelled()
    release.set()
    await survivor
    assert api._token == "new"


@pytest.mark.parametrize(
    "failure", [http_error(500), http_error(429), RequestError("timeout")]
)
async def test_shared_failure_and_next_call_recovers(monkeypatch, failure):
    """A failed refresh reaches all waiters and leaves recovery possible."""
    api = ready_api()
    api._token_rejected = True
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(method, url, *, token, **kwargs):
        calls.append(url)
        if url == SSO_TOKEN_URL:
            entered.set()
            await release.wait()
            if len(calls) == 1:
                raise failure
            return new_token(True)
        assert token == "new"
        return {}

    monkeypatch.setattr(api, "_send_request", send)
    tasks = [asyncio.create_task(api._request("get", RESOURCE)) for _ in range(5)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(result is failure for result in results)
    assert len(calls) == 1
    assert api._token == "old"
    assert api._refresh_token == "refresh"
    await api._request("get", RESOURCE)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"access_token": ""},
        {"access_token": "new", "expires_in": "bad"},
        {"access_token": "new", "refresh_token": 7},
        {"access_token": "new", "expires_in": 0},
    ],
)
async def test_invalid_sso_response_is_atomic(monkeypatch, bad):
    """Malformed successful responses cannot partially replace credentials."""
    api = ready_api()
    before = (api._token, api._refresh_token, api._token_expiration)
    monkeypatch.setattr(api, "_async_sso_token_request", AsyncMock(return_value=bad))
    with pytest.raises(RequestError):
        await api.async_authenticate()
    assert (api._token, api._refresh_token, api._token_expiration) == before


async def test_user_lookup_failure_is_atomic_and_nonrecursive(monkeypatch):
    """A failed first SSO user lookup publishes nothing and does not recurse."""
    api = API("username", "password", use_sso=True)
    send = AsyncMock(side_effect=[new_token(True), http_error(401)])
    monkeypatch.setattr(api, "_send_request", send)
    with pytest.raises(RequestError):
        await asyncio.wait_for(api.async_authenticate(), 1)
    assert send.await_count == 2
    assert api._token is api._refresh_token is api._token_expiration is api.user is None


class FakeSession:
    """Small transport double exposing session ownership and sent headers."""

    def __init__(self, failure=None, headers=None):
        self.closed = False
        self.headers = CIMultiDict(headers or {})
        self.auth = None
        self.failure = failure
        self.requests = []

    def request(self, method, url, **kwargs):
        """Capture requests, optionally failing before a response exists."""
        self.requests.append(kwargs)
        if self.failure:
            raise self.failure
        return self

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, *args):
        """Exit the response context."""

    def raise_for_status(self):
        """Represent a successful HTTP response."""

    async def json(self, **kwargs):
        """Return an empty resource object."""
        return {}

    async def close(self):
        """Record closure."""
        self.closed = True


@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize(
    "failure", [None, TimeoutError(), ClientConnectionError(), asyncio.CancelledError()]
)
async def test_transport_errors_and_session_ownership(monkeypatch, owned, failure):
    """Only owned sessions close; timeouts wrap and cancellations propagate."""
    api = ready_api()
    session = FakeSession(failure)
    if owned:
        monkeypatch.setattr("aioflo.api.ClientSession", lambda **kwargs: session)
    else:
        api._session = session
    if failure is None:
        await api._send_request("get", RESOURCE, token="new")
    else:
        expected = (
            asyncio.CancelledError
            if isinstance(failure, asyncio.CancelledError)
            else RequestError
        )
        with pytest.raises(expected):
            await api._send_request("get", RESOURCE, token="new")
    assert session.closed is owned


async def test_headers_are_copied_and_anonymous_session_defaults_isolated(monkeypatch):
    """Mixed-case credentials cannot leak to auth or mutate caller/session headers."""
    api = ready_api()
    borrowed = FakeSession(headers={"authorization": "session-secret"})
    owned = FakeSession()
    api._session = borrowed
    monkeypatch.setattr("aioflo.api.ClientSession", lambda **kwargs: owned)
    headers = CIMultiDict(
        [
            ("authorization", "caller-secret"),
            ("Authorization", "duplicate"),
            ("X-Test", "value"),
        ]
    )
    original = list(headers.items())
    await api._send_request("post", SSO_TOKEN_URL, token=None, headers=headers)
    assert "Authorization" not in owned.requests[0]["headers"]
    assert owned.closed and not borrowed.closed
    await api._send_request("get", RESOURCE, token="new", headers=headers)
    assert borrowed.requests[0]["headers"].getall("Authorization") == ["Bearer new"]
    assert list(headers.items()) == original
    assert borrowed.headers["Authorization"] == "session-secret"


async def test_simultaneous_401s_share_refresh(monkeypatch):
    """Rejections received while authentication runs wait for that same task."""
    api = ready_api()
    all_sent, refresh_started, release = (asyncio.Event() for _ in range(3))
    old_requests = 0
    auth_requests = 0

    async def send(method, url, *, token, **kwargs):
        nonlocal old_requests, auth_requests
        if url == SSO_TOKEN_URL:
            auth_requests += 1
            refresh_started.set()
            await release.wait()
            return new_token(True)
        if token == "old":
            old_requests += 1
            if old_requests == 6:
                all_sent.set()
            await all_sent.wait()
            raise http_error(401)
        assert token == "new"
        return {}

    monkeypatch.setattr(api, "_send_request", send)
    tasks = [asyncio.create_task(api._request("get", RESOURCE)) for _ in range(6)]
    await refresh_started.wait()
    release.set()
    await asyncio.gather(*tasks)
    assert auth_requests == 1


async def test_abandoned_refresh_finishes_and_consumes_failure(monkeypatch):
    """An auth task outliving its only caller finishes without an unhandled error."""
    api = ready_api()
    api._token = None
    entered, release, finished = (asyncio.Event() for _ in range(3))

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RequestError("failed")

    monkeypatch.setattr(api, "_send_request", send)
    waiter = asyncio.create_task(api._request("get", RESOURCE))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    task = api._auth_task
    task.add_done_callback(lambda _: finished.set())
    release.set()
    await finished.wait()
    assert task.done() and not task.cancelled()
    # CPython sets this false when exception() retrieves the abandoned failure.
    assert not task._log_traceback
    assert api._token is None


@pytest.mark.parametrize("sso", [True, False])
async def test_auth_endpoint_rejection_never_recurses(monkeypatch, sso):
    """A rejected credential login sends one auth request and no resource call."""
    api = API("username", "password", use_sso=sso)
    send = AsyncMock(side_effect=http_error(401))
    monkeypatch.setattr(api, "_send_request", send)
    with pytest.raises(RequestError):
        await asyncio.wait_for(api._request("get", RESOURCE), 1)
    assert send.await_count == 1
    assert api._token is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"token": "", "tokenPayload": {}},
        {**new_token(False), "token": ""},
        {
            "token": "new",
            "tokenPayload": {"timestamp": 0, "user": {"user_id": "user"}},
            "tokenExpiration": 1,
        },
    ],
)
async def test_invalid_legacy_response_is_atomic(monkeypatch, bad):
    """A malformed legacy login preserves the prior complete state."""
    api = ready_api(False)
    before = (api._token, api._token_expiration, api._user_id)
    monkeypatch.setattr(api, "_send_request", AsyncMock(return_value=bad))
    with pytest.raises(RequestError):
        await api.async_authenticate()
    assert (api._token, api._token_expiration, api._user_id) == before


async def test_explicit_authentication_shares_refresh(monkeypatch):
    """Explicit login joins an ongoing recovery instead of racing token writes."""
    api = ready_api()
    api._token = None
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def send(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return new_token(True)

    monkeypatch.setattr(api, "_send_request", send)
    recovery = asyncio.create_task(api._ensure_token())
    await entered.wait()
    explicit = asyncio.create_task(api.async_authenticate())
    release.set()
    await asyncio.gather(recovery, explicit)
    assert calls == 1


async def test_full_login_does_not_keep_previous_refresh_token(monkeypatch):
    """Only refresh-grant responses may retain an omitted refresh token."""
    api = ready_api()
    monkeypatch.setattr(api, "_send_request", AsyncMock(return_value=new_token(True)))
    await api.async_authenticate()
    assert api._token == "new"
    assert api._refresh_token is None


@pytest.mark.parametrize("token", [None, "new"])
async def test_session_basic_auth_does_not_leak_or_conflict(monkeypatch, token):
    """Session BasicAuth is isolated for both auth and resource requests."""
    api = ready_api()
    borrowed, owned = FakeSession(), FakeSession()
    borrowed.auth = ("other-user", "other-password")
    api._session = borrowed
    monkeypatch.setattr("aioflo.api.ClientSession", lambda **kwargs: owned)
    await api._send_request("get", RESOURCE, token=token)
    assert not borrowed.requests and not borrowed.closed
    assert owned.closed
    assert owned.requests[0]["headers"].get("Authorization") == (
        f"Bearer {token}" if token else None
    )


async def test_short_lived_sso_token_does_not_recurse(monkeypatch):
    """Expiry margin leaves a positive usable lifetime for short-lived tokens."""
    api = API("username", "password", use_sso=True)
    send = AsyncMock(
        side_effect=[{"access_token": "new", "expires_in": 30}, {"id": "user"}]
    )
    monkeypatch.setattr(api, "_send_request", send)
    await asyncio.wait_for(api.async_authenticate(), 1)
    assert (
        datetime.now() < api._token_expiration < datetime.now() + timedelta(seconds=30)
    )
    assert send.await_count == 2


@pytest.mark.parametrize("sso", [True, False])
async def test_invalid_utf8_token_response_is_request_error(aresponses, sso):
    """Wrap decoding failures without fallback, resource requests, or state changes."""
    api = ready_api(sso)
    api._token_expiration = datetime.now() - timedelta(seconds=1)
    before = (api._token, api._refresh_token, api._token_expiration, api._user_id)
    host, path = (
        ("4j1gkf0vji.execute-api.us-east-2.amazonaws.com", "/prod/v1/oauth2/token")
        if sso
        else ("api.meetflo.com", "/api/v1/users/auth")
    )
    aresponses.add(
        host,
        path,
        "post",
        aresponses.Response(
            body=b"\xff", status=200, headers={"Content-Type": "application/json"}
        ),
    )
    async with ClientSession() as session:
        api._session = session
        with pytest.raises(RequestError) as raised:
            await api._request("get", RESOURCE)
        assert isinstance(raised.value, FloError)
        assert isinstance(raised.value.__cause__, UnicodeDecodeError)
        assert not session.closed
    assert (
        api._token,
        api._refresh_token,
        api._token_expiration,
        api._user_id,
    ) == before
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize("sso", [True, False])
@pytest.mark.parametrize("interleave", [False, True])
async def test_staggered_failed_recovery(monkeypatch, sso, interleave):
    """Late 401s share their first failed recovery, even across a fresh retry."""
    api = ready_api(sso)
    before = (api._token, api._refresh_token, api._token_expiration)
    releases = [asyncio.Event() for _ in range(4)]
    all_sent = asyncio.Event()
    failure = http_error(503 if sso else 401)
    auth_calls = 0
    old_calls = 0

    async def send(method, url, *, token, **kwargs):
        nonlocal auth_calls, old_calls
        if url != RESOURCE:
            auth_calls += 1
            assert token is None
            if sso:
                assert kwargs["json"]["grant_type"] == "refresh_token"
            if auth_calls == 1:
                raise failure
            return new_token(sso)
        if token == "old":
            index = old_calls
            old_calls += 1
            if old_calls == 4:
                all_sent.set()
            await releases[index].wait()
            raise http_error(401)
        assert token == "new"
        return {"ok": True}

    monkeypatch.setattr(api, "_send_request", send)
    async with asyncio.timeout(5):
        tasks = [asyncio.create_task(api._request("get", RESOURCE)) for _ in releases]
        try:
            await all_sent.wait()
            for index, (release, task) in enumerate(zip(releases, tasks)):
                release.set()
                with pytest.raises(RequestError) as raised:
                    await task
                assert raised.value is failure
                if index == 0:
                    assert auth_calls == 1
                    assert (
                        api._token,
                        api._refresh_token,
                        api._token_expiration,
                    ) == before
                    if interleave:
                        assert await api._request("get", RESOURCE) == {"ok": True}
            assert auth_calls == (2 if interleave else 1)
            assert old_calls == 4
            assert await api._request("get", RESOURCE) == {"ok": True}
            assert auth_calls == 2
            assert not api._token_rejected
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("bad", [[], {"token": []}])
async def test_sso_non_object_response_is_atomic(monkeypatch, bad):
    """Reject non-object token envelopes without replacing stored credentials."""
    api = ready_api()
    before = (api._token, api._refresh_token, api._token_expiration)
    monkeypatch.setattr(api, "_send_request", AsyncMock(return_value=bad))
    with pytest.raises(RequestError, match="Invalid SSO token response"):
        await api.async_authenticate()
    assert (api._token, api._refresh_token, api._token_expiration) == before


@pytest.mark.parametrize("me", [[], {}, {"id": ""}])
async def test_sso_missing_user_id_is_atomic(monkeypatch, me):
    """A successful HTTP lookup without a user ID cannot publish credentials."""
    api = API("username", "password", use_sso=True)
    send = AsyncMock(side_effect=[new_token(True), me])
    monkeypatch.setattr(api, "_send_request", send)
    with pytest.raises(RequestError, match="no user id"):
        await api.async_authenticate()
    assert send.await_count == 2
    assert api._token is api._refresh_token is api._token_expiration is api.user is None


async def test_sso_token_expires_during_user_lookup(monkeypatch):
    """A slow first user lookup cannot publish an already expired token."""
    api = API("username", "password", use_sso=True)
    now = datetime.now()

    class Clock:
        """Advance wall time deterministically during the lookup."""

        @staticmethod
        def now():
            """Return the controlled time."""
            return now

    async def send(method, url, **kwargs):
        nonlocal now
        if url == SSO_TOKEN_URL:
            return {"access_token": "new", "expires_in": 30}
        now += timedelta(seconds=31)
        return {"id": "user"}

    monkeypatch.setattr("aioflo.api.datetime", Clock)
    monkeypatch.setattr(api, "_send_request", send)
    with pytest.raises(RequestError, match="expired during user lookup"):
        await api.async_authenticate()
    assert api._token is api._refresh_token is api._token_expiration is api.user is None


@pytest.mark.parametrize("sso", [True, False])
async def test_late_failure_does_not_join_new_recovery(monkeypatch, sso):
    """Old responses retain their failure while a newer recovery is still running."""
    api = ready_api(sso)
    sent, late_release, entered, release = (asyncio.Event() for _ in range(4))
    failure = http_error(503 if sso else 401)
    auth_calls = 0
    old_calls = 0

    async def send(method, url, *, token, **kwargs):
        nonlocal auth_calls, old_calls
        if url != RESOURCE:
            auth_calls += 1
            if auth_calls == 1:
                raise failure
            entered.set()
            await release.wait()
            return new_token(sso)
        if token == "old":
            old_calls += 1
            if old_calls == 2:
                sent.set()
                await late_release.wait()
            else:
                await sent.wait()
            raise http_error(401)
        return {"ok": True}

    monkeypatch.setattr(api, "_send_request", send)
    async with asyncio.timeout(5):
        first = asyncio.create_task(api._request("get", RESOURCE))
        late = asyncio.create_task(api._request("get", RESOURCE))
        tasks = [first, late]
        try:
            with pytest.raises(RequestError) as raised:
                await first
            assert raised.value is failure
            fresh = asyncio.create_task(api._request("get", RESOURCE))
            tasks.append(fresh)
            await entered.wait()
            late_release.set()
            with pytest.raises(RequestError) as raised:
                await late
            assert raised.value is failure
            assert not fresh.done()
            release.set()
            assert await fresh == {"ok": True}
            assert auth_calls == 2
            assert not api._token_rejected
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_late_401_waiter_cancellation_preserves_recovery(monkeypatch):
    """Cancelling a late 401 waiter does not cancel the recovery it joined."""
    api = ready_api()
    sent, entered, release, joined = (asyncio.Event() for _ in range(4))
    old_calls = 0
    auth_calls = 0

    async def send(method, url, *, token, **kwargs):
        nonlocal old_calls, auth_calls
        if url != RESOURCE:
            auth_calls += 1
            entered.set()
            await release.wait()
            return new_token(True)
        if token == "old":
            old_calls += 1
            if old_calls == 2:
                sent.set()
                await entered.wait()
                joined.set()
            else:
                await sent.wait()
            raise http_error(401)
        return {"ok": True}

    monkeypatch.setattr(api, "_send_request", send)
    async with asyncio.timeout(5):
        survivor = asyncio.create_task(api._request("get", RESOURCE))
        cancelled = asyncio.create_task(api._request("get", RESOURCE))
        try:
            await joined.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert not api._auth_task.done()
            release.set()
            assert await survivor == {"ok": True}
            assert auth_calls == 1
        finally:
            release.set()
            survivor.cancel()
            cancelled.cancel()
            await asyncio.gather(survivor, cancelled, return_exceptions=True)
