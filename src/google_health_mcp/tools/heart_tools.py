"""Heart rate query tool."""

import anyio

from .. import db
from ..helpers import empty_answer, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool(structured_output=False)
@require_auth
async def health_get_heart_rate(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily resting heart rate and heart rate zones.

    Returns resting HR and zone breakdown (Out of Range, Fat Burn, Cardio, Peak)
    from the local cache by default, auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with resting_hr, zones array, calculation_method
    (Google's WITH_SLEEP or ONLY_WITH_AWAKE_DATA) and data_source (the device and
    platform Google says recorded it).
    Zone data: name, minutes, caloriesOut, max/min HR for each zone. Zones are
    present only on days that arrived by import; a sync does not write them.
    Google's own zone boundaries are in health_get_heart_rate_zones.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("heart_rate", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_heart_rate(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return empty_answer("No heart rate data found for this period.", live)

    return format_response({"heart_rate": entries, "count": len(entries)})


@mcp.tool(structured_output=False)
@require_auth
async def health_get_heart_rate_zones(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get Google's daily heart-rate zone boundaries.

    Each day's four zones (LIGHT, MODERATE, VIGOROUS, PEAK) with the minimum
    and maximum beats per minute of each, as Google reports them. Boundaries
    only: time spent in each zone is not part of this. health_get_heart_rate
    carries an import's zones, with minutes, on days that arrived that way.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with zone_boundaries (the list as Google sent
    it, the bounds as strings).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("heart_rate_zones", start, end, live)
    )

    def _query():
        conn = db.get_db()
        try:
            return db.query_heart_rate_zones(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return empty_answer("No heart rate zones found for this period.", live)

    return format_response({"heart_rate_zones": entries, "count": len(entries)})
