"""Exercise log query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_exercises(
    start_date: str | None = None,
    end_date: str | None = None,
    exercise_type: str | None = None,
    live: bool = False,
) -> str:
    """Get exercise log entries (individual tracked activities).

    Returns exercise sessions from the local cache by default. Use live=True
    to fetch from the API. Run health_sync first to populate the cache.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        exercise_type: Filter by activity name (case-insensitive substring match),
            e.g. "cycling", "walk", "run". Default: all types.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns exercise entries with name, duration, calories, avg heart rate,
    distance, and source (auto-detect vs manual).
    Note: HR data from cycling may be unreliable (optical sensor vs handlebar grip).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("exercises", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_exercises(conn, start.isoformat(), end.isoformat(), exercise_type)
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No exercise entries found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )

    return format_response({"exercises": entries, "count": len(entries)})
