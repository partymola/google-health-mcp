"""Food and water log query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_food_log(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get daily food and water log summary.

    Returns calories consumed and water intake (in mL) per day. Only populated
    where the user logs food or water by hand in a connected app. Returns from
    cache by default, auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with calories_in and water_ml.
    Days with no logging are omitted.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("food_log", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_food_log(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No food log data found for this period.",
                "hint": "Food and water are logged by hand, so most days have none.",
            }
        )

    return format_response({"food_log": entries, "count": len(entries)})
