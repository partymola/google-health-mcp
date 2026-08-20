"""Active Zone Minutes (AZM) query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_azm(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily Active Zone Minutes (AZM), the headline cardio metric.

    AZM counts minutes spent in heart rate zones at or above Fat Burn intensity.
    `total_minutes` is the plain sum of the three zone columns as reported, not a
    weighted one. Returns from local cache by default, auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with total_minutes plus per-zone breakdown
    (fat_burn_minutes, cardio_minutes, peak_minutes).
    Distinct from active_minutes in health_get_activity, which counts wall-clock
    minutes regardless of intensity - and which has no source in this API, so it
    is present only for days that arrived by import.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("azm", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_azm(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No AZM data found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )

    return format_response({"azm": entries, "count": len(entries)})
