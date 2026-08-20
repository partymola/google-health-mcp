"""Daily activity query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
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
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with steps, calories, active minutes, distance.
    active_minutes = very_active + fairly_active (excludes lightly active).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("activity", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_activity(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No activity data found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )

    return format_response({"activity": entries, "count": len(entries)})
