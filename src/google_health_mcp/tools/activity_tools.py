"""Daily activity query tool."""

import anyio

from .. import db
from ..helpers import empty_answer, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool(structured_output=False)
@require_auth
async def health_get_activity(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily activity summaries (steps, calories, active minutes, distance).

    Returns data from the local cache by default. Use live=True to fetch
    from the API. Run health_sync first to populate the cache.

    Note: live=True fetches one API call per day - avoid large ranges to
    stay within the 150 requests/hour rate limit.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with steps, calories, active minutes, distance.
    active_minutes = very_active + fairly_active (excludes lightly active).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("activity", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_activity(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return empty_answer("No activity data found for this period.", live)

    return format_response({"activity": entries, "count": len(entries)})


#: Each event type the tool serves, and the field its points carry it under.
_EVENT_TYPES = (
    ("sedentary_periods", "sedentaryPeriod"),
    ("altitude_gains", "altitude"),
    ("swim_lengths", "swimLengthsData"),
)


@mcp.tool(structured_output=False)
@require_auth
async def health_get_activity_events(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
    include_records: bool = False,
) -> str:
    """Get activity events over a date range: sedentary periods, altitude gains, swim lengths.

    Each event's start and end (UTC) and the values Google sends with it, as
    sent: gainMillimeters for an altitude gain, strokeCount for a swim length,
    integers often as strings. Google counts altitude among its true zeros, so
    an interval with no gain field is a gain of none. Swim lengths are
    detected passively by the watch and need not belong to a swim workout.
    Sedentary periods are read from Google's merge of every source rather
    than from each source's own list.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "7d". Default: 7
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.
        include_records: If true, return each event's point exactly as Google
            sent it instead of its interval and values. Altitude and swim
            points carry their data source; Google's merged sedentary stream
            carries none.

    Returns sedentary_periods, altitude_gains and swim_lengths, each a list of
    events with the local date it started on, and count, the events in all.
    """
    start, end = parse_date(start_date, end_date, default_days=7)

    def _refresh():
        for data_type, _field in _EVENT_TYPES:
            refresh_before_query(data_type, start, end, live)

    await anyio.to_thread.run_sync(_refresh)

    def _query():
        conn = db.get_db()
        try:
            return {
                table: db.query_day_lists(conn, table, start.isoformat(), end.isoformat())
                for table, _field in _EVENT_TYPES
            }
        finally:
            conn.close()

    days = await anyio.to_thread.run_sync(_query)

    body = {}
    for table, field in _EVENT_TYPES:
        events = []
        for day in days[table]:
            for point in day["points"] or []:
                if include_records:
                    events.append({"date": day["date"], "record": point})
                    continue
                payload = point.get(field) if isinstance(point, dict) else None
                if not isinstance(payload, dict):
                    continue
                interval = payload.get("interval") or {}
                events.append(
                    {
                        **{k: v for k, v in payload.items() if k != "interval"},
                        "date": day["date"],
                        "start_time": interval.get("startTime"),
                        "end_time": interval.get("endTime"),
                    }
                )
        body[table] = events

    count = sum(len(events) for events in body.values())
    if not count:
        return empty_answer("No activity events found for this period.", live)
    return format_response({**body, "count": count})
