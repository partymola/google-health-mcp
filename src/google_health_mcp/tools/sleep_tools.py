"""Sleep data query tool."""

import anyio

from .. import db
from ..helpers import LIVE_HINT, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_sleep(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get nightly sleep data (duration, stages, efficiency).

    Returns sleep data from the local cache by default. Use live=True
    to fetch from the API. Run health_sync first to populate the cache.

    Sleep data is sparse: only nights with watch-tracked sleep are present.
    Travel, off-wrist nights, or manual logs may be missing.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per night with total_minutes, efficiency, start/end times,
    and stage breakdown (deep, light, REM, wake minutes).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("sleep", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_sleep(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No sleep data found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )

    return format_response({"sleep": entries, "count": len(entries)})


#: The arrays a session carries that run to dozens of entries a night.
_SEGMENT_ARRAYS = ("stages", "shortAwakenings", "outOfBedSegments")


@mcp.tool()
@require_auth
async def health_get_sleep_sessions(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
    include_stages: bool = False,
) -> str:
    """Get each sleep session as Google recorded it, rather than the nightly total.

    A night can be several sessions (a wake-and-resume, a nap); health_get_sleep
    sums them per night, while this returns each one whole: its interval, type
    (CLASSIC or STAGES), Google's metadata (mainSleep, nap, processed,
    manuallyEdited, stagesStatus), its summary (minutesAsleep, minutesAwake,
    minutesInSleepPeriod, minutesToFallAsleep, minutesAfterWakeUp, and the
    per-stage minutes and counts), and the device that recorded it.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.
        include_stages: If true, include every stage segment, short awakening and
            out-of-bed segment. Without it each session carries only how many
            there are, since a night runs to dozens.

    Returns one entry per session with session_id, date (the local day it ended),
    start_time, end_time, provider, record (the session as Google sent it, less
    the segment arrays unless include_stages) and segment_counts (how many of
    each; absent where the stored record cannot be read).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("sleep_sessions", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_sleep_sessions(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    sessions = await anyio.to_thread.run_sync(_query)

    if not sessions:
        return format_response(
            {
                "message": "No sleep sessions found for this period.",
                "hint": LIVE_HINT,
            }
        )

    for session in sessions:
        sleep = (session.get("record") or {}).get("sleep")
        if not isinstance(sleep, dict):
            continue
        session["segment_counts"] = {
            key: len(sleep[key]) if isinstance(sleep.get(key), list) else 0
            for key in _SEGMENT_ARRAYS
        }
        if not include_stages:
            for key in _SEGMENT_ARRAYS:
                sleep.pop(key, None)

    return format_response({"sleep_sessions": sessions, "count": len(sessions)})
