"""Weight query tool."""

import anyio

from .. import db
from ..helpers import LIVE_HINT, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_weight(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get weight log entries (weight, BMI, body fat percentage).

    Returns data from the local cache by default. Use live=True to fetch
    from the API. Run health_sync first to populate the cache.

    Weight data is sparse: only days with weigh-in entries are present.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per day with weight_kg, bmi, fat_pct. Every individual
    weigh-in is in health_get_weight_readings.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("weight", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_weight(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No weight data found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )

    return format_response({"weight": entries, "count": len(entries)})


@mcp.tool()
@require_auth
async def health_get_weight_readings(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get every weigh-in and body-fat reading, as Google recorded each one.

    health_get_weight keeps one row a day; this returns every reading: its
    value in Google's own unit (weightGrams, percentage), when it was taken,
    and the device or app that recorded it.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns weight_readings and body_fat_readings, each entry with reading_id,
    datetime (local), date, and record (the reading as Google sent it), and
    count, the number of readings of both kinds.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    def _refresh():
        for data_type in ("weight_readings", "body_fat_readings"):
            refresh_before_query(data_type, start, end, live)

    await anyio.to_thread.run_sync(_refresh)

    def _query():
        conn = db.get_db()
        try:
            return (
                db.query_weight_readings(conn, start.isoformat(), end.isoformat()),
                db.query_body_fat_readings(conn, start.isoformat(), end.isoformat()),
            )
        finally:
            conn.close()

    weights, fats = await anyio.to_thread.run_sync(_query)
    if not weights and not fats:
        return format_response(
            {
                "message": "No weight or body-fat readings found for this period.",
                "hint": LIVE_HINT,
            }
        )
    return format_response(
        {"weight_readings": weights, "body_fat_readings": fats, "count": len(weights) + len(fats)}
    )


@mcp.tool()
@require_auth
async def health_get_height(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get height readings, in millimetres as Google reports them.

    Height is usually logged once, often years back, so the default window is
    the last ten years. Returns data from the local cache by default.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "Nd". Default: ten years back.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch height from the API before reading the cache.

    Returns one entry per reading with datetime, date, height_mm and provider.
    """
    start, end = parse_date(start_date, end_date, default_days=3653)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("height", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_height(conn, start.isoformat(), end.isoformat())
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)
    if not entries:
        return format_response(
            {
                "message": "No height reading found for this period.",
                "hint": "Try live=True to re-fetch this window from the API.",
            }
        )
    return format_response({"height": entries, "count": len(entries)})
