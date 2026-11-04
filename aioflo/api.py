"""Define a base client for interacting with Flo."""

import asyncio
from datetime import datetime, timedelta
from json import JSONDecodeError
import logging
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError, ClientResponseError
from multidict import CIMultiDict

from .alarm import Alarm
from .const import API_V2_BASE
from .device import Device
from .errors import CredentialsRejectedError, RequestError
from .flodetect import Flodetect
from .location import Location
from .presence import Presence
from .user import User
from .water import Water

_LOGGER = logging.getLogger(__name__)

API_V1_BASE: str = "https://api.meetflo.com/api/v1"

# Moen SSO (Cognito). This is the auth the current Moen Smartwater app uses. The legacy
# ``users/auth`` flow above still works, so this is opt-in cover for that endpoint being
# retired rather than a fix for a live breakage.
SSO_TOKEN_URL: str = (
    "https://4j1gkf0vji.execute-api.us-east-2.amazonaws.com/prod/v1/oauth2/token"
)
SSO_CLIENT_ID: str = "6qn9pep31dglq6ed4fvlq6rp5t"
# Refresh this many seconds before the token actually expires, so a request in flight
# does not race the expiry.
SSO_EXPIRY_MARGIN: int = 60

DEFAULT_HEADER_ACCEPT: str = "application/json, text/plain, */*"
DEFAULT_HEADER_CONTENT_TYPE: str = "application/json;charset=UTF-8"
DEFAULT_HEADER_ORIGIN: str = "https://user.meetflo.com"
DEFAULT_HEADER_REFERER: str = "https://user.meetflo.com/home"
DEFAULT_HEADER_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.117 Safari/537.36"
)
DEFAULT_TIMEOUT: int = 10


class API:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Define the API object."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        session: ClientSession | None = None,
        use_sso: bool = False,
    ) -> None:
        """Initialize.

        :param use_sso: Authenticate via the Moen SSO (Cognito) flow instead of the
            legacy Flo ``users/auth`` flow.
        :type use_sso: ``bool``
        """
        self._password: str = password
        self._session: ClientSession | None = session
        self._token: str | None = None
        self._token_expiration: datetime | None = None
        self._refresh_token: str | None = None
        self._use_sso: bool = use_sso
        self._user_id: str | None = None
        self._username: str = username
        self._auth_task: asyncio.Task | None = None
        self._next_auth: asyncio.Future[asyncio.Task] | None = None
        self._token_generation = 0
        self._token_rejected = False

        self.alarm: Alarm = Alarm(self._request)
        self.location: Location = Location(self._request)
        self.water: Water = Water(self._request)
        self.device: Device = Device(self._request)
        self.presence: Presence = Presence(self._request)
        self.flodetect: Flodetect = Flodetect(self._request)

        # These endpoints will get instantiated post-authentication:
        self.user: User | None = None

    async def _request(  # pylint: disable=too-many-locals
        self,
        method: str,
        url: str,
        *,
        _allow_reauth: bool = True,
        _anonymous: bool = False,
        **kwargs,
    ) -> dict:
        """Make a request against the API."""
        if _anonymous:
            return await self._send_request(method, url, token=None, **kwargs)

        await self._ensure_token()
        retried = False
        while True:
            next_auth = self._next_auth
            token = self._token
            generation = self._token_generation
            try:
                return await self._send_request(method, url, token=token, **kwargs)
            except RequestError as err:
                cause = err.__cause__
                if not isinstance(cause, ClientResponseError) or cause.status != 401:
                    raise
                # A late response must not invalidate a newer token.
                if generation == self._token_generation:
                    self._token_rejected = True
                if not _allow_reauth or retried:
                    raise
                _LOGGER.info("Access token rejected; refreshing and retrying")
                # Keep the outcome of the first recovery after this request was sent,
                # even if newer requests have since started another authentication.
                if next_auth is not None and next_auth.done():
                    await asyncio.shield(next_auth.result())
                await self._ensure_token()
                retried = True

    async def _ensure_token(self, *, full_login: bool = False) -> None:
        """Share authentication, including callers arriving mid-refresh."""
        if self._next_auth is None:
            self._next_auth = asyncio.get_running_loop().create_future()
        if self._auth_task is None or self._auth_task.done():
            if (
                not full_login
                and self._token
                and not self._token_rejected
                and self._token_expiration
                and datetime.now() < self._token_expiration
            ):
                return
            if not full_login:
                _LOGGER.info("Requesting new access token to replace expired one")
            self._auth_task = asyncio.create_task(self._authenticate(full_login))
            self._next_auth.set_result(self._auth_task)
            self._next_auth = asyncio.get_running_loop().create_future()
            # Retrieve failures even if every waiter has been cancelled.
            self._auth_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._auth_task)

    async def _authenticate(self, full_login: bool) -> None:
        """Publish one complete authentication generation."""
        if self._use_sso:
            if full_login:
                await self._async_authenticate_sso()
            else:
                await self._async_refresh_sso()
        else:
            await self._async_authenticate_legacy()
        self._token_generation += 1
        self._token_rejected = False

    async def _send_request(
        self, method: str, url: str, *, token: str | None, **kwargs
    ):
        """Send once without entering authentication or mutating caller headers."""
        kwargs["headers"] = CIMultiDict(kwargs.get("headers") or {})
        kwargs["headers"].popall("Authorization", None)
        kwargs["headers"].update(
            {
                "Accept": DEFAULT_HEADER_ACCEPT,
                "Content-Type": DEFAULT_HEADER_CONTENT_TYPE,
                "Host": urlparse(url).netloc,
                "Origin": DEFAULT_HEADER_ORIGIN,
                "Referrer": DEFAULT_HEADER_REFERER,
                "User-Agent": DEFAULT_HEADER_USER_AGENT,
            }
        )

        if token:
            kwargs["headers"]["Authorization"] = (
                f"Bearer {token}" if self._use_sso else token
            )

        # A borrowed session with default credentials cannot make an anonymous
        # token request safely. Default BasicAuth also conflicts with token headers.
        use_running_session = (
            self._session
            and not self._session.closed
            and not self._session.auth
            and not (token is None and "Authorization" in self._session.headers)
        )

        if use_running_session and self._session is not None:
            session = self._session
        else:
            session = ClientSession(timeout=ClientTimeout(total=DEFAULT_TIMEOUT))

        try:
            async with session.request(method, url, **kwargs) as resp:
                try:
                    resp.raise_for_status()
                except ClientResponseError as err:
                    if (
                        not self._use_sso
                        and method.lower() == "post"
                        and url == f"{API_V1_BASE}/users/auth"
                        and err.status == 400
                    ):
                        try:
                            data = await resp.json(content_type=None)
                        except (
                            ClientError,
                            TimeoutError,
                            JSONDecodeError,
                            UnicodeDecodeError,
                        ):
                            pass
                        else:
                            if (
                                isinstance(data, dict)
                                and data.get("error") is True
                                and data.get("message") == "Invalid username/password."
                            ):
                                raise CredentialsRejectedError(
                                    "Legacy login credentials were rejected"
                                ) from err
                    raise
                return await resp.json(content_type=None)
        except (ClientError, TimeoutError, JSONDecodeError, UnicodeDecodeError) as err:
            raise RequestError(f"There was an error while requesting {url}") from err
        finally:
            if not use_running_session:
                await session.close()

    async def async_authenticate(self) -> None:
        """Authenticate, sharing any authentication already in progress."""
        await self._ensure_token(full_login=True)

    async def _async_authenticate_legacy(self) -> None:
        """Authenticate via the legacy Flo ``users/auth`` flow."""
        auth_response: dict = await self._request(
            "post",
            f"{API_V1_BASE}/users/auth",
            json={"username": self._username, "password": self._password},
            _anonymous=True,
        )

        try:
            token = auth_response["token"]
            expiration = datetime.fromtimestamp(
                auth_response["tokenPayload"]["timestamp"]
                + auth_response["tokenExpiration"]
            )
            user_id = auth_response["tokenPayload"]["user"]["user_id"]
            if not isinstance(token, str) or not token or not user_id:
                raise ValueError("Missing token or user id")
            if expiration <= datetime.now():
                raise ValueError("Expired token")
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
            raise RequestError("Invalid legacy authentication response") from err
        self._token = token
        self._token_expiration = expiration
        if not self._user_id:
            self._user_id = user_id
            self.user = User(self._request, user_id)

    async def _async_sso_token_request(self, payload: dict) -> dict:
        """Post to the SSO token endpoint and return the token object."""
        response: dict = await self._request(
            "post",
            SSO_TOKEN_URL,
            json={**payload, "client_id": SSO_CLIENT_ID},
            _allow_reauth=False,
            _anonymous=True,
        )
        # The endpoint nests the token; tolerate a flat response too.
        if not isinstance(response, dict):
            raise RequestError("Invalid SSO token response")
        token = response.get("token", response)
        if not isinstance(token, dict):
            raise RequestError("Invalid SSO token response")
        return token

    async def _store_sso_token(self, token: dict, *, full_login: bool = False) -> None:
        """Validate all fields and user lookup before publishing credentials."""
        try:
            access_token = token["access_token"]
            refresh_token = token.get(
                "refresh_token", None if full_login else self._refresh_token
            )
            lifetime = int(token.get("expires_in", 3600))
            if not isinstance(access_token, str) or not access_token or lifetime <= 0:
                raise ValueError("Invalid access token or lifetime")
            if refresh_token is not None and (
                not isinstance(refresh_token, str) or not refresh_token
            ):
                raise ValueError("Invalid refresh token")
            # Keep short-lived tokens usable without immediately refreshing again.
            expiration = datetime.now() + timedelta(
                seconds=lifetime - min(SSO_EXPIRY_MARGIN, lifetime / 2)
            )
        except (KeyError, TypeError, ValueError, OverflowError) as err:
            raise RequestError("Invalid SSO token response") from err

        user_id = self._user_id
        if not user_id:
            me = await self._send_request(
                "get", f"{API_V2_BASE}/users/me", token=access_token
            )
            if not isinstance(me, dict) or not me.get("id"):
                raise RequestError("SSO user response returned no user id")
            user_id = me["id"]
        if expiration <= datetime.now():
            raise RequestError("SSO token expired during user lookup")
        self._token = access_token
        self._refresh_token = refresh_token
        self._token_expiration = expiration
        if not self._user_id:
            self._user_id = user_id
            self.user = User(self._request, user_id)

    async def _async_authenticate_sso(self) -> None:
        """Authenticate via the Moen SSO (Cognito) flow."""
        token = await self._async_sso_token_request(
            {"username": self._username, "password": self._password}
        )
        await self._store_sso_token(token, full_login=True)

    async def _async_refresh_sso(self) -> None:
        """Exchange the refresh token, falling back to a full login."""
        if not self._refresh_token:
            await self._async_authenticate_sso()
            return

        try:
            token = await self._async_sso_token_request(
                {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
            )
        except RequestError as err:
            # Preserve the existing HTTP 400/401 fallback, but do not turn
            # transport failures, rate limits, or server outages into logins.
            cause = err.__cause__
            if not isinstance(cause, ClientResponseError) or cause.status not in (
                400,
                401,
            ):
                raise
            _LOGGER.info("Refresh token rejected; falling back to a full login")
            await self._async_authenticate_sso()
            return

        await self._store_sso_token(token)


async def async_get_api(
    username: str,
    password: str,
    *,
    session: ClientSession | None = None,
    use_sso: bool = False,
) -> API:
    """Instantiate an authenticated API object.

    :param session: An ``aiohttp`` ``ClientSession``
    :type session: ``aiohttp.client.ClientSession``
    :param username: A Flo email address
    :type username: ``str``
    :param password: A Flo password
    :type password: ``str``
    :param use_sso: Use the Moen SSO (Cognito) auth flow instead of the legacy one
    :type use_sso: ``bool``
    :rtype: :meth:`aioflo.api.API`
    """
    api = API(username, password, session=session, use_sso=use_sso)
    await api.async_authenticate()
    return api
