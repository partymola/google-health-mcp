"""SpO2 (blood oxygen saturation) query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_spo2(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get nightly SpO2 (blood oxygen saturation) data.

    Returns data from the local cache by default. Run health_sync first to
    populate it.

    SpO2 data is sparse: only nights with on-wrist sleep tracking produce readings.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per night with avg SpO2 percentage, and a pair of
    bounds whose meaning depends on which provider recorded the night:
    min/max are the observed nightly extremes, avg_ci_low/avg_ci_high are a
    confidence interval on that night's average. Only an import fills the
    first pair and only this API fills the second, so a night covered by both
    carries all four - which is the ordinary case wherever an imported
    history overlaps the synced one. They are different measurements: never
    compare or average across them.
    Normal range: 95-100%. Below 90% may indicate sleep apnea.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("spo2", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_spo2(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No SpO2 data found for this period.",
                "hint": "Try live=True to re-fetch this window.",
            }
        )

    return format_response({"spo2": entries, "count": len(entries)})
