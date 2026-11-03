"""Offline checks for the disposable capture harness; no live Flo requests."""

from contextlib import asynccontextmanager
from datetime import date, timedelta, timezone
import json
from types import SimpleNamespace

from aiohttp import ClientResponseError
import pytest

from aioflo import async_get_api
from capture.timezone_template import CaptureSession, capture_scenario, scenarios


class FakeResponse:
    """Supply only the response interface used by API and recorder."""

    def __init__(self, body, status=200):
        """Initialize fake response."""
        self.body = body
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    async def text(self):
        """Return exact body including whitespace."""
        return self.body

    async def json(self, **kwargs):
        """Decode the body as aiohttp would."""
        return json.loads(self.body)

    def raise_for_status(self):
        """Raise an HTTP failure without needing a socket."""
        if self.status >= 400:
            raise ClientResponseError(None, (), status=self.status)


class FakeSession:
    """Provide a finite response plan and fail on unexpected calls."""

    closed = False

    def __init__(self, responses):
        """Initialize the ordered responses."""
        self.responses = iter(responses)

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        """Yield the next fake response."""
        yield next(self.responses)


def test_scenario_matrix():
    """Cover DST durations, equivalent instants and bounded scenario volume."""
    cases = scenarios("America/Denver", date(2026, 11, 2), "ab:cd")
    by_name = {case[0]: case for case in cases}
    assert len(cases) == 24
    for name, hours in (("spring_forward_local", 23), ("fall_back_local", 25)):
        _, start, end, *_ = by_name[name]
        assert end.astimezone(timezone.utc) - start.astimezone(
            timezone.utc
        ) == timedelta(hours=hours)
    assert by_name["day_local"][1:3] == by_name["day_utc_instants"][1:3]
    assert by_name["day_naive_local"][1].tzinfo is None
    assert len(scenarios("UTC", date(2026, 11, 2))) == 17


@pytest.mark.asyncio
async def test_raw_success_failure_and_auth_exclusion(tmp_path):
    """Keep exact raw evidence and query params; exclude all auth material."""
    auth = {
        "token": "SECRET_TOKEN",
        "tokenPayload": {"timestamp": 9999999999, "user": {"user_id": "SECRET_USER"}},
        "tokenExpiration": 3600,
    }
    raw = ' {"items": [{"volume": 2, "time": "2026-11-02T00:00:00-07:00"}]}\n'
    recorder = CaptureSession(
        FakeSession(
            [
                FakeResponse(json.dumps(auth)),
                FakeResponse(raw),
                FakeResponse('{"error": "unavailable"}', 503),
                FakeResponse("not-json"),
                FakeResponse('{"items": []}'),
            ]
        )
    )
    api = await async_get_api("SECRET_EMAIL", "SECRET_PASSWORD", session=recorder)
    cases = scenarios("America/Denver", date(2026, 11, 2))
    outcomes = []
    for case in cases[:4]:
        outcomes.append(
            await capture_scenario(api, recorder, case, "location", tmp_path)
        )
    assert [item["outcome"] for item in outcomes] == [
        "success",
        "error",
        "error",
        "success",
    ]
    record = json.loads((tmp_path / "day_omitted.json").read_text())
    assert record["exchanges"][0]["response_text"] == raw
    assert record["result"] == json.loads(raw)
    assert "tz" not in record["exchanges"][0]["params"]
    failed = json.loads((tmp_path / "day_local.json").read_text())
    assert failed["exchanges"][0]["status"] == 503
    assert failed["exchanges"][0]["params"]["tz"] == "America/Denver"
    assert (
        failed["exchanges"][0]["params"]["startDate"]
        == record["arguments"]["start"]["iso"]
    )
    for path in tmp_path.iterdir():
        assert "SECRET" not in path.read_text()
        assert "Authorization" not in path.read_text()


@pytest.mark.asyncio
async def test_transport_failure_still_writes_result(tmp_path):
    """Record failures before any HTTP response without dumping exception text."""

    async def fail(*args, **kwargs):
        raise TimeoutError("SECRET must not be serialized")

    api = SimpleNamespace(water=SimpleNamespace(get_consumption_info=fail))
    recorder = SimpleNamespace(exchanges=[])
    case = scenarios("UTC", date(2026, 11, 2))[0]
    result = await capture_scenario(api, recorder, case, "location", tmp_path)
    assert result["outcome"] == "error"
    record = json.loads((tmp_path / "day_omitted.json").read_text())
    assert record["error_type"] == "TimeoutError"
    assert record["exchanges"] == []
    assert "SECRET" not in json.dumps(record)


@pytest.mark.asyncio
async def test_login_failure_writes_summary(tmp_path, monkeypatch):
    """Leave useful evidence even when login fails before scenario execution."""
    from capture import timezone_template as capture

    async def fail(*args, **kwargs):
        raise ValueError("SECRET auth response")

    monkeypatch.setattr(capture, "__file__", str(tmp_path / "timezone_local.py"))
    monkeypatch.setattr(capture, "USERNAME", "SECRET_EMAIL")
    monkeypatch.setattr(capture, "PASSWORD", "SECRET_PASSWORD")
    monkeypatch.setattr(capture, "LOCATION_ID", "location")
    monkeypatch.setattr(capture, "async_get_api", fail)
    monkeypatch.setattr(capture.logging, "disable", lambda level: None)
    assert await capture.main() == 1
    summaries = list((tmp_path / "output").glob("*/summary.json"))
    assert len(summaries) == 1
    record = json.loads(summaries[0].read_text())
    assert record["setup_error_type"] == "ValueError"
    assert record["scenarios"] == []
    assert "SECRET" not in summaries[0].read_text()
