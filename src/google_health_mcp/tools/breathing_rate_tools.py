"""Breathing rate query tool."""

import anyio

from .. import db
from ..helpers import empty_answer, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool(structured_output=False)
@require_auth
async def health_get_breathing_rate(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
    include_sleep_summaries: bool = False,
) -> str:
    """Get nightly breathing rate (avg breaths per minute during sleep).

    Sourced during sleep tracking. Useful as an illness/recovery signal:
    sustained increases of 2-3 bpm above personal baseline can indicate
    incipient infection or strain. Returns from cache by default,
    auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.
        include_sleep_summaries: If true, also return Google's breathing-rate
            summaries by sleep stage: rate, signal-to-noise and standard
            deviation for the full sleep and for deep, light and REM sleep. A
            night can carry several, each at its own sample time.

    Returns one entry per night with breaths_per_min and data_source (the device
    and platform Google says recorded it), and count, the nights. With the
    flag also sleep_summaries, each with the local date of its sample,
    sample_time (UTC), utc_offset and data_source, and summary_count.
    Typical adult range: 12-20 bpm at rest.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    # Together, so the daily gate syncs the nights and their summaries at once.
    refreshed = (
        ("breathing_rate", "respiratory_rate_sleep")
        if include_sleep_summaries
        else "breathing_rate"
    )
    await anyio.to_thread.run_sync(lambda: refresh_before_query(refreshed, start, end, live))

    def _query():
        conn = db.get_db()
        try:
            nights = db.query_breathing_rate(conn, start.isoformat(), end.isoformat())
            days = (
                db.query_day_lists(
                    conn, "respiratory_rate_sleep", start.isoformat(), end.isoformat()
                )
                if include_sleep_summaries
                else []
            )
            return nights, days
        finally:
            conn.close()

    entries, days = await anyio.to_thread.run_sync(_query)

    summaries = []
    for day in days:
        for point in day["points"] or []:
            payload = point.get("respiratoryRateSleepSummary") if isinstance(point, dict) else None
            if not isinstance(payload, dict):
                continue
            summaries.append(
                {
                    **{k: v for k, v in payload.items() if k != "sampleTime"},
                    "date": day["date"],
                    "sample_time": (payload.get("sampleTime") or {}).get("physicalTime"),
                    "utc_offset": (payload.get("sampleTime") or {}).get("utcOffset"),
                    "data_source": point.get("dataSource"),
                }
            )

    if not entries and not summaries:
        return empty_answer(
            "No breathing rate data found for this period.", live, "Requires sleep tracking."
        )

    body = {"breathing_rate": entries, "count": len(entries)}
    if include_sleep_summaries:
        body["sleep_summaries"] = summaries
        body["summary_count"] = len(summaries)
    return format_response(body)
