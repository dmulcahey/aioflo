"""Disposable live capture; never run by pytest.

Python 3.11+: from the repository root:
    python -m venv .venv
    .venv/bin/python -m pip install -e .
Edit USERNAME, PASSWORD, LOCATION_ID, and TIMEZONE directly in
capture/timezone_live.py (this runnable file is tracked in Git).
Optionally set DEVICE_MAC and USE_SSO. Then:
    .venv/bin/python -m capture.timezone_live

Do not commit your credential edits. Clear them after capture.
JSON is written beneath ignored capture/output/ in a unique directory per run.
Share that directory only after review: raw consumption bodies retain private
location/device identifiers and water usage. No blanket anonymization is done.
Auth requests/responses and headers are never captured; errors record types only.
An empty response is valid evidence, not proof of timezone correctness. Historical
DST data may be outside API retention; adjust ANCHOR_DATE to a useful past date.
For replay, use exchanges[].response_text as the HTTP body with the recorded
status and content_type, matched by method/url/params. arguments records the
public Water call; tzinfo_key/fold preserve information absent from ISO strings.
At most 24 consumption calls are scheduled (plus bounded API auth retries).
Zones without an offset transition in the prior 370 days omit DST cases.
LOCATION_ID is the Flo location UUID; DEVICE_MAC is optional, not a device ID.
Exit 1 means one or more scenarios (or login) failed; inspect summary.json.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

from aioflo import async_get_api
from aioflo.const import API_V2_BASE

USERNAME = ""
PASSWORD = ""
LOCATION_ID = ""
TIMEZONE = "America/Denver"  # Set to the Flo location's actual IANA timezone.
DEVICE_MAC = None  # Optional: adds a device-filtered comparison.
USE_SSO = False
ANCHOR_DATE = None  # Optional ISO date, e.g. "2026-11-02"; default: yesterday.
BASE_COMMIT = "817afc891cf994b2adc2c3996d8a84a34f557124"
CONSUMPTION_URL = f"{API_V2_BASE}/water/consumption"


def scenarios(zone_name, anchor, mac=None):
    """Build deterministic ranges, including the latest past DST transitions."""
    zone = ZoneInfo(zone_name)
    midnight = datetime.combine(anchor, datetime.min.time(), zone)
    ranges = [("day", midnight, midnight + timedelta(days=1), "1h")]
    month = midnight.replace(day=1)
    year = midnight.replace(month=1, day=1)
    for name, boundary in (("month_boundary", month), ("year_boundary", year)):
        ranges.append(
            (name, boundary - timedelta(days=1), boundary + timedelta(days=1), "1h")
        )
    ranges.extend(
        [
            ("daily_buckets", midnight - timedelta(days=7), midnight, "1d"),
            ("monthly_buckets", year.replace(year=year.year - 1), year, "1m"),
        ]
    )
    found = set()
    for days_ago in range(370):
        start = midnight - timedelta(days=days_ago)
        end = start + timedelta(days=1)
        delta = end.utcoffset() - start.utcoffset()
        if (
            delta
            and (kind := "spring_forward" if delta > timedelta(0) else "fall_back")
            not in found
        ):
            ranges.append((kind, start, end, "1h"))
            found.add(kind)
    result = []
    for name, start, end, interval in ranges:
        for variant, tz in (("omitted", None), ("local", zone_name), ("utc", "UTC")):
            result.append((f"{name}_{variant}", start, end, interval, tz, None))
    result.extend(
        [
            (
                "day_utc_instants",
                midnight.astimezone(timezone.utc),
                (midnight + timedelta(days=1)).astimezone(timezone.utc),
                "1h",
                zone_name,
                None,
            ),
            (
                "day_naive_local",
                midnight.replace(tzinfo=None),
                (midnight + timedelta(days=1)).replace(tzinfo=None),
                "1h",
                zone_name,
                None,
            ),
        ]
    )
    if mac:
        result.append(
            (
                "day_device_filtered",
                midnight,
                midnight + timedelta(days=1),
                "1h",
                zone_name,
                mac,
            )
        )
    return result


class CaptureSession:
    """Observe consumption bodies before API error handling, including retries."""

    def __init__(self, session):
        self.session = session
        self.exchanges = []

    @property
    def closed(self):
        """Expose the session state expected by API."""
        return self.session.closed

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        """Allowlist only consumption; never retain headers or auth traffic."""
        exchange = None
        if method.lower() == "get" and url == CONSUMPTION_URL:
            exchange = {
                "method": method,
                "url": url,
                "params": dict(kwargs.get("params", {})),
            }
            self.exchanges.append(exchange)
        try:
            async with self.session.request(method, url, **kwargs) as response:
                if exchange is not None:
                    exchange.update(
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                    )
                    exchange["response_text"] = await response.text()
                    try:
                        exchange["response_json"] = json.loads(
                            exchange["response_text"]
                        )
                    except ValueError:
                        exchange["response_json"] = None
                yield response
        except Exception as err:
            if exchange is not None:
                exchange["error_type"] = type(err).__name__
            raise


def describe_datetime(value):
    """Preserve timezone and fold alongside serialized public arguments."""
    return {
        "iso": value.isoformat(),
        "tzinfo_key": getattr(value.tzinfo, "key", None),
        "fold": value.fold,
    }


def write_json(path, data):
    """Write each result immediately so later failures do not lose evidence."""
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, indent=2, ensure_ascii=False)
        output.write("\n")


async def capture_scenario(api, recorder, scenario, location_id, directory):
    """Capture one call, retaining failure evidence and continuing the run."""
    name, start, end, interval, tz, mac = scenario
    recorder.exchanges = []
    record = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "scenario": name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "arguments": {
            "location_id": location_id,
            "start": describe_datetime(start),
            "end": describe_datetime(end),
            "interval": interval,
            "timezone": tz,
            "timezone_omitted": tz is None,
            "device_mac_address": mac,
        },
    }
    try:
        kwargs = {} if tz is None else {"timezone": tz}
        record["result"] = await api.water.get_consumption_info(
            location_id, start, end, interval, mac, **kwargs
        )
        record["outcome"] = "success"
    except Exception as err:
        record.update(outcome="error", error_type=type(err).__name__)
    record["exchanges"] = recorder.exchanges
    write_json(directory / f"{name}.json", record)
    return {"scenario": name, "outcome": record["outcome"]}


async def main():
    """Run the tracked capture script explicitly after editing its placeholders."""
    if not all((USERNAME, PASSWORD, LOCATION_ID)):
        raise SystemExit(
            "Set USERNAME, PASSWORD and LOCATION_ID in capture/timezone_live.py."
        )
    logging.disable(logging.CRITICAL)
    zone = ZoneInfo(TIMEZONE)
    anchor = (
        date.fromisoformat(ANCHOR_DATE)
        if ANCHOR_DATE
        else datetime.now(zone).date() - timedelta(days=1)
    )
    cases = scenarios(TIMEZONE, anchor, DEVICE_MAC)
    directory = (
        Path(__file__).parent
        / "output"
        / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
            + uuid.uuid4().hex[:8]
        )
    )
    directory.mkdir(parents=True, mode=0o700)
    summary = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "timezone": TIMEZONE,
        "anchor_date": anchor.isoformat(),
        "scenarios": [],
    }
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            recorder = CaptureSession(session)
            api = await async_get_api(
                USERNAME, PASSWORD, session=recorder, use_sso=USE_SSO
            )
            for case in cases:
                result = await capture_scenario(
                    api, recorder, case, LOCATION_ID, directory
                )
                summary["scenarios"].append(result)
                print(f"{result['scenario']}: {result['outcome']}")
    except Exception as err:
        summary["setup_error_type"] = type(err).__name__
    finally:
        write_json(directory / "summary.json", summary)
        print(f"Capture directory: {directory}")
    return int(
        "setup_error_type" in summary
        or any(item["outcome"] != "success" for item in summary["scenarios"])
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
