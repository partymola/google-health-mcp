"""Breathing rate query tool."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_breathing_rate(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get nightly breathing rate (avg breaths per minute during sleep).

    Sourced during sleep tracking. Useful as an illness/recovery signal:
    sustained increases of 2-3 bpm above personal baseline can indicate
    incipient infection or strain. Returns from cache by default,
    auto-syncing if stale.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per night with breaths_per_min.
    Typical adult range: 12-20 bpm at rest.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("breathing_rate", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_breathing_rate(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No breathing rate data found for this period.",
                "hint": "Try live=True to re-fetch this window. Requires sleep tracking.",
            }
        )

    return format_response({"breathing_rate": entries, "count": len(entries)})
