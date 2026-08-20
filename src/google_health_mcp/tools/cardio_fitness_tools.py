"""Cardio Fitness Score (VO2 Max) query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_cardio_fitness(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get Cardio Fitness Score (VO2 Max estimate).

    Estimated from resting HR, HR during walks and runs, and demographics.
    Updates roughly weekly. Returns from cache by default, auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    A reading usually carries a range or a single value; one re-synced across
    the switchover between providers can hold both. They are different
    measurements - do not average them together or fill one from the other.
    vo2_max_low and vo2_max_high are a reported band (e.g. 39-43); vo2_max is
    a single figure. All in mL/kg/min, higher being better cardiorespiratory
    fitness.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("cardio_fitness", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_cardio_fitness(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No cardio fitness data found for this period.",
                "hint": "Try live=True. Requires age/sex profile and recent walking/running data.",
            }
        )

    return format_response({"cardio_fitness": entries, "count": len(entries)})
