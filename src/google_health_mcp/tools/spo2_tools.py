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

    Returns one entry per night: avg SpO2 percentage and up to two pairs of
    bounds. avg_ci_low/avg_ci_high come from this API, which Google
    describes as the lower and upper bound of the confidence interval of
    oxygen saturation samples during sleep. min/max hold whatever an import
    carried; on the history checked, they matched avg_ci_low/avg_ci_high on
    every night carrying both. std_dev is Google's standard deviation of the
    daily SpO2 averages over the past 7-30 days, not of this night's samples,
    and data_source the device and platform Google says recorded it.
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
