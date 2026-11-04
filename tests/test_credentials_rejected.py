"""Test the narrowly observed legacy credential rejection response."""

import asyncio
from datetime import datetime, timedelta
import json

from aiohttp import ClientResponseError, ClientSession
import pytest

from aioflo.api import API
from aioflo.errors import CredentialsRejectedError, RequestError

from .test_token_recovery import ready_api

LEGACY_HOST = "api.meetflo.com"
LEGACY_PATH = "/api/v1/users/auth"
REJECTION = {"error": True, "message": "Invalid username/password."}


@pytest.mark.parametrize("state", ["initial", "expired", "rejected"])
async def test_legacy_credentials_rejected(aresponses, state, caplog):
    """Classify the proven signal through login and recovery without state changes."""
    api = (
        API("synthetic-user", "synthetic-password")
        if state == "initial"
        else ready_api(False)
    )
    if state == "expired":
        api._token_expiration = datetime.now() - timedelta(seconds=1)
    if state == "rejected":
        aresponses.add(
            "api-gw.meetflo.com",
            "/api/v2/test",
            "get",
            aresponses.Response(status=401),
        )
    before = (api._token, api._refresh_token, api._token_expiration, api._user_id)
    aresponses.add(
        LEGACY_HOST,
        LEGACY_PATH,
        "post",
        aresponses.Response(text=json.dumps(REJECTION), status=400),
    )
    async with ClientSession() as session:
        api._session = session
        with pytest.raises(RequestError) as raised:
            await api._request("get", "https://api-gw.meetflo.com/api/v2/test")
        assert type(raised.value) is CredentialsRejectedError
        assert isinstance(raised.value.__cause__, ClientResponseError)
        assert raised.value.__cause__.status == 400
        assert str(raised.value) == "Legacy login credentials were rejected"
        assert not session.closed
    assert (
        api._token,
        api._refresh_token,
        api._token_expiration,
        api._user_id,
    ) == before
    assert api._token_generation == 0
    assert api._username not in caplog.text
    assert api._password not in caplog.text
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, b""),
        (400, b"{"),
        (400, b"\xff"),
        (400, b"null"),
        (400, b"[]"),
        (400, json.dumps({"token": REJECTION}).encode()),
        (400, json.dumps({"message": REJECTION["message"]}).encode()),
        (400, json.dumps({"error": True}).encode()),
        (400, json.dumps({**REJECTION, "error": 1}).encode()),
        (400, json.dumps({**REJECTION, "error": "true"}).encode()),
        (400, json.dumps({**REJECTION, "error": False}).encode()),
        (400, json.dumps({**REJECTION, "message": "Invalid date range."}).encode()),
        (
            400,
            json.dumps({**REJECTION, "message": "Invalid username/password"}).encode(),
        ),
        (400, json.dumps({**REJECTION, "message": None}).encode()),
        (401, json.dumps(REJECTION).encode()),
        (403, json.dumps(REJECTION).encode()),
        (429, json.dumps(REJECTION).encode()),
        (500, json.dumps(REJECTION).encode()),
    ],
)
async def test_unrecognized_legacy_login_error(aresponses, status, body):
    """Other statuses, envelopes and malformed bodies retain the HTTP error cause."""
    aresponses.add(
        LEGACY_HOST,
        LEGACY_PATH,
        "post",
        aresponses.Response(body=body, status=status),
    )
    async with ClientSession() as session:
        api = API("synthetic-user", "synthetic-password", session=session)
        with pytest.raises(RequestError) as raised:
            await api.async_authenticate()
    assert type(raised.value) is RequestError
    assert isinstance(raised.value.__cause__, ClientResponseError)
    assert raised.value.__cause__.status == status
    assert api._token is None
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize(
    "body", [REJECTION, {"error": True, "message": "Invalid date range."}]
)
async def test_consumption_400_remains_request_error(aresponses, body):
    """A consumption-date failure cannot be mistaken for rejected credentials."""
    aresponses.add(
        "api-gw.meetflo.com",
        "/api/v2/water/consumption",
        "get",
        aresponses.Response(text=json.dumps(body), status=400),
    )
    api = ready_api(False)
    async with ClientSession() as session:
        api._session = session
        with pytest.raises(RequestError) as raised:
            await api.water.get_consumption_info(
                "location", datetime(2026, 1, 2), datetime(2026, 1, 1)
            )
    assert type(raised.value) is RequestError
    assert raised.value.__cause__.status == 400
    assert not api._token_rejected
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize("refresh", [False, True])
async def test_sso_rejection_is_not_classified(aresponses, refresh):
    """The legacy-looking body is not a verified SSO credential rejection signal."""
    for _ in range(2 if refresh else 1):
        aresponses.add(
            "4j1gkf0vji.execute-api.us-east-2.amazonaws.com",
            "/prod/v1/oauth2/token",
            "post",
            aresponses.Response(text=json.dumps(REJECTION), status=400),
        )
    api = (
        ready_api()
        if refresh
        else API("synthetic-user", "synthetic-password", use_sso=True)
    )
    api._token_rejected = True
    before = (api._token, api._refresh_token, api._token_expiration)
    async with ClientSession() as session:
        api._session = session
        with pytest.raises(RequestError) as raised:
            await api._request("get", "https://api-gw.meetflo.com/api/v2/test")
    assert type(raised.value) is RequestError
    assert raised.value.__cause__.status == 400
    assert (api._token, api._refresh_token, api._token_expiration) == before
    aresponses.assert_plan_strictly_followed()


async def test_credentials_rejection_is_shared(aresponses):
    """Concurrent recoveries share the classified failure and preserve credentials."""
    api = ready_api(False)
    api._token_rejected = True
    entered, release = asyncio.Event(), asyncio.Event()
    before = (api._token, api._refresh_token, api._token_expiration)

    async def reject(request):
        entered.set()
        await release.wait()
        return aresponses.Response(text=json.dumps(REJECTION), status=400)

    aresponses.add(LEGACY_HOST, LEGACY_PATH, "post", reject)
    async with ClientSession() as session, asyncio.timeout(5):
        api._session = session
        tasks = [
            asyncio.create_task(
                api._request("get", "https://api-gw.meetflo.com/api/v2/test")
            )
            for _ in range(4)
        ]
        try:
            await entered.wait()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert isinstance(results[0], CredentialsRejectedError)
            assert all(result is results[0] for result in results)
            assert results[0].__cause__.status == 400
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert (api._token, api._refresh_token, api._token_expiration) == before
    aresponses.assert_plan_strictly_followed()
